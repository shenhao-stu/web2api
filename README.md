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

| Path | Protocol | Description |
|------|----------|-------------|
| `/claude/v1/models` | OpenAI | List available models |
| `/claude/v1/chat/completions` | OpenAI | Chat completions |
| `/claude/v1/messages` | Anthropic | Messages API |
| `/config` | — | Admin dashboard |

## Supported models

| Model ID | Upstream | Tier | Notes |
|----------|----------|------|-------|
| `claude-sonnet-4.6` | claude-sonnet-4-6 | Free | Sonnet 4.6 (default) |
| `claude-sonnet-4-5` | claude-sonnet-4-5 | Free | Sonnet 4.5 |
| `claude-sonnet-4-6-thinking` | claude-sonnet-4-6 | Free | Sonnet 4.6 extended thinking |
| `claude-sonnet-4-5-thinking` | claude-sonnet-4-5 | Free | Sonnet 4.5 extended thinking |
| `claude-haiku-4-5` | claude-haiku-4-5 | Pro | Haiku 4.5 (fastest) |
| `claude-haiku-4-5-thinking` | claude-haiku-4-5 | Pro | Haiku 4.5 extended thinking |
| `claude-opus-4-6` | claude-opus-4-6 | Pro | Opus 4.6 (most capable) |
| `claude-opus-4-6-thinking` | claude-opus-4-6 | Pro | Opus 4.6 extended thinking |

Pro models require a Claude Pro subscription and must be enabled in the config page.

## Quick start

1. Set required secrets in Space settings
2. Open `/login` → `/config`
3. Add a proxy group and a Claude account with `auth.sessionKey`
4. (Optional) Enable Pro models toggle if your account has a Pro subscription
5. Call the API:

```bash
# OpenAI format (streaming)
curl $SPACE_URL/claude/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4.6","stream":true,"messages":[{"role":"user","content":"Hello"}]}'

# Anthropic format (streaming)
curl $SPACE_URL/claude/v1/messages \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4.6","stream":true,"max_tokens":1024,"messages":[{"role":"user","content":"Hello"}]}'

# Extended thinking
curl $SPACE_URL/claude/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-6-thinking","stream":true,"messages":[{"role":"user","content":"Solve this step by step: what is 23 * 47?"}]}'
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
