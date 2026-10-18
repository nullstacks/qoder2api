# nullstacks/coding2api — Qoder setup guide

This fork/deployment guide covers **self-hosting Coding2API on a Linux VM**
and enabling the **Qoder international channel**, with the two non-obvious
gotchas that cost us a debugging session (the upstream README doesn't spell
them out).

The gateway also covers CodeBuddy (CN + intl), TRAE SOLO, OpenCode Zen and
Kilo Gateway — Zen and Kilo need no credentials at all.

Based on [RobbsLuo/Coding2API](https://github.com/RobbsLuo/Coding2API) (MIT).

## What you get (verified 2026-10-18)

OpenAI-compatible endpoint with 36+ models:

- **Qoder (16 models)** — `kimi-k3`, `glm-5.3`, `deepseek-v4.1-flash`,
  `deepseek-v4-pro`, `minimax-m3`, `kimi-k2.8-preview`, `qwen3.7-max`,
  `qwen3.7-plus`, `qwen3.8-max`, plus auto-routing tiers (`auto`,
  `efficient`, `performance`, `ultimate`, `sonus`, `cantus`)
- **OpenCode Zen (7)** — free-tier models, no credentials
- **Kilo Gateway (16)** — free-tier models, no credentials
- (CodeBuddy / TRAE / CodeArts activate when you add their accounts)

## Requirements

- Linux VM (2 vCPU / 4 GB is plenty), public IP not required for API use
- [uv](https://docs.astral.sh/uv/) (installs its own Python 3.12)
- A **Qoder account** — sign up at https://qoder.com (international, no card)

Not Cloudflare-Workers-hostable: stateful (SQLite + credential vault +
background tasks).

## Setup

```bash
git clone https://github.com/nullstacks/coding2api
cd coding2api
uv sync

# secrets
python3 -c "import secrets; print('APP_SECRET=' + secrets.token_urlsafe(32))" > .env
python3 -c "import secrets; print('ADMIN_PASSWORD=...' )"   # pick a password

cat >> .env <<'EOF'
ADMIN_USERNAMES=admin
PUBLIC_BASE_URL=http://127.0.0.1:8000
HOST=127.0.0.1
PORT=8000
TZ=Asia/Shanghai
DATA_DIR=./data
EOF

# admin user for the dashboard
uv run python scripts/create_user.py admin --role admin --password-stdin <<< "your-password"

# run
uv run python -m uvicorn src.main:build_app --factory --host 127.0.0.1 --port 8000
```

## Qoder international channel — the two gotchas

**1. Both endpoint env vars must be set.** The credential's realm is `intl`,
but the client *prepends* its configured gateway host over the realm config —
so setting only `QODER_API_ENDPOINT` leaves chat calls hitting the **Chinese**
gateway, which rejects the intl token with `403 code 105 "Login expired"`
(misleading — the token is fine).

```bash
cat >> .env <<'EOF'
QODER_API_ENDPOINT=https://openapi.qoder.sh
QODER_GATEWAY_ENDPOINT=https://api1.qoder.sh
EOF
```

**2. Restart BEFORE logging in.** The OAuth flow's realm is picked from these
env vars at startup. If you log in first and fix the endpoints after, the
credential gets auto-disabled as `session dead` (same misleading 403). Fix:
delete the credential, restart, log in again — or revive it after restart:

```bash
curl -b cookies.txt -H "X-Requested-With: xmlhttprequest" \
  -X POST http://127.0.0.1:8000/api/credentials/<credential_id>/revive
```

### Login

```bash
# dashboard session (needed for the upstream-login API)
curl -c cookies.txt -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"..."}'

# start the device flow
curl -b cookies.txt -H "X-Requested-With: xmlhttprequest" \
  -H "Content-Type: application/json" \
  -X POST http://127.0.0.1:8000/api/auth/upstream/start \
  -d '{"provider":"qoder"}'
# → open auth_url in a browser, sign in with your Qoder account, approve

# poll (the server exchanges the token server-side; it never touches the browser)
curl -b cookies.txt -H "X-Requested-With: xmlhttprequest" \
  -H "Content-Type: application/json" \
  -X POST http://127.0.0.1:8000/api/auth/upstream/poll \
  -d '{"provider":"qoder","state":"<state from start response>"}'
# → {"status":"success","credential_id":"..."}
```

The dashboard (http://127.0.0.1:8000) does the same via
**凭证管理 → 登录渠道账号 → Qoder**.

### Issue an API key

Keys are DB-backed (created via the dashboard or admin API — note the key
format literally contains `...`, e.g. `sk-Ab1...Xy9`; that is the real key):

```bash
curl -b cookies.txt -H "X-Requested-With: xmlhttprequest" \
  -H "Content-Type: application/json" \
  -X POST http://127.0.0.1:8000/api/api-keys -d '{"name":"hermes"}'
```

## Test

```bash
curl http://127.0.0.1:8000/v1/models -H "Authorization: Bearer sk-..."

curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-..." -H "Content-Type: application/json" \
  -d '{"model":"kimi-k3","messages":[{"role":"user","content":"Say OK"}]}'
```

## Channel notes

| Channel | Auth | Quota model |
|---|---|---|
| Qoder (intl) | device-code OAuth | free tier, ~small gateway-level rate; credits from daily check-in |
| CodeBuddy CN | device-code | free quota + growth-center gifts |
| CodeBuddy intl | device-code | same protocol, separate accounts; **unreachable from some regions/IPs** (IN-based VMs get connection timeouts) |
| CodeArts | OAuth2 + pasted callback | benefit models on a daily token pool; credits for built-ins |
| TRAE SOLO | credentials JSON from its client | paid-ish |
| Zen / Kilo | none | keyless free tiers |

Run as a service: `systemd` unit or `docker compose up -d` (compose file
included upstream; adds `host.docker.internal` mapping for per-channel
proxies).

## License

MIT — upstream © Hongsheng Luo (Robbs), see LICENSE + NOTICE.
