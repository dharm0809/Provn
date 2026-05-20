"""Async Walacor backend client: authenticate, write execution records and gateway attempts."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import httpx

from gateway.core.models.execution import ExecutionRecord
from gateway.util.errors import classify_exception
from gateway.util.time import iso8601_utc as _iso8601

logger = logging.getLogger(__name__)

# Refresh JWT this many seconds before expiry to avoid 401 on the first request after expiry.
_REFRESH_LEAD_SECONDS = 300  # 5 minutes
# When JWT has no exp, refresh at this interval (seconds).
_FALLBACK_REFRESH_INTERVAL = 3000  # 50 minutes
_MAX_SLEEP_SECONDS = 3600  # cap single sleep so we don't oversleep on clock skew

# Walacor system envelope type for schema management — not used at runtime,
# but documents where ETId=50 comes from (SystemEnvelopeType.Schema).
_SYSTEM_ETID_SCHEMA = 50

# Walacor `metadata_json` is declared as TEXT(65535) in
# scripts/setup_walacor_schemas.py (gateway_executions.metadata_json). We cap
# below that to leave headroom for JSON escape inflation and any backend-side
# wrappers. The previous limit (4000) was arbitrarily low and silently dropped
# audit-critical fields (analyzer_decisions, walacor_audit, ...) on long
# prompts. We now leave ~5KB of safety margin before the hard 65535 cap.
_METADATA_JSON_MAX_BYTES = 60_000

# Walacor tool_events `input_data`/`output_data`/`sources` are TEXT(65535).
# Cap each individually with headroom so a single oversized blob can't reject
# the whole record on submit.
_TOOL_EVENT_BLOB_MAX_BYTES = 60_000

# Internal classifier / pipeline keys that are useful for debugging the gateway
# but are NOT auditable content. They get rehomed under metadata._internal so
# audit dashboards can filter cleanly. Any key starting with "_" is treated as
# internal (see _split_internal_keys). The explicit list below documents the
# non-underscore-prefixed keys that should also be considered internal.
#
# Convention: any metadata key that starts with `_` is gateway-internal and
# lives under `metadata._internal` on the Walacor side. Read-side consumers
# (lineage dashboard, audit exports) should hide / collapse this namespace.
_EXPLICIT_INTERNAL_KEYS = frozenset({
    "_translated_from_openai",
    "schema_mapper_confidence",
    "schema_mapper_mapped",
    "schema_mapper_unmapped",
    "schema_mapper_overflow_keys",
    "schema_mapper_timing",
    "schema_mapper_citations",
})


def _parse_jwt_exp(token: str) -> datetime | None:
    """Extract exp (expiration) from a JWT. Returns UTC datetime or None if missing/invalid."""
    if not token:
        return None
    jwt = token.removeprefix("Bearer ").strip()
    parts = jwt.split(".")
    if len(parts) != 3:
        return None
    try:
        payload_b64 = parts[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (ValueError, KeyError, TypeError):
        return None
    exp = payload.get("exp")
    if exp is None:
        return None
    try:
        return datetime.fromtimestamp(int(exp), tz=timezone.utc)
    except (ValueError, OSError):
        return None


def _next_refresh_delay_seconds(token: str | None) -> float:
    """Seconds to sleep before the next proactive refresh. Uses exp from JWT or fallback."""
    exp = _parse_jwt_exp(token or "")
    now = datetime.now(timezone.utc)
    if exp is not None:
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp > now:
            delay = (exp - now).total_seconds() - _REFRESH_LEAD_SECONDS
            return max(0.0, min(delay, _MAX_SLEEP_SECONDS))
    return float(_FALLBACK_REFRESH_INTERVAL)


def _split_internal_keys(meta: dict[str, Any]) -> dict[str, Any]:
    """Move gateway-internal classifier / pipeline keys under ``_internal``.

    Convention (documented at module top): any metadata key starting with ``_``
    is gateway-internal, plus the explicit ``_EXPLICIT_INTERNAL_KEYS`` set.
    These keys leak gateway implementation detail into auditor-visible
    metadata (intent classifier internals, schema mapper bookkeeping, OpenAI
    body translation flags). Bucketing them keeps the audit surface clean
    while preserving every byte the WAL retained — investigators can still
    reach the internals via ``metadata._internal.*`` if they need them.

    Idempotent: an already-namespaced ``_internal`` dict on input is merged
    into the output rather than overwritten or re-bucketed.

    Returns a new dict; does not mutate the input.
    """
    if not isinstance(meta, dict):
        return meta
    rehomed: dict[str, Any] = {}
    existing_internal = meta.get("_internal") if isinstance(meta.get("_internal"), dict) else {}
    internal: dict[str, Any] = dict(existing_internal)
    for key, value in meta.items():
        if key == "_internal":
            continue
        if key.startswith("_") or key in _EXPLICIT_INTERNAL_KEYS:
            internal[key] = value
        else:
            rehomed[key] = value
    if internal:
        rehomed["_internal"] = internal
    return rehomed


def _truncate_metadata_json(
    meta: dict[str, Any],
    *,
    keep: frozenset[str],
    max_bytes: int = _METADATA_JSON_MAX_BYTES,
) -> tuple[str, list[str]]:
    """Serialise ``meta`` to JSON; if it exceeds ``max_bytes`` keep only ``keep``.

    Returns the serialised string and a list of top-level keys that were
    dropped during truncation (empty when no truncation happened). Callers
    are expected to write the dropped key list back into the record under a
    ``metadata_truncated_keys`` field so investigators know the Walacor copy
    is a strict subset of the WAL copy.

    The ``_internal`` bucket is always droppable on truncation — it's
    gateway-internal by definition and not part of the audit surface.
    """
    raw = json.dumps(meta, default=str)
    if len(raw) <= max_bytes:
        return raw, []
    pruned = {k: v for k, v in meta.items() if k in keep}
    pruned_raw = json.dumps(pruned, default=str)
    dropped = sorted(k for k in meta.keys() if k not in keep)
    return pruned_raw, dropped


class WalacorClient:
    """Async HTTP client for writing execution records and gateway attempts to Walacor.

    Two Walacor schemas are required (created once, not by this class):
      - ETId=walacor_executions_etid  (default 9000001)  → walacor_gw_executions
      - ETId=walacor_attempts_etid    (default 9000002)  → walacor_gw_attempts

    Authentication uses username/password → JWT Bearer token with automatic
    re-authentication on 401 and proactive refresh before expiry.
    """

    def __init__(
        self,
        server: str,
        username: str,
        password: str,
        executions_etid: int = 9000001,
        attempts_etid: int = 9000002,
        tool_events_etid: int = 9000003,
        lifecycle_events_etid: int = 9000024,
    ) -> None:
        self._server = server.rstrip("/")
        self._username = username
        self._password = password
        self._executions_etid = executions_etid
        self._attempts_etid = attempts_etid
        self._tool_events_etid = tool_events_etid
        self._lifecycle_events_etid = lifecycle_events_etid
        self._token: str | None = None
        self._http: httpx.AsyncClient | None = None
        self._auth_lock: asyncio.Lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None
        self._closed = False
        # Bounded deque of recent delivery outcomes for the /v1/connections
        # endpoint. Entries are (ts, op, ok, detail).
        self._delivery_log: deque = deque(maxlen=100)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Create HTTP client and authenticate. Must be called before any writes."""
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        await self._authenticate()
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        logger.info(
            "WalacorClient ready server=%s executions_etid=%d attempts_etid=%d tool_events_etid=%d lifecycle_events_etid=%d",
            self._server, self._executions_etid, self._attempts_etid, self._tool_events_etid,
            self._lifecycle_events_etid,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client and stop the refresh task."""
        self._closed = True
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
            self._refresh_task = None
        if self._http:
            await self._http.aclose()
            self._http = None

    # ── Auth ─────────────────────────────────────────────────────────────────

    async def _authenticate(self) -> None:
        assert self._http is not None, "call start() first"
        async with self._auth_lock:
            resp = await self._http.post(
                f"{self._server}/auth/login",
                json={"userName": self._username, "password": self._password},
            )
            resp.raise_for_status()
            self._token = resp.json()["api_token"]  # "Bearer <jwt>"

    async def _refresh_loop(self) -> None:
        """Background task: refresh JWT before expiry to avoid 401 latency spike."""
        while not self._closed and self._http is not None:
            delay = _next_refresh_delay_seconds(self._token)
            await asyncio.sleep(delay)
            if self._closed or self._http is None:
                break
            try:
                await self._authenticate()
                logger.debug("WalacorClient JWT refreshed proactively")
            except Exception as e:
                logger.warning("WalacorClient proactive JWT refresh failed: %s", e)
                await asyncio.sleep(60.0)

    def _headers(self, etid: int) -> dict[str, str]:
        return {
            "Authorization": self._token or "",
            "Content-Type": "application/json",
            "ETId": str(etid),
        }

    # ── Delivery instrumentation ─────────────────────────────────────────────

    def _record_delivery(self, op: str, *, ok: bool, detail: str | None) -> None:
        self._delivery_log.append((time.time(), op, ok, detail))

    def delivery_snapshot(self) -> dict:
        now = time.time()
        recent = [e for e in self._delivery_log if now - e[0] <= 60.0]
        if not recent:
            return {
                "success_rate_60s": 1.0,
                "last_failure": None,
                "last_success_ts": None,
                "time_since_last_success_s": None,
            }
        oks = [e for e in recent if e[2]]
        last_success = max((e[0] for e in self._delivery_log if e[2]), default=None)
        last_failure_entry = next(
            ((e[0], e[1], e[3]) for e in reversed(recent) if not e[2]),
            None,
        )
        return {
            "success_rate_60s": len(oks) / len(recent),
            "last_failure": {
                "ts": _iso8601(last_failure_entry[0]),
                "op": last_failure_entry[1],
                "detail": last_failure_entry[2],
            } if last_failure_entry else None,
            "last_success_ts": _iso8601(last_success) if last_success else None,
            "time_since_last_success_s": (now - last_success) if last_success else None,
        }

    # ── Core submit ──────────────────────────────────────────────────────────

    async def _submit(self, etid: int, records: list[dict[str, Any]]) -> None:
        """POST records to /envelopes/submit; re-authenticates once on 401.

        Walacor returns HTTP 200 even on validation errors (unknown fields, type
        mismatches) with ``{"success": false, "error": ...}``.  We check the body
        and raise so callers see the failure.
        """
        assert self._http is not None
        for attempt in range(2):
            resp = await self._http.post(
                f"{self._server}/envelopes/submit",
                json={"Data": records},
                headers=self._headers(etid),
            )
            if resp.status_code == 401 and attempt == 0:
                logger.debug("WalacorClient token expired — re-authenticating")
                await self._authenticate()
                continue
            resp.raise_for_status()
            # Walacor may return 200 with success=false for schema validation errors
            try:
                body = resp.json()
                if isinstance(body, dict) and body.get("success") is False:
                    err_detail = body.get("error", {})
                    raise RuntimeError(
                        f"Walacor submit rejected ETId={etid}: {err_detail}"
                    )
            except (ValueError, KeyError):
                pass  # Non-JSON or unexpected shape — treat as success
            return

    # ── Public write methods ─────────────────────────────────────────────────

    # Fields defined in the Walacor gateway_executions schema (created by
    # scripts/setup_walacor_schemas.py with ETId 9000031). Walacor rejects
    # records with unknown top-level fields silently (HTTP 200 +
    # ``success: false``). Anything *not* in this set that still needs to
    # survive the dual-write contract is serialised into ``metadata_json``
    # below — that's the documented extension point.
    _EXECUTION_SCHEMA_FIELDS = frozenset({
        # Core identity
        "execution_id", "model_attestation_id", "model_id", "provider",
        "tenant_id", "gateway_id", "timestamp", "user", "session_id",
        # Policy
        "policy_version", "policy_result",
        # Content
        "prompt_text", "response_content", "thinking_content", "metadata_json",
        # Provider details
        "provider_request_id", "model_hash",
        # Token usage
        "prompt_tokens", "completion_tokens", "total_tokens",
        "cached_tokens", "cache_creation_tokens", "cache_hit", "latency_ms",
        # Session chain — ID-pointer chain (new) + Merkle hash chain (legacy transition)
        "sequence_number", "record_id", "previous_record_id",
        "record_hash", "previous_record_hash", "record_signature",
        # Tool awareness
        "tool_strategy", "tool_count",
        # Routing
        "variant_id", "retry_of",
    })

    # Top-level execution-record keys that are *deliberately* not sent to
    # Walacor and not rehomed into metadata_json. The schema-field strip
    # below logs + audit-marks ANY other non-None key it discards, so a
    # field added for a new dashboard panel (the way ``timings`` was lost
    # for months — visible locally via LineageReader, silently dropped on
    # the Walacor path) can never again vanish without a trace. Adding a
    # new intentional drop is a conscious one-line edit here.
    #   prompt_hash / response_hash: gateway sends full text; Walacor
    #     hashes on ingest (see CLAUDE.md "Gateway does NOT compute hashes").
    #   metadata / file_metadata / timings: rehomed into metadata_json above.
    _INTENTIONAL_NON_SCHEMA_KEYS = frozenset({
        "prompt_hash", "response_hash",
        "metadata", "file_metadata", "timings",
    })

    # Audit-critical fields the orchestrator stuffs into ``metadata`` that
    # MUST survive ``metadata_json`` truncation. Anything not in this set
    # is dropped first when the serialised JSON exceeds the size cap, and
    # the dropped keys surface in ``metadata_truncated_keys`` so an auditor
    # knows the Walacor copy is a strict subset of the WAL copy.
    #
    # Categories:
    #   - core identity (session, user, request_type, walacor_audit)
    #   - safety / governance (analyzer_decisions, pii_decisions,
    #     response_policy_*, input_analysis, enforcement_mode,
    #     content_analysis)
    #   - caller identity (caller_email, caller_roles, identity_source)
    #   - audit correlation (prompt_id, client_context, received_at,
    #     delivery_error)
    #   - chain bookkeeping (sequence_number, record_hash, ...)
    #   - tool audit (tool_strategy, tool_interaction_count,
    #     tool_interactions, tool_events_detail — note: dropping
    #     tool_events_detail on truncation is acceptable because the
    #     gateway_tool_events table is the durable copy)
    #   - schema_mapper_* (under _internal — survives because we walk the
    #     namespace below; included explicitly for clarity)
    _METADATA_KEEP_FIELDS = frozenset({
        # Identity / correlation
        "session_id", "prompt_id", "request_type", "received_at",
        "client_context", "chat_id", "user_email", "user_name",
        # Caller identity (from JWT/SSO + headers)
        "caller_email", "caller_roles", "identity_source", "user",
        # Governance decisions
        "walacor_audit", "analyzer_decisions", "pii_decisions",
        "response_policy_version", "response_policy_result",
        "input_analysis", "enforcement_mode", "content_analysis",
        # Delivery / completeness
        "delivery_error",
        # Pipeline timings (for dashboard waterfall trace)
        "timings",
        # File audit
        "file_metadata",
        # Tool audit (the row-level copy lives in gateway_tool_events;
        # this is the embedded summary)
        "tool_strategy", "tool_interaction_count", "tool_interactions",
        "tool_loop_iterations", "tool_events_detail",
        # Chain bookkeeping (mirrored top-level too; kept here for
        # back-compat lineage readers)
        "sequence_number", "record_hash", "previous_record_hash",
        # Canonical schema-mapper output (provider-agnostic view of the
        # response — keep for cross-provider audit)
        "canonical",
        # Gateway-internal bucket — preserved verbatim. Anything inside
        # _internal is namespaced and dashboards collapse it on display.
        "_internal",
    })

    @classmethod
    def unexpected_execution_keys(cls, record: dict[str, Any]) -> list[str]:
        """Return non-None top-level keys that would be silently dropped.

        Same predicate used by ``write_execution`` to detect drift, but
        exposed for CI contract tests: every field the orchestrator
        constructs into an execution record MUST be in
        ``_EXECUTION_SCHEMA_FIELDS`` OR ``_INTENTIONAL_NON_SCHEMA_KEYS``,
        otherwise it vanishes only on the Walacor read path and stays
        visible locally — the exact prod-only regression class that lost
        ``timings`` for months. See CLAUDE.md "Failure modes & guards".
        """
        return sorted(
            k for k, v in record.items()
            if v is not None
            and k not in cls._EXECUTION_SCHEMA_FIELDS
            and k not in cls._INTENTIONAL_NON_SCHEMA_KEYS
        )

    async def write_execution(self, record: ExecutionRecord | dict[str, Any]) -> None:
        """Persist one execution record to Walacor (ETId=walacor_executions_etid).

        Accepts ExecutionRecord or a dict (gateway builds dicts without prompt_hash/response_hash;
        backend hashes from prompt_text/response_content). The ``metadata`` dict is serialised
        to ``metadata_json``. None-valued fields are omitted.  Fields not in the Walacor
        schema are stripped to avoid silent rejection.

        Dual-write fidelity: the orchestrator writes the full ``metadata`` dict
        to the WAL verbatim. This method MUST preserve every audit-critical
        key on the Walacor side or record the loss in
        ``metadata_truncated_keys`` so reviewers can detect divergence.
        """
        if isinstance(record, dict):
            data = dict(record)
        else:
            data = record.model_dump(mode="json")
        # Schema validation: ensure all fields have correct types before write
        from gateway.classifier.schema import validate_execution
        data = validate_execution(data)
        meta = data.pop("metadata", None)
        fm = data.pop("file_metadata", None)
        # Timings are not in the Walacor schema but are needed for the dashboard
        # Pipeline Trace waterfall. Rehome them into metadata_json (the documented
        # extension point) so they survive the round-trip via WalacorLineageReader.
        timings = data.pop("timings", None)
        if timings:
            if meta is not None:
                meta["timings"] = timings
            else:
                meta = {"timings": timings}
        # Extract tool_strategy and tool_count from metadata to top-level fields
        if meta:
            if meta.get("tool_strategy") and "tool_strategy" not in data:
                data["tool_strategy"] = meta["tool_strategy"]
            if meta.get("tool_interaction_count") and "tool_count" not in data:
                data["tool_count"] = meta["tool_interaction_count"]
        # Store file_metadata inside metadata (no separate Walacor schema field needed)
        if fm and meta:
            meta["file_metadata"] = fm
        elif fm:
            meta = {"file_metadata": fm}
        if meta:
            # Strip bulky OpenWebUI-injected fields that bloat metadata_json
            # without contributing audit value. These come from the
            # OpenWebUI plugin body and are duplicated elsewhere (citations
            # live on canonical.citations, file content lives on
            # file_metadata, etc).
            for _strip_key in ("features", "tool_ids", "files", "variables",
                               "params", "knowledge", "citations"):
                meta.pop(_strip_key, None)
            # Rehome gateway-internal classifier / pipeline keys under
            # ``_internal`` so the audit surface stays clean.
            meta = _split_internal_keys(meta)
            # Serialise; if too big, drop everything that isn't audit-critical
            # and record which keys we dropped so investigators can correlate
            # against the WAL. Read ``_METADATA_JSON_MAX_BYTES`` from the
            # module (not the function default) so test/operator overrides
            # via monkeypatch / module attribute are honoured.
            import gateway.walacor.client as _mod
            raw, dropped = _truncate_metadata_json(
                meta,
                keep=self._METADATA_KEEP_FIELDS,
                max_bytes=_mod._METADATA_JSON_MAX_BYTES,
            )
            if dropped:
                logger.warning(
                    "metadata_json truncated execution_id=%s dropped_keys=%s",
                    data.get("execution_id", "?"), dropped,
                )
                # Re-add the marker to the pruned meta + re-serialise so the
                # truncation evidence rides along inside metadata_json.
                pruned = json.loads(raw)
                pruned["metadata_truncated_keys"] = dropped
                raw = json.dumps(pruned, default=str)
            data["metadata_json"] = raw
        # Strip fields not in the Walacor schema. Before discarding, detect
        # any non-None key that is being lost *unexpectedly* — i.e. not in
        # the schema AND not on the intentional-drop list. This is the
        # self-revealing guard for the ``timings`` class of bug: a field
        # added to the execution record for a new dashboard panel that
        # nobody added to the allowlist would otherwise vanish only on the
        # Walacor read path (LineageReader still has it locally), making it
        # a prod-only, test-invisible regression. Now it screams in the
        # logs and embeds forensic evidence in the record itself.
        unexpected_drops = sorted(
            k for k, v in data.items()
            if v is not None
            and k not in self._EXECUTION_SCHEMA_FIELDS
            and k not in self._INTENTIONAL_NON_SCHEMA_KEYS
        )
        eid = data.get("execution_id", "?")
        if unexpected_drops:
            logger.warning(
                "Walacor schema strip dropped UNEXPECTED non-None fields "
                "execution_id=%s keys=%s — add to _EXECUTION_SCHEMA_FIELDS "
                "(if Walacor should store it), rehome into metadata_json, or "
                "add to _INTENTIONAL_NON_SCHEMA_KEYS. Until then this field "
                "is LOST on the Walacor read path.",
                eid, unexpected_drops,
            )
            # Embed audit evidence inside metadata_json so the loss is
            # discoverable by querying the record, not just by catching the
            # log line at write time. Mirrors metadata_truncated_keys.
            try:
                _mj = json.loads(data["metadata_json"]) if data.get("metadata_json") else {}
                _mj["schema_stripped_keys"] = unexpected_drops
                data["metadata_json"] = json.dumps(_mj, default=str)
            except (json.JSONDecodeError, TypeError, KeyError):
                pass
        data = {k: v for k, v in data.items()
                if v is not None and k in self._EXECUTION_SCHEMA_FIELDS}
        try:
            await self._submit(self._executions_etid, [data])
            self._record_delivery("write_execution", ok=True, detail=None)
            logger.debug("Walacor write_execution execution_id=%s", eid)
        except Exception as e:
            self._record_delivery("write_execution", ok=False, detail=classify_exception(e))
            logger.error(
                "Walacor write_execution failed execution_id=%s: %s",
                eid, e,
            )
            raise

    async def write_attempt(
        self,
        request_id: str,
        tenant_id: str,
        path: str,
        disposition: str,
        status_code: int,
        provider: str | None = None,
        model_id: str | None = None,
        execution_id: str | None = None,
        user: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Persist one gateway_attempts row to Walacor (ETId=walacor_attempts_etid).

        Failures are logged as warnings only — attempt records are best-effort
        and must never block the response path.
        """
        record: dict[str, Any] = {
            "request_id": request_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tenant_id": tenant_id,
            "path": path,
            "disposition": disposition,
            "status_code": status_code,
        }
        if provider:
            record["provider"] = provider
        if model_id:
            record["model_id"] = model_id
        if execution_id:
            record["execution_id"] = execution_id
        if user:
            record["user"] = user
        if reason:
            record["reason"] = reason
        try:
            await self._submit(self._attempts_etid, [record])
            self._record_delivery("write_attempt", ok=True, detail=None)
            logger.debug(
                "Walacor write_attempt request_id=%s disposition=%s",
                request_id, disposition,
            )
        except Exception as e:
            self._record_delivery("write_attempt", ok=False, detail=classify_exception(e))
            logger.warning(
                "Walacor write_attempt failed request_id=%s: %s",
                request_id, e,
            )
            # Swallow — attempt records are best-effort

    # Fields defined in the Walacor gateway_tool_events schema (created by
    # scripts/setup_walacor_schemas.py with ETId 9000033). Walacor rejects
    # records with unknown top-level fields silently. Anything *not* in this
    # set that still needs to survive the dual-write contract is folded into
    # ``content_analysis`` (the only JSON-shaped LongText field with slack)
    # under a ``_extras`` key — see ``_pack_tool_event_extras`` below.
    _TOOL_EVENT_SCHEMA_FIELDS = frozenset({
        # Identity
        "event_id", "execution_id", "session_id", "tenant_id", "gateway_id",
        "prompt_id", "timestamp",
        # Tool details
        "tool_name", "tool_type", "tool_source", "mcp_server_name",
        # Input/output
        "input_data", "input_hash", "output_data", "output_hash", "sources",
        # Execution metadata
        "duration_ms", "iteration", "is_error", "content_analysis",
    })

    # Tool-event keys the orchestrator emits that have no dedicated Walacor
    # column. Without this list they were silently dropped on submit. We
    # fold them into ``content_analysis._extras`` so the Walacor copy stays
    # equivalent to the WAL copy.
    _TOOL_EVENT_EXTRA_KEYS = frozenset({
        "event_type",      # always "tool_call" today; preserved for forward-compat
        "tool_id",         # provider-assigned ID, distinct from tool_name
        "mcp_server_url",  # paired with mcp_server_name when MCP-sourced
        "client_context",  # caller IP / UA / forwarded headers
    })

    @staticmethod
    def _pack_tool_event_extras(
        data: dict[str, Any],
        *,
        extra_keys: frozenset[str],
        schema_fields: frozenset[str],
    ) -> dict[str, Any]:
        """Fold non-schema tool-event keys into ``content_analysis._extras``.

        ``data`` is mutated in place: extras (any key that is *not* a
        ``schema_fields`` member AND IS in ``extra_keys``) are popped from
        the record and re-attached under ``content_analysis``, which is the
        only JSON-shaped LongText column with slack on the tool_events
        schema. Pre-existing ``content_analysis`` (an analyzer-decision
        payload from the orchestrator) is preserved; ``_extras`` is added
        as a sibling key inside the wrapping object.
        """
        extras: dict[str, Any] = {}
        for k in list(data.keys()):
            if k in schema_fields:
                continue
            if k in extra_keys:
                extras[k] = data.pop(k)
        if not extras:
            return data
        existing = data.get("content_analysis")
        if existing is None:
            wrapper: dict[str, Any] = {}
        elif isinstance(existing, dict):
            wrapper = dict(existing)
        elif isinstance(existing, str):
            # The current orchestrator serialises content_analysis to JSON
            # before this method runs only when it was a dict; preserve
            # whatever was already serialised by re-parsing or wrapping.
            try:
                parsed = json.loads(existing)
                if isinstance(parsed, dict):
                    wrapper = parsed
                else:
                    wrapper = {"content_analysis": parsed}
            except (ValueError, TypeError):
                wrapper = {"content_analysis": existing}
        else:
            wrapper = {"content_analysis": existing}
        wrapper["_extras"] = extras
        data["content_analysis"] = wrapper
        return data

    @staticmethod
    def _cap_tool_event_blobs(
        data: dict[str, Any],
        *,
        max_bytes: int = _TOOL_EVENT_BLOB_MAX_BYTES,
    ) -> list[str]:
        """Cap oversized tool-event payloads; return the list of truncated keys.

        Walacor's ``input_data`` / ``output_data`` / ``sources`` are
        TEXT(65535). A single oversized blob (e.g. a 200KB tool response)
        would silently reject the whole record. We cap each blob and mark
        it with a sentinel so investigators can correlate against the WAL.
        """
        truncated: list[str] = []
        for key in ("input_data", "output_data", "sources"):
            value = data.get(key)
            if not isinstance(value, str):
                continue
            if len(value) <= max_bytes:
                continue
            keep = max_bytes - 200  # leave room for the sentinel suffix
            data[key] = value[:keep] + f"…[truncated {len(value) - keep} chars]"
            truncated.append(key)
        return truncated

    async def write_tool_event(self, record: dict[str, Any]) -> None:
        """Persist one tool event record to Walacor (ETId=walacor_tool_events_etid).

        Best-effort — failures are logged as warnings and swallowed so tool event
        auditing never blocks the response path.

        Dual-write fidelity: every field that ends up in the WAL must end up
        in Walacor. Schema-unsupported keys (``event_type``, ``tool_id``,
        ``mcp_server_url``, ``client_context``) are folded into
        ``content_analysis._extras``. Oversized blobs (``input_data``,
        ``output_data``, ``sources``) are capped and marked in
        ``tool_event_truncated_keys`` so reviewers know the Walacor copy is
        a strict subset.
        """
        data = dict(record)
        # Serialise dict/list LongText payloads to JSON strings BEFORE
        # validate_tool_event coerces them with bare ``str()`` (which would
        # produce Python repr — single quotes, ``None`` instead of ``null``
        # — and break downstream JSON readers). ``content_analysis`` stays
        # as a dict here on purpose so we can wrap it with ``_extras`` below
        # before final serialisation.
        for key in ("input_data", "output_data", "sources"):
            if key in data and isinstance(data[key], (dict, list)):
                data[key] = json.dumps(data[key], default=str)
        # Pull content_analysis aside so validate_tool_event's str()-coerce
        # doesn't mangle a dict into a Python repr we'd have to parse back.
        _ca_pending = data.pop("content_analysis", None)
        from gateway.classifier.schema import validate_tool_event
        data = validate_tool_event(data)
        if _ca_pending is not None:
            data["content_analysis"] = _ca_pending
        # Field mapping: gateway uses "source", schema uses "tool_source"
        if "source" in data:
            data["tool_source"] = data.pop("source")
        # Cap oversized blobs (input_data / output_data / sources) BEFORE
        # the field strip so we can attach the truncation marker. Read the
        # module-level cap (not the static default) so test/operator
        # overrides via monkeypatch are honoured.
        import gateway.walacor.client as _mod
        truncated_blobs = self._cap_tool_event_blobs(
            data, max_bytes=_mod._TOOL_EVENT_BLOB_MAX_BYTES,
        )
        # Fold non-schema keys (event_type, tool_id, mcp_server_url,
        # client_context) into content_analysis._extras so the Walacor copy
        # stays equivalent to the WAL copy.
        data = self._pack_tool_event_extras(
            data,
            extra_keys=self._TOOL_EVENT_EXTRA_KEYS,
            schema_fields=self._TOOL_EVENT_SCHEMA_FIELDS,
        )
        # Finalise content_analysis serialisation now that extras are in.
        ca = data.get("content_analysis")
        if isinstance(ca, (dict, list)):
            if truncated_blobs and isinstance(ca, dict):
                ca["tool_event_truncated_keys"] = truncated_blobs
            data["content_analysis"] = json.dumps(ca, default=str)
        elif truncated_blobs:
            # No content_analysis — surface the truncation marker by
            # creating one. Read-side parsers already handle dict-shaped
            # content_analysis.
            data["content_analysis"] = json.dumps(
                {"tool_event_truncated_keys": truncated_blobs}, default=str
            )
        # Strip fields not in the Walacor schema
        data = {k: v for k, v in data.items()
                if v is not None and k in self._TOOL_EVENT_SCHEMA_FIELDS}
        try:
            await self._submit(self._tool_events_etid, [data])
            logger.debug("Walacor write_tool_event event_id=%s", data.get("event_id", "?"))
        except Exception as e:
            logger.warning(
                "Walacor write_tool_event FAILED event_id=%s: %s",
                data.get("event_id", "?"), e,
            )

    # ── Query API ─────────────────────────────────────────────────────────

    async def query_complex(self, etid: int, pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Query Walacor via /api/query/getcomplex with a MongoDB-style aggregation pipeline.

        Returns the result list from ``response.data``.  Re-authenticates once on 401.
        """
        assert self._http is not None, "call start() first"
        url = f"{self._server}/query/getcomplex"
        for attempt in range(2):
            resp = await self._http.post(
                url,
                json=pipeline,
                headers=self._headers(etid),
            )
            if resp.status_code == 401 and attempt == 0:
                logger.debug("WalacorClient query_complex: 401 — re-authenticating")
                await self._authenticate()
                continue
            resp.raise_for_status()
            body = resp.json()
            if isinstance(body, dict):
                return body.get("data", [])
            return body if isinstance(body, list) else []
        return []

    # ── Write-time idempotency probes ────────────────────────────────────────
    #
    # The delivery worker retries any write that did not produce a clean ack.
    # That is correct for *true* failures but produces a duplicate when the
    # write actually landed and only the *ack* was lost on the return path
    # (network blip, proxy timeout, etc.) — the record_id is identical, the
    # row is already in Walacor, but the worker has no way to know and writes
    # it again. PR #61 added a read-time dedup band-aid; these probes are the
    # structural fix.
    #
    # Usage contract:
    #   The worker should only call these after a write attempt has failed
    #   in a way that *could* be a lost-ack. A True return means "Walacor
    #   already has this record_id; do NOT retry, mark delivered." A False
    #   return means "Walacor does not have it; retry is safe." On a probe
    #   error (network/auth) we conservatively return False so the worker
    #   falls back to its normal retry path — the existing read-side dedup
    #   still catches whatever duplicate that path produces.

    async def execution_exists(self, record_id: str) -> bool:
        """Return True iff an execution row with ``record_id`` is already in Walacor.

        Issues a one-row ``query_complex`` against the executions ETId. Errors
        are swallowed and return False — the caller treats False as
        "retry is safe", and the read-time dedup in ``WalacorLineageReader``
        is the belt-and-braces fallback (see PR #61).
        """
        if not record_id:
            return False
        try:
            rows = await self.query_complex(
                self._executions_etid,
                [{"$match": {"record_id": record_id}}, {"$limit": 1}],
            )
        except Exception as e:
            logger.debug(
                "execution_exists probe failed record_id=%s: %s", record_id, e
            )
            return False
        return bool(rows)

    async def tool_event_exists(self, event_id: str) -> bool:
        """Return True iff a tool_event row with ``event_id`` is already in Walacor.

        Same contract as ``execution_exists`` but keyed on the tool_events
        schema's ``event_id`` column (executions are keyed on ``record_id``;
        tool events use a distinct identifier — see ``_TOOL_EVENT_SCHEMA_FIELDS``).
        """
        if not event_id:
            return False
        try:
            rows = await self.query_complex(
                self._tool_events_etid,
                [{"$match": {"event_id": event_id}}, {"$limit": 1}],
            )
        except Exception as e:
            logger.debug(
                "tool_event_exists probe failed event_id=%s: %s", event_id, e
            )
            return False
        return bool(rows)
