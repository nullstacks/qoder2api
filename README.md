# qoder2api

**Qoder international channel as a self-hosted OpenAI-compatible API** — one
purpose, one channel, verified end to end (2026-10-18).

A packaged deployment of [RobbsLuo/Coding2API](https://github.com/RobbsLuo/Coding2API)
(MIT) configured for **Qoder international** only, with the setup guide that
covers the two undocumented gotchas that break the intl channel.

## What you get

An OpenAI-compatible endpoint (`/v1/chat/completions`, `/v1/models`,
streaming included) serving **16 Qoder models**:

```
kimi-k3 · glm-5.3 · deepseek-v4.1-flash · deepseek-v4-pro · minimax-m3 ·
kimi-k2.8-preview · qwen3.7-max · qwen3.7-plus · qwen3.8-max ·
auto · efficient · performance · ultimate · sonus · cantus
```

All four headline models chat-verified end to end (Kimi-K3, GLM-5.3,
DeepSeek V4.1 Flash, Qwen3.7-Max).

## Requirements

- Linux VM (2 vCPU / 4 GB plenty) with [uv](https://docs.astral.sh/uv/)
- A Qoder account — sign up free at https://qoder.com (international, no card)
- **Not** Cloudflare-Workers-hostable: stateful (SQLite credential vault,
  background token refresh, dashboard)

## Setup

```bash
git clone https://github.com/nullstacks/qoder2api
cd qoder2api
uv sync

# 1. secrets + config (intl endpoints — BOTH are required, see gotchas)
python3 -c "import secrets; print('APP_SECRET=' + secrets.token_urlsafe(32))" > .env
cat >> .env <<'EOF'
ADMIN_USERNAMES=admin
PUBLIC_BASE_URL=http://127.0.0.1:8000
HOST=127.0.0.1
PORT=8000
TZ=Asia/Shanghai
DATA_DIR=./data
QODER_API_ENDPOINT=https://openapi.qoder.sh
QODER_GATEWAY_ENDPOINT=https://api1.qoder.sh
EOF

# 2. dashboard admin user
uv run python scripts/create_user.py admin --role admin --password-stdin <<< "your-password"

# 3. run
uv run python -m uvicorn src.main:build_app --factory --host 127.0.0.1 --port 8000
```

## The two gotchas (why "Login expired" doesn't mean expired)

1. **Both env vars required.** The credential realm is `intl`, but the client
   *prepends* its configured gateway host over the realm config — set only
   `QODER_API_ENDPOINT` and chat calls still hit the **Chinese** gateway
   (`gateway.qoder.com.cn`), which rejects the intl token with
   `403 code 105 "Login expired"`. The token is fine; the host is wrong.

2. **Restart before logging in.** Realm comes from these env vars at startup.
   Login-then-fix leaves the credential auto-disabled as `session dead`
   (the same misleading 403). Already stuck? Delete the credential and log in
   again — or revive it:

   ```bash
   curl -b cookies.txt -H "X-Requested-With: xmlhttprequest" \
     -X POST http://127.0.0.1:8000/api/credentials/<id>/revive
   ```

## Login (device flow — token never touches the browser)

```bash
curl -c cookies.txt -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"..."}'

curl -b cookies.txt -H "X-Requested-With: xmlhttprequest" \
  -H "Content-Type: application/json" \
  -X POST http://127.0.0.1:8000/api/auth/upstream/start \
  -d '{"provider":"qoder"}'
# → open auth_url (qoder.com) in a browser, sign in, approve

curl -b cookies.txt -H "X-Requested-With: xmlhttprequest" \
  -H "Content-Type: application/json" \
  -X POST http://127.0.0.1:8000/api/auth/upstream/poll \
  -d '{"provider":"qoder","state":"<state>"}'
# → {"status":"success","credential_id":"..."}
```

The dashboard (http://127.0.0.1:8000) drives the same flow via
**凭证管理 → 登录渠道账号 → Qoder**. Credentials expire ~30 days; the
server auto-refreshes, and a truly dead session just needs one re-login.

## API key + test

Keys are DB-backed; create via the dashboard, or the admin API. Note: keys
literally contain `...` (e.g. `sk-Ab1...Xy9`) — that is the real format.

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-..." -H "Content-Type: application/json" \
  -d '{"model":"kimi-k3","messages":[{"role":"user","content":"Say OK"}]}'
```

## Other channels

This packaging keeps the full upstream engine, so CodeBuddy (CN + intl),
TRAE SOLO, OpenCode Zen and Kilo Gateway can be switched on by adding their
credentials — Zen and Kilo are already keyless. See the upstream docs:
[README-upstream.en.md](README-upstream.en.md).

## Run as a service

`systemd` unit or `docker compose up -d` (compose file included; adds
`host.docker.internal` for per-channel proxies). Keep it on localhost and
expose via tunnel/reverse proxy — the credential vault is AES-encrypted at
rest with `APP_SECRET`, and losing that secret means re-logging every account.

## License

MIT — upstream implementation © Hongsheng Luo (Robbs); packaging, intl
endpoint configuration and this guide © nullstacks. See LICENSE + NOTICE.
