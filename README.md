# Tony-EDWARD

Predictive B2B intelligence system on Render.com.
Self-evolving, model-agnostic, zero-logging.

Built on an upgraded Agent-Reach codebase.

---

## What it does

Tony-EDWARD is a FastAPI service that:

1. **Scrapes** 10+ platforms (Twitter/X, Reddit, YouTube, News, generic web, plus a dynamic app adapter that auto-synthesizes scrapers for unknown sites).
2. **Predicts** sentiment velocity, multi-component risk scores, and engagement decay — per app, on the fly.
3. **Learns** usage patterns with a Zero-Log Pattern Extractor that stores only abstract vectors + signatures, never raw user queries.
4. **Serves** two roles:
   - **Super-Admin** — unrestricted velocity, full Render Web Terminal access, runs on primary system LLM credentials.
   - **End-User** — BYO-API key, strict token-bucket rate limits, sandboxed payloads, no terminal/admin/DB access.
5. **Deploys** to Render.com as a Docker service with a 20GB persistent disk at `/app/storage`.

## Architecture

```
app/
├── core/          config, security, zero-log middleware, terminal exec
├── scrapers/      upgraded httpx+playwright scrapers + dynamic_app_adapter
├── engine/        predictive_risk, sentiment_velocity, pattern_learning,
│                  model_router, algo_synthesizer
├── storage/       pattern_db_cache, cloudflare_r2, auto_purge (20GB limit)
└── api/v1/        search, predict, admin, terminal (WS), crypto_checkout
```

## Quick start (local)

```bash
cd tony-edward
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # then edit .env to add your LLM key
export $(grep -v '^#' .env | xargs)
python main.py
# → http://localhost:8000/docs
```

The admin key is printed to stdout on first boot. Set `SUPER_ADMIN_KEY`
in your environment to suppress that.

## Deploy to Render

1. Push this repo to GitHub.
2. In Render dashboard: New > Blueprint > select the repo.
3. Render reads `render.yaml` and provisions:
   - Web service (Docker) on the starter plan
   - 20GB persistent disk mounted at `/app/storage`
4. Set the secret env vars in the Render dashboard:
   - `SUPER_ADMIN_KEY` (generate with `python -c "import secrets; print(f'tedw_sk_{secrets.token_urlsafe(32)}')"`)
   - `JWT_SECRET`
   - `PRIMARY_LLM_API_KEY`
   - Optional: `R2_*` for pattern DB backups

## API surface

### Public

- `GET  /health`
- `GET  /`                              — service info
- `GET  /v1/search/sources`            — list available sources
- `GET  /v1/crypto/plans`              — list pricing plans
- `GET  /v1/predict/synthesize/list`    — list synthesized algorithms

### Auth (any role)

- `POST /v1/search`                     — search across sources
- `POST /v1/search/url`                 — fetch a URL
- `POST /v1/search/adaptive`            — force dynamic app adapter
- `POST /v1/predict/sentiment`          — lexicon sentiment
- `POST /v1/predict/sentiment/llm`      — LLM-refined sentiment (BYO key for end-users)
- `POST /v1/predict/sentiment/batch`    — batch sentiment
- `POST /v1/predict/velocity`           — sentiment velocity
- `POST /v1/predict/risk`               — multi-signal risk score
- `POST /v1/predict/synthesize`        — synthesize per-app algorithm
- `POST /v1/predict/engagement`         — apply engagement algorithm
- `GET  /v1/crypto/checkout`           — checkout status

### Super-Admin only (`Authorization: Bearer <SUPER_ADMIN_KEY>`)

- `GET  /v1/admin/status`               — full system status
- `GET  /v1/admin/patterns`              — list patterns (no raw text)
- `POST /v1/admin/purge`                — trigger immediate purge
- `POST /v1/admin/vacuum`               — vacuum SQLite DB
- `POST /v1/admin/r2/backup`             — backup DB to R2
- `GET  /v1/admin/r2/status`             — R2 connection status
- `GET  /v1/admin/adapter/registry`      — adapter stats
- `POST /v1/admin/adapter/override`      — pin selectors for a host
- `DELETE /v1/admin/adapter/override`    — remove override
- `POST /v1/admin/export/finetune`       — export JSONL fine-tune dataset
- `GET  /v1/admin/terminal/active`        — is a terminal session running?

### WebSocket (Super-Admin only)

- `WS  /v1/terminal`                     — interactive Render Web Terminal

Auth: first message must be `{"type":"auth","token":"<SUPER_ADMIN_KEY>"}`.
30s ping/pong keep-alive. Single seat (one admin at a time). 10-min idle
timeout. 60-min max session lifetime.

## Zero-Logging Policy

- The ASGI middleware scrubs request bodies from all logs.
- Only `request_id`, `method`, `path template`, `status`, `latency_ms` are logged.
- Pattern DB stores ONLY abstract embeddings + SHA-256 signatures. No raw text.
- Terminal I/O is in-memory only — never written to disk, never sent to R2.

## Upgrades over Agent-Reach

Agent-Reach (the source repo) is a CLI orchestrator — its channel classes
only probe for external CLI availability (`twitter-cli`, `yt-dlp`,
`rdt-cli`). They never make HTTP calls themselves. Tony-EDWARD:

- Adds direct httpx scrapers with UA rotation and 2-5s jittered delays.
- Adds Playwright fallback for anti-bot sites.
- Adds a FastAPI service layer with auth, rate limits, zero-log policy.
- Adds the predictive engine, pattern learning, and dynamic adapter.
- Adds the Render Web Terminal.
- Preserves the original Agent-Reach source under `vendor/agent-reach/`
  so upgraded modules can still fall back to the original CLI backends
  when present.

## License

MIT (inherited from Agent-Reach).
