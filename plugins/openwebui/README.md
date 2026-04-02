# Walacor Gateway Event Logger for OpenWebUI

A single unified filter plugin that intercepts **every event** in OpenWebUI
and sends it to the Walacor Gateway for logging.

## What It Captures

| Hook | When | What |
|------|------|------|
| `inlet` | Before LLM call | User message, model, files, metadata, all messages |
| `stream` | Per SSE chunk | Streaming content deltas (buffered in memory) |
| `outlet` | After LLM response | Full response, governance headers, stream chunks |

Every event is POSTed to `POST /v1/openwebui/events` on the gateway, which
appends it as a JSONL line to a text file.

## Install

1. Upload `walacor_event_logger.py` as a **Filter Function** in
   OpenWebUI Admin > Functions
2. Enable **Global** toggle so it applies to all models
3. Set environment variables (or edit Valves in the admin UI):
   - `WALACOR_GATEWAY_URL` -- Gateway base URL (default: `http://gateway:8000`)
   - `WALACOR_GATEWAY_API_KEY` -- Gateway API key

## Gateway Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/openwebui/events` | Receive an event and append to log file |
| `GET`  | `/v1/openwebui/events` | Read logged events (supports `?limit=`, `?type=`, `?chat_id=`) |

The log file is stored at `{WAL_PATH}/openwebui_events.log` (or `/tmp/walacor_openwebui_events.log` if no WAL path is configured).

## Configuration (Valves)

| Valve | Default | Description |
|-------|---------|-------------|
| `priority` | 0 | Filter execution order (lower = first) |
| `gateway_url` | `http://gateway:8000` | Gateway base URL |
| `gateway_api_key` | `""` | API key for authentication |
| `enabled` | `true` | Enable/disable event logging |
| `log_stream_chunks` | `true` | Include stream chunks in outlet event |
| `max_response_chars` | 5000 | Max response chars to log |
| `max_user_message_chars` | 2000 | Max user message chars to log |