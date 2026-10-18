# qoder2api

**Qoder (international) as an OpenAI-compatible API on a single Cloudflare
Worker** — 3 files, zero dependencies, one device-flow login, no server.

[![Deploy to Cloudflare Workers](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/nullstacks/qoder2api)

A Worker implementation of the Qoder COSY protocol, based on the
reverse-engineered client in
[RobbsLuo/Coding2API](https://github.com/RobbsLuo/Coding2API) (MIT).

## Models (verified 2026-10-18)

```
kimi-k3 · glm-5.3 · deepseek-v4.1-flash · deepseek-v4-pro · minimax-m3 ·
kimi-k2.8-preview · qwen3.7-max · qwen3.7-plus · qwen3.8-max ·
auto · efficient · performance · ultimate · sonus · cantus
```

`GET /v1/models` returns the live list (credit rate, context, modality
flags included).

## Deploy

```bash
# 1. login — prints a credential JSON (device flow; token never touches a browser)
pip install httpx
python3 qoder_oauth.py --test

# 2. deploy
npx wrangler login
npx wrangler deploy
wrangler secret put QODER_CREDENTIAL        # paste the credential JSON
wrangler secret put API_KEY                 # your client-facing key (optional lock)

# 3. test
curl https://<your-worker>.workers.dev/v1/chat/completions \
  -H "Authorization: Bearer <API_KEY>" -H "Content-Type: application/json" \
  -d '{"model":"kimi-k3","messages":[{"role":"user","content":"hi"}]}'
```

Or use the deploy button above, then set the two secrets in the dashboard.

## Endpoints

```
GET  /v1/models            live model catalog (credit rate, context, flags)
POST /v1/chat/completions  OpenAI wire (stream + non-stream, tools)
GET  /health
```

## Files

| File | What it is |
|---|---|
| `worker.js` | routes, OpenAI wire, SSE envelope decode, streaming |
| `cosy.js` | the COSY protocol: custom base64 wire encoding, MD5 request signatures, device fingerprints, AES-128-CBC identity envelope, session material |
| `rsa-pkcs1.js` | RSAES-PKCS#1-v1_5 in pure BigInt modexp (WebCrypto omits v1.5 encryption; the protocol requires it) — DER SPKI parser included |
| `qoder_oauth.py` | standalone device-flow login → prints the credential JSON (`--test` verifies quota) |

Protocol notes: session keys are wrapped with the server's RSA public key;
identity JSON is AES-128-CBC encrypted with the session key doubling as the
IV; every request is MD5-signed over `payload · cosy_key · date · body ·
path`. Nothing is stored server-side — the Worker derives everything from
your credential per request.

## Credential refresh

Qoder credentials last ~30 days and the token refreshes on the server side
during normal use — a Worker deployment just uses the access token until it
expires. When chat starts returning auth errors, re-run `qoder_oauth.py`
and update the `QODER_CREDENTIAL` secret.

Self-hosting the full multi-channel engine (CodeBuddy, TRAE, CodeArts,
check-ins, quota dashboards) instead? Use the upstream
[RobbsLuo/Coding2API](https://github.com/RobbsLuo/Coding2API) — our
[QODER-SETUP.md](QODER-SETUP.md) documents the intl-channel gotchas we hit
(dual endpoint env vars, login-before-restart trap) which apply there too.

## License

MIT — COSY protocol reverse-engineering © Hongsheng Luo (Robbs); Worker
port, auth script and docs © nullstacks. See LICENSE + NOTICE.
