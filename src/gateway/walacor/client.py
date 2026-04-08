"""Async Walacor backend client: authenticate, write execution records and gateway attempts."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from gateway.core.models.execution import ExecutionRecord

logger = logging.getLogger(__name__)

# Refresh JWT this many seconds before expiry to avoid 401 on the first request after expiry.
_REFRESH_LEAD_SECONDS = 300  # 5 minutes
# When JWT has no exp, refresh at this interval (seconds).
_FALLBACK_REFRESH_INTERVAL = 3000  # 50 minutes
_MAX_SLEEP_SECONDS = 3600  # cap single sleep so we don't oversleep on clock skew

# Walacor system envelope type for schema management — not used at runtime,
# but documents where ETId=50 comes from (SystemEnvelopeType.Schema).
_SYSTEM_ETID_SCHEMA = 50


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
    ) -> None:
        self._server = server.rstrip("/")
        self._username = username
        self._password = password
        self._executions_etid = executions_etid
        self._attempts_etid = attempts_etid
        self._tool_events_etid = tool_events_etid
        self._token: str | None = None
        self._http: httpx.AsyncClient | None = None
        self._auth_lock: asyncio.Lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None
        self._closed = False

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Create HTTP client and authenticate. Must be called before any writes."""
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        await self._authenticate()
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        logger.info("WalacorClient ready server=%s executions_etid=%d attempts_etid=%d tool_events_etid=%d",
                    self._server, self._executions_etid, self._attempts_etid, self._tool_events_etid)

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

    # Fields defined in the Walacor gateway_executions schema (ETId 9000011).
    # Records with unknown fields are silently rejected (HTTP 200 + success:false).
    _EXECUTION_SCHEMA_FIELDS = frozenset({
        "execution_id", "model_attestation_id", "model_id", "provider",
        "policy_version", "policy_result", "tenant_id", "gateway_id",
        "timestamp", "user", "session_id", "metadata_json", "prompt_text",
        "response_content", "provider_request_id", "model_hash",
        "thinking_content", "latency_ms", "prompt_tokens", "completion_tokens",
        "total_tokens", "cache_hit", "cached_tokens", "cache_creation_tokens",
        "retry_of", "variant_id",
        # NOTE: sequence_number, record_hash, previous_record_hash are in the
        # local WAL but NOT in the Walacor sandbox schema (ETId 9000011).
        # Adding them here causes Walacor to reject writes with
        # "This field is not defined in schema". Keep them WAL-only until
        # the Walacor admin adds these columns to the schema.
    })

    async def write_execution(self, record: ExecutionRecord | dict[str, Any]) -> None:
        """Persist one execution record to Walacor (ETId=walacor_executions_etid).

        Accepts ExecutionRecord or a dict (gateway builds dicts without prompt_hash/response_hash;
        backend hashes from prompt_text/response_content). The ``metadata`` dict is serialised
        to ``metadata_json``. None-valued fields are omitted.  Fields not in the Walacor
        schema are stripped to avoid silent rejection.
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
        # Preserve chain fields inside metadata (Walacor schema doesn't have top-level columns)
        for _chain_key in ("sequence_number", "record_hash", "previous_record_hash"):
            if data.get(_chain_key) is not None:
                if meta is None:
                    meta = {}
                meta[_chain_key] = data[_chain_key]
        # Store file_metadata inside metadata (no separate Walacor schema field needed)
        if fm and meta:
            meta["file_metadata"] = fm
        elif fm:
            meta = {"file_metadata": fm}
        if meta:
            # Strip bulky OpenWebUI-injected fields that bloat metadata_json
            for _strip_key in ("features", "tool_ids", "files", "variables",
                               "params", "knowledge", "citations"):
                meta.pop(_strip_key, None)
            raw = json.dumps(meta)
            # Walacor schema field limit — truncate to prevent write rejection
            if len(raw) > 4000:
                # Keep essential audit fields, drop the rest
                _keep = {"session_id", "prompt_id", "client_context", "request_type",
                         "user", "identity_source", "walacor_audit", "_intent",
                         "_intent_confidence", "_intent_tier", "_intent_reason",
                         "chat_id", "user_email", "user_name"}
                meta = {k: v for k, v in meta.items() if k in _keep}
                raw = json.dumps(meta)
            data["metadata_json"] = raw
        # Strip fields not in the Walacor schema (timings, cache_hit, etc.)
        data = {k: v for k, v in data.items()
                if v is not None and k in self._EXECUTION_SCHEMA_FIELDS}
        eid = data.get("execution_id", "?")
        try:
            await self._submit(self._executions_etid, [data])
            logger.debug("Walacor write_execution execution_id=%s", eid)
        except Exception as e:
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
        try:
            await self._submit(self._attempts_etid, [record])
            logger.debug(
                "Walacor write_attempt request_id=%s disposition=%s",
                request_id, disposition,
            )
        except Exception as e:
            logger.warning(
                "Walacor write_attempt failed request_id=%s: %s",
                request_id, e,
            )
            # Swallow — attempt records are best-effort

    # Fields defined in the Walacor gateway_tool_events schema (ETId 9000013).
    _TOOL_EVENT_SCHEMA_FIELDS = frozenset({
        "event_id", "execution_id", "session_id", "tenant_id", "gateway_id",
        "timestamp", "tool_name", "tool_type", "tool_source", "input_data",
        "input_hash", "output_data", "output_hash", "duration_ms", "iteration",
        "is_error", "content_analysis", "sources", "metadata_json",
    })

    async def write_tool_event(self, record: dict[str, Any]) -> None:
        """Persist one tool event record to Walacor (ETId=walacor_tool_events_etid).

        Best-effort — failures are logged as warnings and swallowed so tool event
        auditing never blocks the response path.
        """
        data = dict(record)
        from gateway.classifier.schema import validate_tool_event
        data = validate_tool_event(data)
        # Field mapping: gateway uses "source", schema uses "tool_source"
        if "source" in data:
            data["tool_source"] = data.pop("source")
        # Serialise dict/list fields to JSON strings
        for key in ("input_data", "sources", "content_analysis"):
            if key in data and isinstance(data[key], (dict, list)):
                data[key] = json.dumps(data[key], default=str)
        # Drop prompt_id and other fields not in schema
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
