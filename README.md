---
title: Web2API
emoji: 🧩
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 9000
pinned: false
---

# Web2API

Bridge Claude Web sessions to OpenAI / Anthropic compatible APIs. Runs as a Docker Space on Hugging Face.

## Endpoints

| Path | Description |
|------|-------------|
| `/` | Landing page |
| `/login` | Authenticate to access config |
| `/config` | Manage proxy groups & accounts |
| `/claude/v1/models` | List available models |
| `/claude/v1/chat/completions` | Chat completions (OpenAI format) |

## Supported models

| Model ID | Upstream | Notes |
|----------|----------|-------|
| `claude-sonnet-4.6` | claude-sonnet-4-6 | Sonnet 4.6 (default) |
| `claude-sonnet-4-5` | claude-sonnet-4-5 | Sonnet 4.5 |
| `claude-sonnet-4-6-thinking` | claude-sonnet-4-6 | Extended thinking enabled |
| `claude-haiku-4-5` | claude-haiku-4-5 | Haiku 4.5 (fastest) |
| `claude-opus-4-6` | claude-opus-4-6 | Opus 4.6 (most capable) |

## Quick start

1. Set required secrets in Space settings
2. Open `/login` → `/config`
3. Add a proxy group and a Claude account with `auth.sessionKey`
4. Call the API:

```bash
# Standard completion
curl $SPACE_URL/claude/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4.6","stream":true,"messages":[{"role":"user","content":"Hello"}]}'

# Sonnet 4.5
curl $SPACE_URL/claude/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-5","stream":true,"messages":[{"role":"user","content":"Hello"}]}'

# Thinking model
curl $SPACE_URL/claude/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-6-thinking","stream":true,"messages":[{"role":"user","content":"Hello"}]}'
```

## Required secrets

| Secret | Purpose |
|--------|---------|
| `WEB2API_AUTH_API_KEY` | API auth key for `/claude/v1/*` |
| `WEB2API_AUTH_CONFIG_SECRET` | Password for `/login` and `/config` |
| `WEB2API_DATABASE_URL` | PostgreSQL URL for persistent config (optional) |

## Recommended environment variables

For a small CPU Space:

```
WEB2API_BROWSER_NO_SANDBOX=true
WEB2API_BROWSER_DISABLE_GPU=true
WEB2API_BROWSER_DISABLE_GPU_SANDBOX=true
WEB2API_SCHEDULER_RESIDENT_BROWSER_COUNT=0
WEB2API_SCHEDULER_TAB_MAX_CONCURRENT=5
WEB2API_BROWSER_CDP_PORT_COUNT=6
```
