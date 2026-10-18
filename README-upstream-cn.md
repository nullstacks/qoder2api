# coding2api (nullstacks)

Self-hosted multi-channel AI gateway — packages **CodeBuddy (CN + intl),
TRAE SOLO, OpenCode Zen, Kilo Gateway, Qoder and CodeArts** behind one
OpenAI-compatible API with a shared credential pool, health scheduling,
unified model catalog and per-channel quota tracking.

[![Deploy to Cloudflare Workers](https://img.shields.io/badge/Note-not_Worker_hostable-gray)](#why-not-cloudflare-workers)

> Fork/deployment of [RobbsLuo/Coding2API](https://github.com/RobbsLuo/Coding2API)
> (MIT). Upstream docs: [README-upstream.md](README-upstream.md) /
> [README-upstream.en.md](README-upstream.en.md). This repo adds a
> battle-tested **[Qoder international setup guide](QODER-SETUP.md)** with the
> two config gotchas the upstream README doesn't cover.

## Verified working (2026-10-18, Qoder intl channel)

**36+ models** through one endpoint — Qoder 16 (kimi-k3, glm-5.3,
deepseek-v4.1-flash, deepseek-v4-pro, minimax-m3, kimi-k2.8-preview,
qwen3.7-max/plus, qwen3.8-max + auto-routing tiers), Zen 7 and Kilo 16
(keyless free tiers), 4 of the Qoder models chat-verified end to end.

## Quick start

```bash
git clone https://github.com/nullstacks/coding2api
cd coding2api
uv sync
# ... see QODER-SETUP.md for the full walkthrough
uv run python -m uvicorn src.main:build_app --factory --host 127.0.0.1 --port 8000
```

**Start here: [`QODER-SETUP.md`](QODER-SETUP.md)** — setup, the Qoder intl
gotchas (dual endpoint env vars, login-before-restart trap), device-flow
login automation, API-key issuance and channel notes.

## Why not Cloudflare Workers

Stateful by design: SQLite (credential vault + usage stats), background
token-refresh/check-in tasks, per-channel throttles, and a dashboard. Run it
on a VM (systemd or the included docker-compose).

## Security notes

- Credential columns are AES-encrypted at rest with `APP_SECRET` — losing it
  means re-logging every channel
- The README upstream warns: reverse proxy + auth + IP allowlist for any
  public deployment; we run it on localhost and expose via tunnels only
- Device keys and refresh tokens never transit the browser (server-side
  polling OAuth flows)

## License

MIT — © Hongsheng Luo (Robbs) for the upstream implementation; packaging and
setup docs © nullstacks. See LICENSE + NOTICE.
