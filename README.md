# LLM router

A lightweight, self-hosted HTTP API and load balancer for the `agy` CLI with automated quota-based routing.

It allows you to expose local `agy` non-interactive CLI runs over secure HTTPS and distribute requests across multiple servers based on remaining 5-hour and weekly quotas.

---

## Architecture & How It Works

`llm-router` can run on any server in one of three modes, controlled by explicit flags:

- **Worker** (`is_worker: true`, `is_router: false`):
  Runs on your servers where `agy` CLI is installed and authenticated. A background thread polls `agy -p /usage --output-format json` every 60 seconds and caches quota stats in memory and `data/limits.json`. Serves `/limits` instantly with zero delay, and runs prompts via `/chat`.
- **Router** (`is_worker: false`, `is_router: true`):
  Runs as an intelligent reverse proxy. When receiving a prompt at `/router/chat`, it polls the `/limits` of all configured workers in parallel, checks remaining weekly and 5-hour quotas, and forwards the prompt to the worker with the highest remaining quota for the requested model group.
- **Hybrid** (`is_worker: true`, `is_router: true`):
  Acts both as a local worker and as a load-balancing router.

```
 Client (your app)
        │  HTTPS (Basic Auth)
        ▼
   Caddy Proxy (:443)
        │
        ▼
  llm-router (Router mode)
   ├── Polls workers concurrently (/limits)
   ├── Filters out exhausted accounts (weekly remaining == 0)
   ├── Selects best server (highest 5h remaining quota)
   └── Proxies request to best worker
        │
   ┌────┴──────────────────────────┐
   ▼                               ▼
Worker 1 (llm1.example.com)      Worker 2 (llm2.example.com)
  ├── Serves cached /limits        ├── Serves cached /limits
  └── Executes local agy CLI       └── Executes local agy CLI
```

---

## Features

- **Zero Heavy Dependencies**: Pure Python with only `flask` and `requests`. No Docker required.
- **Instant Limits Endpoint**: `/limits` is served straight from memory cache. Quotas update automatically every 60 seconds in a background thread.
- **Model-Aware Quota Routing**: Automatically separates Gemini models and Claude/GPT models to route against the correct quota bucket.
- **Failover & Resilience**: If an eligible worker fails during execution, the router automatically fails over to the next best candidate worker.
- **Built-in CLI Validator**: Run `python3 check_config.py` to inspect syntax, system environment, and `agy` binary availability with colored terminal output.
- **Production Ready**: Ships with automated systemd installer and Caddy reverse proxy templates.

---

## Requirements

- Python 3.10+
- `agy` CLI installed and logged in on worker servers
- (Optional) Caddy for automatic HTTPS and TLS certificates

---

## Quick Start

### 1. Clone repository
```bash
git clone https://github.com/pohape/llm-router.git ~/GitHub/llm-router
cd ~/GitHub/llm-router
```

### 2. Configure
Copy the example configuration:
```bash
cp config.example.json config.json
nano config.json
```

Verify your configuration and environment:
```bash
python3 check_config.py
```

### 3. Install & Start with Systemd
Run the automated installer:
```bash
./install.sh
```

Then start the service:
```bash
sudo systemctl start llm-router
sudo systemctl status llm-router
```

### 4. Setup Caddy (HTTPS)
Add a block to `/etc/caddy/Caddyfile`:
```caddyfile
llm1.example.com {
    reverse_proxy 127.0.0.1:8741
}
```
Reload Caddy:
```bash
sudo systemctl reload caddy
```

---

## Configuration Reference (`config.json`)

```json
{
  "is_worker": true,
  "is_router": false,
  "port": 8741,
  "user": "admin",
  "password": "your-secure-password",

  "workers": [
    {
      "host": "llm1.example.com",
      "user": "admin",
      "password": "worker1-password"
    },
    {
      "host": "llm2.example.com",
      "user": "admin",
      "password": "worker2-password"
    }
  ],

  "fallback": {
    "enabled": true,
    "api_key": "sk-your-openai-api-key",
    "base_url": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
    "timeout": 120
  }
}
```

| Parameter | Type | Description |
|---|---|---|
| `is_worker` | `bool` | Enables local `agy` execution and `/limits` caching. |
| `is_router` | `bool` | Enables `/router/status` and `/router/chat` load balancing. |
| `port` | `int` | Local TCP port for Flask (default: `8741`). |
| `user` | `string` | HTTP Basic Auth username for this node. |
| `password` | `string` | HTTP Basic Auth password for this node. |
| `workers` | `list` | Target workers list (required if `is_router: true`). |
| `fallback` | `object` | Optional fallback to OpenAI-compatible API when all workers are offline or exhausted. |
| `fallback.enabled` | `bool` | Enables or disables API fallback. |
| `fallback.api_key` | `string` | API key for OpenAI or any OpenAI-compatible provider. |
| `fallback.base_url` | `string` | Base URL (default: `https://api.openai.com/v1`). Supports Groq, Together, OpenRouter, vLLM, etc. |
| `fallback.model` | `string` | Fallback model name (default: `gpt-4o-mini`). |
| `fallback.timeout` | `int` | Timeout in seconds (default: `120`). |
| `fallback.max_tokens` | `int` | (Optional) Max completion tokens. If omitted, uses model's natural default. |

---

## API Endpoints

All endpoints except `/health` require HTTP Basic Authentication (`-u user:password`).

### 1. Health Check
`GET /health` (Public, no auth)
```bash
curl https://llm1.example.com/health
```
Response:
```json
{
  "status": "ok",
  "is_worker": true,
  "is_router": false
}
```

---

### 2. Check Account Limits (Worker)
`GET /limits` (Basic Auth)
Returns quota stats instantly from memory.
```bash
curl -u admin:password https://llm1.example.com/limits
```
Response:
```json
{
  "updated_at": 1730748193,
  "updated_at_iso": "2026-11-04T19:23:13+00:00",
  "groups": {
    "gemini": {
      "name": "Gemini Models",
      "weekly_remaining": 0.984,
      "weekly_reset": "2026-11-11T10:38:40Z",
      "five_hour_remaining": 1.0,
      "five_hour_reset": "2026-11-05T00:23:13Z"
    },
    "claude_gpt": {
      "name": "Claude and GPT models",
      "weekly_remaining": 0.489,
      "weekly_reset": "2026-11-11T10:33:57Z",
      "five_hour_remaining": 1.0,
      "five_hour_reset": "2026-11-05T00:23:13Z"
    }
  }
}
```

---

### 3. Run Prompt Directly on Worker
`POST /chat` (Basic Auth)
Executes `agy` locally and returns response.
```bash
curl -X POST https://llm1.example.com/chat \
  -u admin:password \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Explain quantum superposition in 2 sentences",
    "model": "gemini-3.8-flash-high"
  }'
```
Response:
```json
{
  "conversation_id": "6363c808-fbfd-4536-a86f-f1c851fb017a",
  "status": "SUCCESS",
  "response": "Quantum superposition is...",
  "duration_seconds": 2.14,
  "usage": {
    "input_tokens": 120,
    "output_tokens": 45,
    "total_tokens": 165
  }
}
```

---

### 4. Router Status
`GET /router/status` (Basic Auth)
Polls all configured workers concurrently and returns their live health and quota breakdown.
```bash
curl -u admin:password https://llm-router.example.com/router/status
```

---

### 5. Router Load-Balanced Prompt
`POST /router/chat` (Basic Auth)
Automatically chooses the server with the most remaining quota for the requested model and proxies the execution.
```bash
curl -X POST https://llm-router.example.com/router/chat \
  -u admin:password \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Write a short haiku about routers",
    "model": "gemini-3.8-flash-high"
  }'
```
Response includes diagnostic routing headers:
- `X-Routed-Worker: llm2.example.com`
- `X-Routed-Model-Group: gemini`
- `X-Routed-5h-Remaining: 0.9500`

---

## Routing & Quota Algorithm

When a request arrives at `/router/chat`:
1. **Model Group Identification**:
   - `gemini*` models map to the `Gemini Models` bucket.
   - Claude and other models map to the `Claude and GPT models` bucket.
2. **Parallel Quota Query**:
   The router queries `/limits` across all configured workers simultaneously with a 4-second timeout.
3. **Hard Filter**:
   Any worker with weekly quota remaining fraction <= 0.001 (0%) is temporarily excluded.
4. **Ranking**:
   Remaining online workers are sorted descending by their 5-hour quota fraction.
5. **Failover**:
   The router forwards the call to the top worker. If that worker experiences a connection failure or server error, the router automatically fails over to the next candidate in the ranked list.
6. **OpenAI-Compatible Fallback**:
   If all eligible workers are exhausted (weekly quota = 0), offline, or returned errors during failover, the router automatically routes the prompt to the configured fallback API (OpenAI, OpenRouter, Groq, etc.). Response headers indicate fallback routing:
   - `X-Routed-Worker: openai-fallback`
   - `X-Routed-Model-Group: fallback`

---

## Updating
```bash
git pull
sudo systemctl restart llm-router
```
