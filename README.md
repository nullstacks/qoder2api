# qoder2api

**Qoder as an OpenAI-compatible API — two deployment options:**

| | Self-hosted (Python) | Cloudflare Worker |
|---|---|---|
| Files | full Coding2API engine (`src/`) | `worker.js` + `cosy.js` + `rsa-pkcs1.js` (3 files, zero deps) |
| Login | dashboard or `qoder_oauth.py` | `qoder_oauth.py` → `wrangler secret put QODER_CREDENTIAL` |
| Quota | full (probe, check-in, refresh) | uses your credential directly |
| Verified | ✅ 16 models, 4 chat-tested | ✅ 17 models, chat-tested (Kimi-K3 "WORKER-OK", `billable: false`) |

**Live Worker example:** `https://qoder2api.pushkarsingh4343.workers.dev/v1`
(lock it with your own `API_KEY` secret on deploy)

A packaged deployment of [RobbsLuo/Coding2API](https://github.com/RobbsLuo/Coding2API)
(MIT) configured for **Qoder international**.

## Models (verified 2026-10-18)

```
kimi-k3 · glm-5.3 · deepseek-v4.1-flash · deepseek-v4-pro · minimax-m3 ·
kimi-k2.8-preview · qwen3.7-max · qwen3.7-plus · qwen3.8-max ·
auto · efficient · performance · ultimate · sonus · cantus
```

## Option A — Cloudflare Worker (fastest)

```bash
# 1. login (prints credential JSON)
pip install httpx
python3 qoder_oauth.py --test

# 2. deploy
npx wrangler login
npx wrangler deploy
wrangler secret put QODER_CREDENTIAL        # paste the credential JSON
wrangler secret put API_KEY                  # your client-facing key (optional lock)

# 3. test
curl https://<your-worker>.workers.dev/v1/chat/completions \
  -H "Authorization: Bearer <API_KEY>" -H "Content-Type: application/json" \
  -d '{"model":"kimi-k3","messages":[{"role":"user","content":"hi"}]}'
```

**Worker internals** (`cosy.js`): the Qoder COSY protocol ported to
WebCrypto/pure-JS — custom base64 wire encoding, MD5 request signatures,
stable device fingerprints derived from uid, AES-128-CBC identity envelope
(key doubles as IV), and RSA PKCS#1 v1.5 session-key wrapping implemented
in raw BigInt modexp (WebCrypto omits PKCS#1 v1.5 encryption; verified
byte-for-byte against Python's `cryptography`).

## Option B — self-hosted (full engine)

```bash
git clone https://github.com/nullstacks/qoder2api
cd qoder2api
uv sync

cp .env.example.qoder .env      # APP_SECRET + admin password to fill in
uv run python scripts/create_user.py admin --role admin --password-stdin <<< "pw"

uv run python -m uvicorn src.main:build_app --factory --host 127.0.0.1 --port 8000
# login via dashboard → 凭证管理 → 登录渠道账号 → Qoder
```

Full details in **[QODER-SETUP.md](QODER-SETUP.md)**.

## The two gotchas (intl channel)

1. **Both env vars required** — `QODER_API_ENDPOINT=https://openapi.qoder.sh`
   AND `QODER_GATEWAY_ENDPOINT=https://api1.qoder.sh`. The client *prepends*
   its configured gateway host over the realm config; set only one and chat
   calls hit the Chinese gateway, which rejects the intl token with a
   misleading `403 code 105 "Login expired"`.
2. **Restart before logging in** — realm is read at startup. Login-then-fix
   auto-disables the credential as `session dead` (same misleading error).
   Already stuck? Delete + re-login, or `POST /api/credentials/<id>/revive`.

The Worker doesn't have these problems: endpoints are hardcoded to the intl
hosts, and the credential is used directly (no realm indirection).

## License

MIT — upstream © Hongsheng Luo (Robbs); COSY Worker port, auth script and
setup docs © nullstacks. See LICENSE + NOTICE.
