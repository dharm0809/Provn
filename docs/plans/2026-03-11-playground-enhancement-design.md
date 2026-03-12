# Playground Enhancement Design

**Date**: 2026-03-11
**Scope**: Enhanced Playground for admin/developer governance testing (Direction A)

## Context

The Playground is a governance-aware prompt testing tool embedded in the Lineage Dashboard. It currently works but has three functional gaps that make testing awkward: no streaming, no multi-turn, and no session/user headers.

## What We're Building

### 1. Streaming Response

- Send `stream: true` in request body
- Read SSE via `ReadableStream` + `TextDecoder`, parse `data: {...}` lines
- Append `delta.content` tokens to response pane in real-time
- Governance headers available on initial response (before body) — show immediately
- Full execution record fetch after stream completes
- Comparison mode: both streams run concurrently via `Promise.all`

### 2. Multi-Turn Conversation

- Accumulate `messages` array in component state
- Each send appends `{role: "user"}`, sends full array, appends `{role: "assistant"}` after stream completes
- UI shows conversation as a simple message list above the input
- "Clear Conversation" button resets the array
- System prompt prepended on every request (not stored in messages array)
- Comparison mode: each model gets independent message history

### 3. Session & User Headers

- Generate `sessionId` (UUID) on mount and on "Clear Conversation"
- Send `X-Session-ID` on every request — all turns share one Merkle chain
- Reuse Control tab's `sessionStorage.cp_api_key` — send as `X-API-Key`
- Add `X-User-Id` text field in Playground settings (default: `playground-user`)

## What We're NOT Doing

- No landing page change — Overview stays default
- No conversation history/persistence — clears on navigation
- No end-user auth mode — reuse existing API key from Control tab
- No conversation list UI
