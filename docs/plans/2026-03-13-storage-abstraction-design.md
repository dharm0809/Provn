# Storage Abstraction Layer Design

## Problem

The gateway's dual-write pattern (Walacor + WAL) is duplicated across 12 call sites in 3 files. Each site repeats the same `if ctx.walacor_client: try/except` + `if ctx.wal_writer: try/except` structure. This creates maintenance risk (a fix in one site can be missed in another), makes adding future backends difficult, and leaks backend-specific concerns (field filtering, serialization) into the pipeline.

## Goal

Replace the scattered dual-write logic with a single `StorageRouter` that fans out writes to pluggable `StorageBackend` implementations. One call site per write type instead of two-per-backend-per-site.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Record types | Fixed: execution, attempt, tool_event | Well-established types; generics would lose type safety |
| Backend priority | All equal, independent fan-out | Matches current behavior; WAL is inherently more reliable |
| Error contract | Fire-and-forget for attempts/tool_events; `WriteResult` for executions | Executions are critical for audit; others are best-effort |
| Delivery worker | Stays separate | Async reconciliation is a different concern from request-path writes |

## Architecture

### StorageBackend Protocol

```python
@runtime_checkable
class StorageBackend(Protocol):
    @property
    def name(self) -> str: ...
    async def write_execution(self, record: dict) -> bool: ...
    async def write_attempt(self, record: dict) -> None: ...
    async def write_tool_event(self, record: dict) -> None: ...
    async def close(self) -> None: ...
```

- `write_execution` returns `bool` (success/failure). Others return `None`.
- Each backend handles its own field mapping, serialization, and error swallowing internally.
- All methods are `async`.

### StorageRouter

```python
@dataclass(frozen=True)
class WriteResult:
    succeeded: list[str]   # backend names
    failed: list[str]      # backend names

class StorageRouter:
    def __init__(self, backends: list[StorageBackend]) -> None: ...
    async def write_execution(self, record: dict) -> WriteResult: ...
    async def write_attempt(self, record: dict) -> None: ...
    async def write_tool_event(self, record: dict) -> None: ...
    async def close(self) -> None: ...
```

- Sequential fan-out (not `asyncio.gather`) — deterministic ordering, negligible latency difference with 2 backends.
- Each backend's errors are isolated. One failing never blocks the other.
- `write_execution` returns `WriteResult` so callers can react (metrics/alerting) or ignore.
- `write_attempt` and `write_tool_event` are fire-and-forget.

### Backend Implementations

**WALBackend** — wraps `WALWriter`. Thin delegation, sync SQLite calls inside async methods.

**WalacorBackend** — wraps `WalacorClient`. Field filtering (`_EXECUTION_SCHEMA_FIELDS`, `_TOOL_EVENT_SCHEMA_FIELDS`) stays in `WalacorClient`.

Both are thin wrappers. No logic rewrite of the underlying classes.

### PipelineContext

```python
storage: StorageRouter | None = None

# Kept for DeliveryWorker, health checks, lineage reader:
wal_writer: WALWriter | None = None
walacor_client: WalacorClient | None = None
```

### Startup

```python
backends = []
if ctx.wal_writer:
    backends.append(WALBackend(ctx.wal_writer))
if ctx.walacor_client:
    backends.append(WalacorBackend(ctx.walacor_client))
ctx.storage = StorageRouter(backends)
```

WAL listed first — local writes complete before remote.

### Call Site Replacement

12 scattered write sites (6 Walacor + 6 WAL) across `orchestrator.py`, `completeness.py`, and `main.py` collapse to:

```python
# Execution (4 sites in orchestrator)
result = await ctx.storage.write_execution(record)
if result.succeeded:
    execution_id_var.set(eid)

# Attempt (1 site in completeness middleware)
await ctx.storage.write_attempt({...})

# Tool event (1 site in orchestrator)
await ctx.storage.write_tool_event(record)
```

## File Layout

### New files

```
src/gateway/storage/
    __init__.py           # exports StorageBackend, StorageRouter, WriteResult
    backend.py            # StorageBackend protocol
    router.py             # StorageRouter + WriteResult
    wal_backend.py        # WALBackend
    walacor_backend.py    # WalacorBackend
```

### Modified files

```
src/gateway/pipeline/context.py       # add storage field
src/gateway/main.py                   # init StorageRouter
src/gateway/pipeline/orchestrator.py  # replace 8 dual-write sites
src/gateway/middleware/completeness.py # replace 2 dual-write sites
```

## What Does NOT Change

- `WALWriter` and `WalacorClient` — untouched, still work the same
- `DeliveryWorker` — still reads from `ctx.wal_writer` directly
- `LineageReader` — still reads from WAL db directly
- Health checks — still read `ctx.wal_writer.pending_count()` etc.
- `main.py` self-test — still uses `ctx.wal_writer` directly

## Testing

`tests/unit/test_storage_router.py`:

- `test_write_execution_fan_out_both_succeed`
- `test_write_execution_one_fails`
- `test_write_execution_all_fail`
- `test_write_attempt_fire_and_forget`
- `test_write_tool_event_fire_and_forget`
- `test_close_all_backends`
- `test_empty_backends_list`

No tests for WALBackend/WalacorBackend directly — thin wrappers over already-tested writers. Router is where the logic lives.
