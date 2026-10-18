// worker.js — qoder2api: Qoder international as an OpenAI-compatible API.
// COSY-protocol port (cosy.js) + envelope SSE decode + chat/models routes.
// Credential comes from env QODER_CREDENTIAL (JSON from qoder_oauth.py).

import { CosySession, qoderEncode, b64encode } from "./cosy.js";

const GATEWAY = "https://api1.qoder.sh";
const OPENAPI = "https://openapi.qoder.sh";
const EP_CHAT = "/algo/api/v2/service/pro/sse/agent_chat_generation?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1";
const EP_MODELS = "/algo/api/v2/model/list?Encode=1";
const CLIENT_UA = "Go-http-client/2.0";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Authorization, Content-Type",
  "Access-Control-Max-Age": "86400",
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status, headers: { "content-type": "application/json", ...CORS },
  });
}

function getCredential(env) {
  const raw = env.QODER_CREDENTIAL;
  if (!raw) return null;
  try { return JSON.parse(raw); } catch { return null; }
}

// ---- envelope SSE parsing ----------------------------------------------------
// Upstream frames: standard SSE (event/data lines, blank-line delimited).
// Each data is a JSON envelope {statusCodeValue, body}; body is an OpenAI
// chunk (plain JSON) or "[DONE]".

async function* sseFrames(response) {
  const decoder = new TextDecoder();
  let buf = "";
  let event = "", dataParts = [];
  const reader = response.body.getReader();
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, idx).replace(/\r$/, "");
      buf = buf.slice(idx + 1);
      if (line === "") {
        if (dataParts.length) {
          yield { event, data: dataParts.join("\n") };
          event = ""; dataParts = [];
        }
        continue;
      }
      if (line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon < 0 ? line : line.slice(0, colon);
      let value2 = colon < 0 ? "" : line.slice(colon + 1);
      if (value2.startsWith(" ")) value2 = value2.slice(1);
      if (field === "event") event = value2;
      else if (field === "data") dataParts.push(value2);
    }
  }
  if (dataParts.length) yield { event, data: dataParts.join("\n") };
}

// ---- OpenAI chunk conversion -------------------------------------------------
function cleanDelta(delta) {
  if (!delta || typeof delta !== "object") return {};
  const cleaned = { ...delta };
  for (const key of ["extra_fields", "refusal", "reasoning_content"]) {
    if (key in cleaned && !cleaned[key]) delete cleaned[key];
  }
  if (Array.isArray(cleaned.tool_calls) && !cleaned.tool_calls.length) delete cleaned.tool_calls;
  return cleaned;
}

function toolCallsOf(delta) {
  const calls = [];
  if (Array.isArray(delta.tool_calls))
    for (const tc of delta.tool_calls)
      if (tc && (tc.function?.name || tc.function?.arguments))
        calls.push(tc);
  if (delta.function_call && (delta.function_call.name || delta.function_call.arguments))
    calls.push({ type: "function", function: delta.function_call });
  return calls;
}

// ---- routes -------------------------------------------------------------------

async function handleModels(credential) {
  const session = await new CosySession(credential).init();
  const signBody = qoderEncode(new Uint8Array(0));
  const url = GATEWAY + EP_MODELS;
  const headers = await session.headers({ body: signBody, rawUrl: url, sse: false, accept: "application/json" });
  headers["content-type"] = "application/json";
  const resp = await fetch(url, { method: "GET", headers });
  if (!resp.ok) {
    const text = await resp.text();
    return json({ error: { message: `upstream ${resp.status}: ${text.slice(0, 200)}` } }, 502);
  }
  const payload = await resp.json();
  const chat = Array.isArray(payload.chat) ? payload.chat : [];
  const models = chat
    .filter((m) => m && typeof m.key === "string" && m.key)
    .map((m) => ({
      id: m.key, object: "model", created: 0, owned_by: "qoder",
      name: typeof m.display_name === "string" ? m.display_name : m.key,
      credit_rate: typeof m.price_factor === "number" ? m.price_factor : null,
      context: m.max_input_tokens ?? null,
      supports_images: !!m.is_vl,
      supports_tool_call: !!m.supportsToolCall,
      supports_reasoning: !!m.is_reasoning,
    }));
  return json({ object: "list", data: models });
}

function buildChatBody(payload, model) {
  const body = {
    messages: payload.messages ?? [],
    model,
    stream: true,
    agent_id: "agent_common",
  };
  for (const key of ["tools", "tool_choice", "temperature", "top_p", "stop",
    "presence_penalty", "frequency_penalty", "response_format"]) {
    if (payload[key] !== undefined && payload[key] !== null) body[key] = payload[key];
  }
  const parameters = {};
  let maxTokens = payload.max_tokens ?? payload.max_completion_tokens;
  if (typeof maxTokens === "number" && maxTokens > 0) parameters.max_tokens = maxTokens;
  let effort = payload.reasoning_effort;
  if (!effort && payload.reasoning && typeof payload.reasoning === "object")
    effort = payload.reasoning.effort;
  if (effort) parameters.reasoning_effort = String(effort);
  if (Object.keys(parameters).length) body.parameters = parameters;
  return body;
}

async function handleChat(request, credential, url) {
  const payload = await request.json();
  const model = payload.model;
  if (!model) return json({ error: { message: "'model' is required" } }, 400);
  const clientStream = payload.stream === true;

  const session = await new CosySession(credential).init();
  const body = buildChatBody(payload, model);
  const raw = new TextEncoder().encode(JSON.stringify(body));
  const encoded = qoderEncode(raw);
  const chatUrl = GATEWAY + EP_CHAT;
  const headers = await session.headers({ body: encoded, rawUrl: chatUrl, modelKey: model, sse: true });

  const upstream = await fetch(chatUrl, {
    method: "POST", headers, body: new TextEncoder().encode(encoded),
  });
  if (!upstream.ok) {
    const text = await upstream.text();
    return json({ error: { message: `upstream ${upstream.status}: ${text.slice(0, 250)}` } }, 502);
  }

  const id = "chatcmpl-" + crypto.randomUUID().replace(/-/g, "").slice(0, 24);
  const created = Math.floor(Date.now() / 1000);
  let sawAny = false;

  // Accumulators for non-stream mode
  let content = "", reasoning = "", finish = "";
  const tools = new Map();
  let usage = null;

  for await (const frame of sseFrames(upstream)) {
    const data = frame.data.trim();
    if (!data) continue;
    let envelope;
    try { envelope = JSON.parse(data); } catch { continue; }
    const status = envelope.statusCodeValue;
    let bodyStr = envelope.body;
    if (status !== undefined && status !== 200) {
      if (bodyStr === undefined || bodyStr === null)
        return json({ error: { message: `upstream envelope status ${status}` } }, 502);
    }
    if (bodyStr === "[DONE]") break;
    if (typeof bodyStr !== "string" || !bodyStr) continue;
    let chunk;
    try { chunk = JSON.parse(bodyStr); } catch { continue; }
    sawAny = true;
    if (chunk.usage) usage = chunk.usage;
    const choice = chunk.choices && chunk.choices[0];
    if (!choice) continue;
    if (choice.finish_reason) finish = choice.finish_reason;
    const delta = choice.delta || {};
    if (typeof delta.content === "string" && delta.content) content += delta.content;
    if (typeof delta.reasoning_content === "string" && delta.reasoning_content)
      reasoning += delta.reasoning_content;
    if (Array.isArray(delta.tool_calls))
      for (const tc of delta.tool_calls) {
        const i = typeof tc.index === "number" ? tc.index : 0;
        if (!tools.has(i)) tools.set(i, { id: "", name: "", args: "" });
        const acc = tools.get(i);
        if (tc.id) acc.id = tc.id;
        if (tc.function) {
          if (tc.function.name) acc.name = tc.function.name;
          if (typeof tc.function.arguments === "string") acc.args += tc.function.arguments;
        }
      }
  }

  if (!sawAny)
    return json({ error: { message: "upstream returned an empty stream" } }, 502);

  // Non-stream clients get one aggregated JSON; streaming clients get a
  // single-chunk SSE (upstream frames were consumed during aggregation).
  const message = { role: "assistant", content };
  if (reasoning) message.reasoning_content = reasoning;
  if (tools.size) {
    message.tool_calls = [...tools.entries()].sort((a, b) => a[0] - b[0])
      .map(([, t]) => ({ id: t.id || "call_0", type: "function", function: { name: t.name, arguments: t.args } }));
  }
  const completion = {
    id, object: "chat.completion", created, model,
    choices: [{ index: 0, message, finish_reason: finish || "stop" }],
    ...(usage ? { usage } : {}),
  };

  if (clientStream) {
    const sse = "data: " + JSON.stringify({
      id, object: "chat.completion.chunk", created, model,
      choices: [{ index: 0, delta: {
        ...(reasoning ? { reasoning_content: reasoning } : {}),
        ...(content ? { content } : {}),
        ...(tools.size ? { tool_calls: message.tool_calls } : {}),
      }, finish_reason: finish || "stop" }],
      ...(usage ? { usage } : {}),
    }) + "\n\ndata: [DONE]\n\n";
    return new Response(sse, {
      headers: { "content-type": "text/event-stream", "cache-control": "no-store", ...CORS },
    });
  }
  return json(completion);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;

    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });

    if (path === "/" || path === "/health") {
      return json({
        ok: true, service: "qoder2api", upstream: GATEWAY,
        has_credential: !!getCredential(env),
      });
    }

    const credential = getCredential(env);
    if (!credential)
      return json({ error: { message: "QODER_CREDENTIAL secret not set — run qoder_oauth.py and `wrangler secret put QODER_CREDENTIAL`" } }, 503);

    // optional lock
    const apiKey = env.API_KEY;
    if (apiKey) {
      const auth = request.headers.get("authorization") || "";
      const provided = auth.replace(/^Bearer\s+/i, "").trim();
      if (provided !== apiKey)
        return json({ error: { message: "invalid api key", code: "invalid_api_key" } }, 401);
    }

    if (path === "/v1/models" && request.method === "GET") return handleModels(credential);
    if (path === "/v1/chat/completions" && request.method === "POST") return handleChat(request, credential, url);

    return json({ error: { message: `unsupported ${request.method} ${path}` } }, 404);
  },
};
