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

A Hugging Face Docker Space for bridging Claude Web to OpenAI / Anthropic compatible APIs.

- App: `/`
- Login: `/login`
- Config: `/config`
- Models: `/claude/v1/models`

## Setup

Configure these Space secrets before use:

- `WEB2API_AUTH_API_KEY`
- `WEB2API_AUTH_CONFIG_SECRET`
- `WEB2API_DATABASE_URL` *(optional, enables PostgreSQL-backed config storage)*

Then open `/config`, add your proxy and Claude account, and call the API with your configured API key.

Runtime configuration details: [docs/config.md](docs/config.md)
