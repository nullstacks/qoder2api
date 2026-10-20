# server.py — qoder2api as a local Python API (Flask).
#
# Same wire surface as worker.js (OpenAI-compatible):
#   GET  /v1/models            live model catalog (credit rate, context, flags)
#   POST /v1/chat/completions  OpenAI wire (stream + non-stream, tools)
#   GET  /health
#
# Credential comes from env QODER_CREDENTIAL (the JSON printed by
# qoder_oauth.py) or the file ./credential.json. Optional API_KEY lock.
#
# Run:  python server.py            # 0.0.0.0:8080 (env PORT/HOST override)
#       flask --app server run      # dev style
#       gunicorn -w 1 -b 0.0.0.0:8080 server:app   # production
#
# Deps: flask + httpx (httpx is the same dependency qoder_oauth.py needs).

import json
import os
import threading
import time
import uuid

import httpx
from flask import Flask, Response, request

from cosy import CosySession, qoder_encode, b64encode

GATEWAY = "https://api1.qoder.sh"
EP_CHAT = "/algo/api/v2/service/pro/sse/agent_chat_generation?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
EP_MODELS = "/algo/api/v2/model/list?Encode=1"
CLIENT_UA = "Go-http-client/2.0"

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Max-Age": "86400",
}

app = Flask(__name__)

# --- credential -----------------------------------------------------------------

_credential_lock = threading.Lock()
_credential_cache = None
_credential_loaded_at = 0.0


def _read_credential():
    global _credential_cache, _credential_loaded_at
    with _credential_lock:
        if _credential_cache is not None and time.time() - _credential_loaded_at < 30:
            return _credential_cache
        raw = os.environ.get("QODER_CREDENTIAL", "")
        if not raw:
            for path in ("credential.json", "credential.local.json"):
                if os.path.isfile(path):
                    try:
                        with open(path, encoding="utf-8") as fh:
                            raw = fh.read()
                        break
                    except OSError:
                        continue
        credential = None
        if raw:
            try:
                credential = json.loads(raw)
            except json.JSONDecodeError:
                credential = None
        _credential_cache = credential
        _credential_loaded_at = time.time()
        return credential


# --- envelope SSE parsing ----------------------------------------------------------
# Upstream frames: standard SSE (event/data lines, blank-line delimited).
# Each data is a JSON envelope {statusCodeValue, body}; body is an OpenAI
# chunk (plain JSON) or "[DONE]".


def _sse_frames(resp):
    """Yield {event, data} frames from a streaming httpx response."""
    buf = ""
    event = ""
    data_parts = []
    for chunk in resp.iter_text():
        buf += chunk
        while True:
            idx = buf.find("\n")
            if idx < 0:
                break
            line = buf[:idx].rstrip("\r")
            buf = buf[idx + 1:]
            if line == "":
                if data_parts:
                    yield {"event": event, "data": "\n".join(data_parts)}
                    event = ""
                    data_parts = []
                continue
            if line.startswith(":"):
                continue
            colon = line.find(":")
            if colon < 0:
                field, value = line, ""
            else:
                field, value = line[:colon], line[colon + 1:]
            if value.startswith(" "):
                value = value[1:]
            if field == "event":
                event = value
            elif field == "data":
                data_parts.append(value)
    if data_parts:
        yield {"event": event, "data": "\n".join(data_parts)}


# --- helpers ------------------------------------------------------------------------


def _json(data, status=200):
    return Response(json.dumps(data), status=status,
                    headers={"content-type": "application/json", **CORS_HEADERS})


def _error(message, status, code=None):
    err = {"message": message}
    if code:
        err["code"] = code
    return _json({"error": err}, status)


def _check_api_key():
    expected = os.environ.get("API_KEY", "").strip()
    if not expected:
        return True
    provided = request.headers.get("authorization", "")
    if provided.lower().startswith("bearer "):
        provided = provided[7:].strip()
    else:
        provided = provided.strip()
    return provided == expected


# --- OpenAI chunk conversion ----------------------------------------------------------


def _clean_delta(delta):
    if not isinstance(delta, dict):
        return {}
    cleaned = dict(delta)
    for key in ("extra_fields", "refusal", "reasoning_content"):
        if key in cleaned and not cleaned[key]:
            del cleaned[key]
    if isinstance(cleaned.get("tool_calls"), list) and not cleaned["tool_calls"]:
        del cleaned["tool_calls"]
    return cleaned


def _tool_calls_of(delta):
    calls = []
    for tc in delta.get("tool_calls") or []:
        if isinstance(tc, dict) and ((tc.get("function") or {}).get("name")
                                     or (tc.get("function") or {}).get("arguments")):
            calls.append(tc)
    fc = delta.get("function_call")
    if isinstance(fc, dict) and (fc.get("name") or fc.get("arguments")):
        calls.append({"type": "function", "function": fc})
    return calls


# --- routes -----------------------------------------------------------------------------


def _handle_models(credential):
    session = CosySession(credential).init()
    sign_body = qoder_encode(b"")
    url = GATEWAY + EP_MODELS
    headers = session.headers(body=sign_body, raw_url=url, sse=False,
                              accept="application/json")
    headers["content-type"] = "application/json"
    with httpx.Client(timeout=60) as client:
        resp = client.get(url, headers=headers)
    if resp.status_code >= 400:
        return _error(f"upstream {resp.status_code}: {resp.text[:200]}", 502)
    payload = resp.json()
    chat = payload.get("chat") if isinstance(payload, dict) else None
    models = []
    for m in chat if isinstance(chat, list) else []:
        if not (isinstance(m, dict) and isinstance(m.get("key"), str) and m["key"]):
            continue
        models.append({
            "id": m["key"], "object": "model", "created": 0, "owned_by": "qoder",
            "name": m.get("display_name") if isinstance(m.get("display_name"), str) else m["key"],
            "credit_rate": m.get("price_factor") if isinstance(m.get("price_factor"), (int, float)) else None,
            "context": m.get("max_input_tokens"),
            "supports_images": bool(m.get("is_vl")),
            "supports_tool_call": bool(m.get("supportsToolCall")),
            "supports_reasoning": bool(m.get("is_reasoning")),
        })
    return _json({"object": "list", "data": models})


def _build_chat_body(payload, model):
    body = {
        "messages": payload.get("messages") or [],
        "model": model,
        "stream": True,
        "agent_id": "agent_common",
    }
    for key in ("tools", "tool_choice", "temperature", "top_p", "stop",
                "presence_penalty", "frequency_penalty", "response_format"):
        if payload.get(key) is not None:
            body[key] = payload[key]
    parameters = {}
    max_tokens = payload.get("max_tokens", payload.get("max_completion_tokens"))
    if isinstance(max_tokens, (int, float)) and max_tokens > 0:
        parameters["max_tokens"] = max_tokens
    effort = payload.get("reasoning_effort")
    if not effort and isinstance(payload.get("reasoning"), dict):
        effort = payload["reasoning"].get("effort")
    if effort:
        parameters["reasoning_effort"] = str(effort)
    if parameters:
        body["parameters"] = parameters
    return body


def _handle_chat(credential):
    try:
        payload = request.get_json(force=True)
    except Exception:
        return _error("invalid JSON body", 400)
    if not isinstance(payload, dict):
        return _error("body must be a JSON object", 400)
    model = payload.get("model")
    if not model:
        return _error("'model' is required", 400)
    client_stream = payload.get("stream") is True

    session = CosySession(credential).init()
    body = _build_chat_body(payload, model)
    encoded = qoder_encode(json.dumps(body, separators=(",", ":")).encode("utf-8"))
    chat_url = GATEWAY + EP_CHAT
    headers = session.headers(body=encoded, raw_url=chat_url, model_key=model, sse=True)
    headers["content-type"] = "application/json"

    try:
        with httpx.Client(timeout=httpx.Timeout(600, connect=30)) as client:
            with client.stream("POST", chat_url, headers=headers, content=encoded.encode("utf-8")) as upstream:
                if upstream.status_code >= 400:
                    text = upstream.read().decode("utf-8", "replace")
                    return _error(f"upstream {upstream.status_code}: {text[:250]}", 502)
                return _consume_upstream(upstream, model, client_stream)
    except httpx.HTTPError as exc:
        return _error(f"upstream fetch failed: {exc}", 502)


def _consume_upstream(upstream, model, client_stream):
    completion_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    saw_any = False
    content_parts = []
    reasoning_parts = []
    finish = ""
    tools = {}
    usage = None
    error_result = None  # set to a Response to abort with an error

    def handle_frame(frame):
        nonlocal saw_any, usage, finish, error_result
        data = frame["data"].strip()
        if not data:
            return
        try:
            envelope = json.loads(data)
        except json.JSONDecodeError:
            return
        if not isinstance(envelope, dict):
            return
        status = envelope.get("statusCodeValue")
        body_str = envelope.get("body")
        if status is not None and status != 200 and body_str is None:
            error_result = _error(f"upstream envelope status {status}", 502)
            return
        # A non-200 envelope that still carries a body falls through and is
        # parsed like any other chunk (same as the Worker).
        if body_str == "[DONE]":
            error_result = "__done__"  # sentinel: stop consuming
            return
        if not isinstance(body_str, str) or not body_str:
            return
        try:
            chunk = json.loads(body_str)
        except json.JSONDecodeError:
            return
        saw_any = True
        if isinstance(chunk, dict) and chunk.get("usage"):
            usage = chunk["usage"]
        choices = chunk.get("choices") if isinstance(chunk, dict) else None
        if not choices:
            return
        choice = choices[0]
        if choice.get("finish_reason"):
            finish = choice["finish_reason"]
        delta = choice.get("delta") or {}
        if isinstance(delta.get("content"), str) and delta["content"]:
            content_parts.append(delta["content"])
        if isinstance(delta.get("reasoning_content"), str) and delta["reasoning_content"]:
            reasoning_parts.append(delta["reasoning_content"])
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index") if isinstance(tc.get("index"), int) else 0
            acc = tools.setdefault(idx, {"id": "", "name": "", "args": ""})
            if tc.get("id"):
                acc["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                acc["name"] = fn["name"]
            if isinstance(fn.get("arguments"), str):
                acc["args"] += fn["arguments"]

    for frame in _sse_frames(upstream):
        handle_frame(frame)
        if error_result is not None:
            break

    if error_result is not None and error_result != "__done__":
        return error_result

    if not saw_any:
        return _error("upstream returned an empty stream", 502)

    content = "".join(content_parts)
    reasoning = "".join(reasoning_parts)
    message = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    if tools:
        message["tool_calls"] = [
            {"id": t["id"] or "call_0", "type": "function",
             "function": {"name": t["name"], "arguments": t["args"]}}
            for _, t in sorted(tools.items())
        ]
    completion = {
        "id": completion_id, "object": "chat.completion", "created": created,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish or "stop"}],
    }
    if usage:
        completion["usage"] = usage

    if client_stream:
        # Streaming clients get a single-chunk SSE (upstream frames were
        # consumed during aggregation), same as the Worker.
        chunk_payload = {
            "id": completion_id, "object": "chat.completion.chunk",
            "created": created, "model": model,
            "choices": [{"index": 0, "delta": {
                **({"reasoning_content": reasoning} if reasoning else {}),
                **({"content": content} if content else {}),
                **({"tool_calls": message["tool_calls"]} if tools else {}),
            }, "finish_reason": finish or "stop"}],
        }
        if usage:
            chunk_payload["usage"] = usage
        sse = "data: " + json.dumps(chunk_payload) + "\n\ndata: [DONE]\n\n"
        return Response(sse, headers={
            "content-type": "text/event-stream",
            "cache-control": "no-store",
            **CORS_HEADERS,
        })
    return _json(completion)


# --- Flask wiring ------------------------------------------------------------------


@app.before_request
def _options_short_circuit():
    if request.method == "OPTIONS":
        return Response(status=204)


@app.after_request
def _cors(resp):
    for key, value in CORS_HEADERS.items():
        resp.headers.setdefault(key, value)
    return resp


@app.route("/", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    return _json({
        "ok": True, "service": "qoder2api (python)", "upstream": GATEWAY,
        "has_credential": _read_credential() is not None,
    })


@app.route("/v1/models", methods=["GET"])
def models():
    credential = _read_credential()
    if not credential:
        return _error("QODER_CREDENTIAL not set — run qoder_oauth.py and export "
                      "QODER_CREDENTIAL='<credential JSON>' (or drop ./credential.json)", 503)
    try:
        return _handle_models(credential)
    except Exception as exc:  # noqa: BLE001
        return _error(str(exc)[:200], 502)


@app.route("/v1/chat/completions", methods=["POST"])
def chat():
    credential = _read_credential()
    if not credential:
        return _error("QODER_CREDENTIAL not set — run qoder_oauth.py and export "
                      "QODER_CREDENTIAL='<credential JSON>' (or drop ./credential.json)", 503)
    if not _check_api_key():
        return _error("invalid api key", 401, code="invalid_api_key")
    return _handle_chat(credential)


@app.route("/<path:_any>", methods=["GET", "POST"])
def not_found(_any):
    method = request.method
    return _json({"error": {"message": f"unsupported {method} /{_any}"}}, 404)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "0.0.0.0")
    app.run(host=host, port=port, threaded=True)
