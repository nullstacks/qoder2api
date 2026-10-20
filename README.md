# qoder2api

**Qoder (international) as an OpenAI-compatible API** — deploy as a single
Cloudflare Worker **or run the Python Flask server locally/on any box**.
3 files per runtime, zero crypto dependencies, one device-flow login.

[![Deploy to Cloudflare Workers](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/nullstacks/qoder2api)

An implementation of the Qoder COSY protocol, based on the reverse-engineered
client in [RobbsLuo/Coding2API](https://github.com/RobbsLuo/Coding2API) (MIT).

## Two runtimes, same API

| | Cloudflare Worker | Python (Flask) |
|---|---|---|
| Files | `worker.js` + `cosy.js` + `rsa-pkcs1.js` | `server.py` + `cosy.py` |
| Runtime | edge, no server | local machine, VPS, Docker, systemd… |
| Credential | `QODER_CREDENTIAL` secret | `QODER_CREDENTIAL` env or `./credential.json` |
| Crypto | WebCrypto + pure-JS RSA | stdlib (`hashlib`, pure-Python AES + RSA) |
| Install | `npx wrangler deploy` | `pip install -r requirements.txt` |

Both expose the identical endpoints and wire format. The Python port is
byte-for-byte cross-validated against the Worker's COSY implementation
(custom base64, MD5 signatures, SPKI/RSA key wrap, AES identity envelope —
including WebCrypto's double-pad behavior).

## Models (verified 2026-10-18)

```
kimi-k3 · glm-5.3 · deepseek-v4.1-flash · deepseek-v4-pro · minimax-m3 ·
kimi-k2.8-preview · qwen3.7-max · qwen3.7-plus · qwen3.8-max ·
auto · efficient · performance · ultimate · sonus · cantus
```

`GET /v1/models` returns the live list (credit rate, context, modality
flags included).

## Login (once, either runtime)

```bash
# 1. login — prints a credential JSON (device flow; token never touches a browser)
pip install httpx
python3 qoder_oauth.py --test
```

## Deploy to Cloudflare Workers

```bash
npx wrangler login
npx wrangler deploy
wrangler secret put QODER_CREDENTIAL        # paste the credential JSON
wrangler secret put API_KEY                 # your client-facing key (optional lock)
```

Or use the deploy button above, then set the two secrets in the dashboard.

## Run the Python server

```bash
pip install -r requirements.txt          # flask + httpx
export QODER_CREDENTIAL='<credential JSON from qoder_oauth.py>'
#    — or save it once:  echo '<credential JSON>' > credential.json
export API_KEY='my-secret-key'           # optional client-facing lock

python3 server.py                        # serves on 0.0.0.0:8080 (PORT/HOST to override)

# test
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer my-secret-key" -H "Content-Type: application/json" \
  -d '{"model":"kimi-k3","messages":[{"role":"user","content":"hi"}]}'
```

Production options:

```bash
gunicorn -w 1 -b 0.0.0.0:8080 server:app          # WSGI (keep 1 worker: in-memory state)
flask --app server run --host 0.0.0.0 --port 8080 # dev server
```

A `Dockerfile` is included: `docker build -t qoder2api . && docker run -p 8080:8080 -e QODER_CREDENTIAL='...' qoder2api`.

## Endpoints

```
GET  /v1/models            live model catalog (credit rate, context, flags)
POST /v1/chat/completions  OpenAI wire (stream + non-stream, tools)
GET  /health
```

## Files

| File | What it is |
|---|---|
| `worker.js` | Worker routes, OpenAI wire, SSE envelope decode, streaming |
| `cosy.js` | the COSY protocol (JS): custom base64 wire encoding, MD5 request signatures, device fingerprints, AES-128-CBC identity envelope, session material |
| `rsa-pkcs1.js` | RSAES-PKCS#1-v1_5 in pure BigInt modexp (WebCrypto omits v1.5 encryption; the protocol requires it) — DER SPKI parser included |
| `server.py` | Flask routes, OpenAI wire, SSE envelope decode, streaming (same behavior as `worker.js`) |
| `cosy.py` | the COSY protocol (Python, stdlib-only crypto): same custom base64 / MD5 signatures / fingerprints / AES envelope / RSA wrap as `cosy.js` |
| `qoder_oauth.py` | standalone device-flow login → prints the credential JSON (`--test` verifies quota) |

Protocol notes: session keys are wrapped with the server's RSA public key;
identity JSON is AES-128-CBC encrypted with the session key doubling as the
IV; every request is MD5-signed over `payload · cosy_key · date · body ·
path`. Nothing is stored server-side — both runtimes derive everything from
your credential per request.

> Implementation quirk preserved in `cosy.py`: WebCrypto's AES-CBC encrypt
> always appends its own PKCS#7 pad block (even to aligned input), so the
> Worker's identity envelope is double-padded — upstream decrypts exactly
> that, and the Python port reproduces it byte-for-byte.

## Credential refresh

Qoder credentials last ~30 days and the token refreshes on the server side
during normal use — a deployment just uses the access token until it
expires. When chat starts returning auth errors, re-run `qoder_oauth.py`
and update the `QODER_CREDENTIAL` secret (Workers) or env/`credential.json`
(Python).

Self-hosting the full multi-channel engine (CodeBuddy, TRAE, CodeArts,
check-ins, quota dashboards) instead? Use the upstream
[RobbsLuo/Coding2API](https://github.com/RobbsLuo/Coding2API) — our
[QODER-SETUP.md](QODER-SETUP.md) documents the intl-channel gotchas we hit
(dual endpoint env vars, login-before-restart trap) which apply there too.

## License

MIT — COSY protocol reverse-engineering © Hongsheng Luo (Robbs); Worker
port, Python port, auth script and docs © nullstacks. See LICENSE + NOTICE.
