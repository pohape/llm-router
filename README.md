# llm-router

A lightweight, self-hosted HTTP API and load balancer for the `agy` CLI with automated quota-based routing, automatic failover, and OpenAI-compatible fallback.

It allows you to expose local `agy` non-interactive CLI runs over secure HTTPS and distribute requests across multiple servers based on remaining 5-hour and weekly quotas. If all workers become unavailable or their quotas are exhausted, requests automatically fall back to an external OpenAI-compatible API provider (OpenAI, OpenRouter, Groq, Together, vLLM, etc.) with identical response formatting.

---

## Architecture & How It Works

`llm-router` can run on any server in one of three modes, controlled by explicit flags:

- **Worker** (`is_worker: true`, `is_router: false`):
  Runs on servers where the `agy` CLI is installed and authenticated. A background thread polls `agy -p /usage --output-format json` every 60 seconds and caches quota statistics in memory and `data/limits.json`. Serves `/limits` instantly with zero delay, and executes prompts via `/chat`.
- **Router** (`is_worker: false`, `is_router: true`):
  Acts as an intelligent reverse proxy. When receiving a prompt at `/router/chat`, it polls the `/limits` of all configured workers in parallel, checks remaining weekly and 5-hour quotas, and forwards the prompt to the worker with the highest remaining quota for the requested model group. If all workers fail or quotas are depleted, it automatically routes to the configured fallback API.
- **Hybrid** (`is_worker: true`, `is_router: true`):
  Acts simultaneously as a local worker and as a load-balancing router.

```
                    Client Application
                            │
                            │ HTTPS (HTTP Basic Auth)
                            ▼
                    Caddy Proxy (:443)
                            │
                            ▼
                  llm-router (Router mode)
                   ├── 1. Polls all workers concurrently (/limits)
                   ├── 2. Filters out exhausted quotas (weekly <= 0.001)
                   ├── 3. Selects best server (fewest calls in flight, then highest 5h quota)
                   └── 4. Proxies request with automatic failover chain
                            │
           ┌────────────────┴──────────────────────────┐
           │ (Normal Quota-Based Execution)            │ (Fallback if workers
           ▼                                           │  offline or exhausted)
┌───────────────────────────┐                          ▼
│ Worker 1 (llm1.example...)│               ┌──────────────────────────┐
│   └── Local agy CLI       │               │  OpenAI-Compatible API   │
│                           │               │  (OpenAI, Groq, vLLM)    │
│ Worker 2 (llm2.example...)│               └──────────────────────────┘
│   └── Local agy CLI       │
└───────────────────────────┘
```

---

## Features

- **Lightweight Dependencies**: Pure Python using only `Flask`, `Requests`, `PyYAML` and `jsonschema`. No Docker or compilation required.
- **Instant Limits Endpoint**: `/limits` is served straight from an in-memory cache. Quotas update automatically every 60 seconds in a background thread.
- **Model-Aware Quota Routing**: Automatically separates Gemini models and Claude/GPT models to route against the correct quota bucket.
- **Automatic Multi-Tier Failover**: If the top worker fails (network timeout, HTTP 5xx, HTTP 429 quota exhaustion, or — for a request with `json_schema` — a 2xx answer without a `structured_output` that the schema accepts), the router immediately tries the next best worker in the ranked list; each worker is tried at most once per call.
- **OpenAI-Compatible Fallback**: Automatically invokes an external API (OpenAI, OpenRouter, Groq, Together, or local vLLM) when all workers are offline or exhausted.
- **Reasoning Model Support**: Supports thinking/reasoning models (such as `o1`, `o3`, or custom reasoning models) with configurable `reasoning_effort` (`"low"`, `"medium"`, `"high"`), set in config only: a request's `effort` targets the agy model variant and is never carried over to the fallback model.
- **Universal Response Format**: Fallback responses are normalized to match the `agy` worker schema (`response`, `status`, `conversation_id`, `duration_seconds`, `usage.input_tokens`, `usage.output_tokens`, `usage.thinking_tokens`) while preserving standard OpenAI keys (`prompt_tokens`, `completion_tokens`) for complete dual compatibility.
- **Built-in CLI Validator**: Run `python3 check_config.py` to inspect syntax, credentials, and system environment with colored terminal output.
- **Production Ready**: Ships with an automated systemd installer and Caddy reverse proxy configurations.

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
cp config.example.yaml config.yaml
nano config.yaml
```

Verify your configuration and system environment:
```bash
python3 check_config.py
```

### 3. Install & Start with Systemd
Run the automated installer:
```bash
./install.sh
```

Start the service:
```bash
sudo systemctl start llm-router
sudo systemctl status llm-router
```

### 4. Setup Caddy (HTTPS)
Add a reverse proxy block to `/etc/caddy/Caddyfile`:
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

## Configuration Reference (`config.yaml`)

```yaml
# Role configuration (worker, router, or hybrid)
is_worker: false
is_router: true

# Default agy model for incoming prompts (required when is_router: true)
default_model: gemini-3.8-flash-high

# Server port and credentials
port: 8741
user: admin
password: your-secure-password

# Remote worker nodes (polled concurrently for quotas and load balancing)
workers:
  - host: llm1.example.com
    user: admin
    password: worker1-password

  - host: llm2.example.com
    user: admin
    password: worker2-password

# Optional fallback provider (called when all workers fail or quotas are depleted)
fallback:
  enabled: true
  api_key: sk-your-openai-api-key
  base_url: https://api.openai.com/v1
  model: gpt-6-luna
  reasoning_effort: high
  timeout: 600   # high effort answers can take minutes (up to ~200 s observed)
```

### Configuration Options

| Parameter | Type | Required | Description |
|---|---|---|---|
| `is_worker` | `bool` | Yes | Enables local `agy` execution and `/limits` background caching. |
| `is_router` | `bool` | Yes | Enables `/router/status` and `/router/chat` load balancing. |
| `default_model` | `string` | Router | Default model used for `agy` routing when the client does not specify one. **Required** when `is_router: true`. |
| `port` | `int` | No | Local TCP port for Flask (default: `8741`). |
| `user` | `string` | Yes | HTTP Basic Auth username for this node. |
| `password` | `string` | Yes | HTTP Basic Auth password for this node. |
| `workers` | `list` | Router | List of worker objects with `host`, `user`, and `password`. |
| `fallback` | `object` | No | Optional external API fallback configuration. |
| `fallback.enabled` | `bool` | Yes* | Enables or disables API fallback (*if `fallback` block present). |
| `fallback.api_key` | `string` | Yes* | API key for OpenAI or any compatible provider. |
| `fallback.base_url` | `string` | No | Base API URL (default: `https://api.openai.com/v1`). |
| `fallback.model` | `string` | No | Fallback model name (default: `gpt-4o-mini`). |
| `fallback.reasoning_effort` | `string` | No | Reasoning depth for thinking models: `"low"`, `"medium"`, `"high"`. Omit for standard models. |
| `fallback.timeout` | `int` | No | HTTP request timeout in seconds, 1–600 (default: `120`). The fallback's own budget: it is not reduced by the request's `timeout` nor by the time spent on workers. |
| `fallback.max_tokens` | `int` | No | Maximum completion tokens. If omitted, uses model default. |

---

## API Endpoints

All endpoints except `/health` require HTTP Basic Authentication (`-u user:password`).

### 1. Health Check
`GET /health` (Public, no authentication required)
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
Returns quota statistics instantly from in-memory cache.
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
      "weekly_remaining": 0.9842,
      "weekly_reset": "2026-11-11T10:38:40Z",
      "five_hour_remaining": 1.0,
      "five_hour_reset": "2026-11-05T00:23:13Z"
    },
    "claude_gpt": {
      "name": "Claude and GPT models",
      "weekly_remaining": 0.4891,
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
Executes `agy` locally and returns the parsed result. The `model` parameter is **strictly required**.
```bash
curl -X POST https://llm1.example.com/chat \
  -u admin:password \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Explain quantum superposition in two sentences",
    "model": "gemini-3.8-flash-high",
    "effort": "low",
    "timeout": 120
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
    "total_tokens": 165,
    "thinking_tokens": 30
  }
}
```

#### Tools are disabled
`agy` is an agent CLI: by default the model sees tools (terminal commands, file edits, search, MCP) and in headless mode a tool call that needs permission is auto-denied, leaving an empty `response` with `denied_actions`. The worker therefore never runs the default agent. It starts `agy` with `--agent` from the workspace `agy_workspace/`, whose custom agents (`agy_workspace/.agents/agents/*.md`, format: https://antigravity.google/docs/subagents) declare `tools: []`, `excludeDefaultComponents: true` and `inheritMcp: false`, so the model gets no tool at all and answers in text:

- `router-text` — every request without `json_schema`; no tools.
- `router-json` — requests with `json_schema`; its only tool is `finish`, through which `agy --json-schema` returns the structured output.

This is not configurable. `--dangerously-skip-permissions` is never passed.

#### Structured output (`json_schema`)
Optional `json_schema` (a JSON object whose root has `"type": "object"`) is passed to `agy --json-schema`. The validated result arrives in the `structured_output` field (`response` carries the same JSON as text). Other schemas are refused with HTTP 400. The router forwards the field to workers unchanged and refuses with HTTP 400 a schema that is not valid JSON Schema (draft 2020-12) or has a `$ref` that does not resolve inside the schema. A worker's 2xx answer counts only if it carries a `structured_output` valid against the schema; otherwise (status `SUCCESS` without the field included) the router logs one line with the worker, its status and the reason, and tries the next worker, then the fallback. The OpenAI-compatible fallback sends it as Chat Completions structured output (`response_format: {"type": "json_schema", "json_schema": {"name", "schema", "strict"}}`), always with `strict: true`, so the answer is guaranteed to match the schema; therefore the router refuses with HTTP 400 any schema in which some object does not set `additionalProperties: false` or does not list all its properties in `required` (OpenAI's strict-mode requirement); the parsed object comes back in `structured_output` the same way, and an answer that is not a JSON object is an error (HTTP 502).
```bash
curl -X POST https://llm1.example.com/chat -u admin:password -H "Content-Type: application/json" \
  -d '{"prompt": "Suggest two MA configs", "model": "gemini-3.8-flash-low",
       "json_schema": {"type": "object", "properties": {"notes": {"type": "string"}}, "required": ["notes"]}}'
```

---

### 4. Router Status
`GET /router/status` (Basic Auth)
Polls all configured workers concurrently and returns live connectivity, quota breakdown, and fallback status.
```bash
curl -u admin:password https://llm-router.example.com/router/status
```
Response:
```json
{
  "total_workers": 2,
  "online_workers": 2,
  "default_model": "gemini-3.8-flash-high",
  "fallback_configured": true,
  "fallback_model": "gpt-4o-mini",
  "fallback_reasoning_effort": "medium",
  "workers": [
    {
      "host": "llm1.example.com",
      "online": true,
      "status_code": 200,
      "error": null,
      "quotas": {
        "gemini": {
          "weekly_remaining": 0.9842,
          "five_hour_remaining": 1.0
        },
        "claude_gpt": {
          "weekly_remaining": 0.4891,
          "five_hour_remaining": 0.85
        }
      }
    }
  ]
}
```

---

### 5. Router Load-Balanced Prompt
`POST /router/chat` (Basic Auth)
Automatically selects the optimal worker with the highest remaining quota for the requested model group. If the `model` field is omitted from the request body, the router automatically resolves it to the configured `default_model` before forwarding to workers. If workers fail or are depleted, falls back automatically to the external API.

```bash
curl -X POST https://llm-router.example.com/router/chat \
  -u admin:password \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Write a short haiku about routers",
    "model": "gemini-3.8-flash-high",
    "effort": "medium"
  }'
```

#### Successful Response from Worker:
Response headers include diagnostic routing metadata:
- `X-Routed-Worker: llm2.example.com`
- `X-Routed-Model-Group: gemini`
- `X-Routed-5h-Remaining: 0.9500`

Body:
```json
{
  "conversation_id": "fab7fa21-8768-4766-a5a8-e6a48c90ebb6",
  "status": "SUCCESS",
  "response": "Packets find their way,\nThrough the wires and wireless skies,\nGuiding every bit.\n",
  "duration_seconds": 2.49,
  "usage": {
    "input_tokens": 6918,
    "output_tokens": 84,
    "total_tokens": 7002,
    "thinking_tokens": 82
  }
}
```

#### Successful Response via Fallback (Workers Offline or Depleted):
Response headers indicate fallback routing:
- `X-Routed-Worker: openai-fallback`
- `X-Routed-Model-Group: fallback`

Body (normalized universal schema):
```json
{
  "status": "SUCCESS",
  "response": "Silent pathways guide,\nPackets dance from node to node,\nWorld connected now.",
  "conversation_id": "chatcmpl-EgeM1WnFIAjDJMFA7dpEW46oB8bie",
  "duration_seconds": 1.98,
  "model": "gpt-4o-mini",
  "source": "openai-fallback",
  "usage": {
    "input_tokens": 15,
    "output_tokens": 68,
    "total_tokens": 83,
    "thinking_tokens": 53,
    "prompt_tokens": 15,
    "completion_tokens": 68
  }
}
```

---

## Routing & Quota Algorithm

When a request arrives at `/router/chat`:

1. **Model Group Identification**:
   - Models beginning with `gemini` map to the `Gemini Models` bucket.
   - Claude and all other models map to the `Claude and GPT models` bucket.
2. **Parallel Quota Query**:
   The router queries `/limits` across all configured workers simultaneously with a 4-second timeout.
3. **Hard Filter**:
   Any worker with a weekly quota remaining fraction `<= 0.001` (0%) is excluded.
4. **Ranking & Load Distribution**:
   Quotas reach the router with a delay, so ranking by quota alone sends concurrent calls to the same worker. The router process therefore keeps, in memory and under one lock, the number of calls in flight per worker. Among eligible workers it picks by: fewer calls in flight → higher 5-hour quota (1% tiers) → random. The in-flight count is released when the call ends, whatever the outcome (success, error, timeout). Nothing is stored on disk: after a restart the counters start empty.
5. **Failover Execution**:
   The router forwards the call to the picked worker. If that worker experiences a connection failure, network timeout, HTTP 5xx server error, or HTTP 429 quota error, the router picks again by the same order among the workers not yet tried.
6. **OpenAI-Compatible Fallback**:
   If no workers have sufficient weekly quota, all workers are offline, or every candidate in the failover chain returns an error, the router transparently executes the prompt via the configured fallback API provider. The fallback uses `fallback.reasoning_effort` and `fallback.timeout` from config, whatever the request says.
7. **Time budget**:
   The request's `timeout` (default `300` s) bounds each worker attempt; workers are tried one after another. The fallback then gets `fallback.timeout` in full. Worst case of one `/router/chat` call: `4 s (limits poll) + eligible_workers × timeout + fallback.timeout`; with 5 workers, `timeout: 600` and `fallback.timeout: 600` that is 4 + 3000 + 600 = 3604 s. A client's HTTP timeout must be at least that sum.

---

## Testing

Run the comprehensive test suite (72 standard library unittest tests covering YAML configuration parsing, quota tracking, worker ranking, failover handling, fallback mocking, and Unicode safety):

```bash
venv/bin/python test_all.py -v
```

Validate your live configuration at any time:
```bash
venv/bin/python check_config.py
```

---

## Updating

To pull the latest updates and restart the service:

```bash
cd ~/GitHub/llm-router
git pull
venv/bin/pip install -r requirements.txt
sudo systemctl restart llm-router
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.

---

## IPv4 only for agy ("User location is not supported")

Google decides the caller's country from the source IP with its own geolocation, and for some datacenter IPv6
ranges it is wrong: on OVH hosts in Germany and France the IPv6 address was seen as RU and IR, so every model call
failed with `FAILED_PRECONDITION: User location is not supported` while `/limits` still worked (the quota endpoint
does not check the country). The same hosts are located correctly over IPv4. The worker therefore always starts agy
with `GODEBUG=netdns=cgo` (agy is a Go binary; use the glibc resolver) and `RES_OPTIONS=no-aaaa` (glibc >= 2.36
returns no IPv6 addresses), so agy connects over IPv4 only on every host, however the service is started
(`executor.py`, `AGY_IPV4_ONLY_ENV`). Only the agy child process is affected; the router itself and the rest of the
host keep IPv6. An IPv6-only host cannot run agy this way. Reports of the
same failure: https://discuss.ai.google.dev/t/gemini-models-fail-with-user-location-is-not-supported/176552
