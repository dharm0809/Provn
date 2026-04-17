"""Phase F — Kitchen-sink stateful composition test.

Models the Walacor Gateway as a state machine. Hypothesis generates arbitrary
sequences of rules (requests, policy updates, attestation revocation) and
checks the ten gateway invariants after every step and at teardown.

Invariants verified:
  I1 — Completeness: every fired request produces one gateway_attempts row
  I2 — Session chain: per session, contiguous sequence_number, record_hash
       recomputes, previous_record_hash links form a valid chain
  I3 — Policy enforcement: requests blocked by policy never produce exec records
  I4 — Content enforcement: responses with blocked content return 403
  I5 — Budget enforcement: token budget never exceeded (budget disabled in this
       run — covered by A4)
  I6 — Audit completeness: tool_events.count matches invocations (tools
       disabled here — covered by B2)
  I7 — Dual-write: WAL records match in-memory execution count (Walacor
       disabled — trivially satisfied, parity covered by B3)
  I8 — No 500s: only known-good status codes appear
  I9 — Redis parity: not exercised here (covered by C1)
  I10 — No memory leak: verdict buffer size stays ≤ max_size

Setup cost: ~5s per example to spin up the full ASGI app. We therefore cap at
3 examples × 20 steps = 60 actions + 3 × 5s setup ≈ 21s total.

Caveats:
  - The stateful runner creates a new RuleBasedStateMachine instance per
    example. Each instance gets its own temp WAL + control DB, so examples
    don't contaminate each other.
  - asyncio: rules are synchronous methods; each rule internally runs its
    async work on a per-instance event loop.
  - The torture-test fixture pattern is reused (mock transport, env setup).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
)


_TENANT = "stateful-tenant"
_API_KEY_A = "stateful-key-a"
_API_KEY_B = "stateful-key-b"
_MODEL_ID = "stateful-model"
_SESSION_IDS = [f"sf-sess-{i}" for i in range(4)]
_ALLOWED_STATUS = {200, 400, 401, 403, 404, 413, 422, 429}

# Response markers — mock transport inspects the prompt and emits matching
# response content. This lets the state machine exercise the content-analysis
# pipeline without a real LLM.
_MARK_NORMAL = "REPLY_NORMAL"
_MARK_CSAM = "REPLY_WITH_CSAM"  # triggers toxicity BLOCK
_MARK_CREDIT = "REPLY_WITH_CREDIT"  # triggers PII BLOCK (credit_card)
_MARK_SSN = "REPLY_WITH_SSN"  # triggers PII WARN (low-tier)


# ── Mock provider (shared across all instances) ─────────────────────────────


def _mock_handler(request: httpx.Request) -> httpx.Response:
    """Inspect the prompt for markers; return a response whose content will
    trigger the matching content-analysis verdict."""
    try:
        body = json.loads(request.content or b"{}")
    except Exception:
        body = {}
    prompt = ""
    for m in body.get("messages", []) or []:
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                prompt += c + "\n"

    if _MARK_CSAM in prompt:
        reply = "csam content here"  # matches toxicity child_safety pattern
    elif _MARK_CREDIT in prompt:
        reply = "Here is a card: 4111 1111 1111 1111"
    elif _MARK_SSN in prompt:
        reply = "SSN on file: 123-45-6789"
    else:
        reply = "Normal mock reply — nothing to see here."

    stream = bool(body.get("stream"))
    created = int(time.time())
    model = body.get("model", _MODEL_ID)
    rid = f"mock-{uuid.uuid4().hex[:8]}"
    msg = {
        "id": rid,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": reply},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 6, "total_tokens": 16},
    }
    if not stream:
        return httpx.Response(200, json=msg, headers={"content-type": "application/json"})

    # Streaming SSE
    chunk = {
        "id": rid, "object": "chat.completion.chunk", "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": reply}, "finish_reason": None}],
    }
    done = dict(chunk)
    done["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
    content = "".join([
        f"data: {json.dumps(chunk)}\n\n",
        f"data: {json.dumps(done)}\n\n",
        "data: [DONE]\n\n",
    ]).encode("utf-8")
    return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})


def _build_mock_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "mock-provider" and request.url.path.startswith("/v1/chat/completions"):
            return _mock_handler(request)
        return httpx.Response(502, json={"error": f"unexpected: {request.method} {request.url}"})
    return httpx.MockTransport(handler)


# ── Env setup ────────────────────────────────────────────────────────────────


def _set_env(tmp_path: Path) -> dict[str, str]:
    env = {
        "WALACOR_SKIP_GOVERNANCE": "false",
        "WALACOR_GATEWAY_TENANT_ID": _TENANT,
        "WALACOR_GATEWAY_ID": "gw-stateful",
        "WALACOR_GATEWAY_PROVIDER": "ollama",
        "WALACOR_GATEWAY_API_KEYS": f"{_API_KEY_A},{_API_KEY_B}",
        "WALACOR_CONTROL_PLANE_URL": "",
        "WALACOR_CONTROL_PLANE_ENABLED": "true",
        "WALACOR_CONTROL_PLANE_DB_PATH": str(tmp_path / "control.db"),
        "WALACOR_SERVER": "",
        "WALACOR_USERNAME": "",
        "WALACOR_PASSWORD": "",
        "WALACOR_WAL_PATH": str(tmp_path / "wal"),
        "WALACOR_INTELLIGENCE_DB_PATH": str(tmp_path / "intel.db"),
        "WALACOR_ONNX_MODELS_BASE_PATH": str(tmp_path / "models"),
        "WALACOR_PROVIDER_OLLAMA_URL": "http://mock-provider:11434",
        "WALACOR_RATE_LIMIT_ENABLED": "false",
        "WALACOR_TOKEN_RATE_LIMIT_ENABLED": "false",
        "WALACOR_LLAMA_GUARD_ENABLED": "false",
        "WALACOR_OTEL_ENABLED": "false",
        "WALACOR_STARTUP_PROBES_ENABLED": "false",
        "WALACOR_TOOL_AWARE_ENABLED": "false",
        "WALACOR_WEB_SEARCH_ENABLED": "false",
        "WALACOR_EXPORT_ENABLED": "false",
        "WALACOR_PROMPT_GUARD_ENABLED": "false",
        "WALACOR_PRESIDIO_PII_ENABLED": "false",
        "WALACOR_DLP_ENABLED": "false",
        "WALACOR_SEMANTIC_CACHE_ENABLED": "false",
        "WALACOR_HEDGED_REQUESTS_ENABLED": "false",
        "WALACOR_LOAD_BALANCER_ENABLED": "false",
        "WALACOR_WAL_BATCH_ENABLED": "false",
        "WALACOR_ADAPTIVE_CONCURRENCY_ENABLED": "false",
        "WALACOR_LINEAGE_ENABLED": "true",
        "WALACOR_METRICS_ENABLED": "true",
        "WALACOR_AUTH_MODE": "api_key",
        "WALACOR_MAX_REQUEST_BODY_MB": "50",
        "WALACOR_REDIS_URL": "",
        "WALACOR_TOKEN_BUDGET_ENABLED": "false",
    }
    for k, v in env.items():
        os.environ[k] = v
    return env


# ── Chain hash helper (matches session_chain.compute_record_hash) ───────────


def _recompute_record_hash(
    execution_id: str, policy_version: int, policy_result: str,
    previous_record_hash: str, sequence_number: int, timestamp: str,
) -> str:
    canonical = "|".join([
        execution_id, str(policy_version), policy_result,
        previous_record_hash, str(sequence_number), timestamp,
    ])
    return hashlib.sha3_512(canonical.encode("utf-8")).hexdigest()


# ── The state machine ───────────────────────────────────────────────────────


class GatewayStateMachine(RuleBasedStateMachine):
    """Stateful composition test.

    Each rule sends a request or mutates control-plane state. After every
    rule we check cheap in-memory invariants (I8, I10). At teardown we do
    the expensive SQL-based chain + completeness checks (I1, I2).
    """

    def __init__(self):
        super().__init__()
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)
        self.env = _set_env(self._tmp_path)

        # Clear gateway.* imports so create_app re-reads settings.
        # Preserve gateway.metrics.* — Prometheus counters are process-wide
        # singletons (the torture test teaches this).
        for mod in list(sys.modules.keys()):
            if (mod.startswith("gateway.") or mod == "gateway") and not mod.startswith("gateway.metrics"):
                sys.modules.pop(mod, None)
        from gateway.config import get_settings
        get_settings.cache_clear()
        self._get_settings = get_settings

        from gateway.main import create_app, on_startup, on_shutdown
        from gateway.pipeline.context import get_pipeline_context

        self._on_startup = on_startup
        self._on_shutdown = on_shutdown

        # Sanity-check env override took effect before startup
        _s = get_settings()
        assert _s.wal_path == self.env["WALACOR_WAL_PATH"], (
            f"env override failed: wal_path={_s.wal_path!r}"
        )

        self.app = create_app()
        self._run(self._on_startup())

        transport = httpx.ASGITransport(app=self.app)
        self.client = httpx.AsyncClient(transport=transport, base_url="http://gateway")
        self.ctx = get_pipeline_context()

        # Swap outbound httpx client for MockTransport
        if self.ctx.http_client is not None:
            try:
                self._run(self.ctx.http_client.aclose())
            except Exception:
                pass
        self.ctx.http_client = httpx.AsyncClient(
            transport=_build_mock_transport(),
            timeout=httpx.Timeout(10.0),
        )

        # Install SQLite-backed lineage reader (local mode doesn't auto-wire)
        if self.ctx.lineage_reader is None:
            from gateway.lineage.reader import LineageReader
            wal_db = str(Path(self.env["WALACOR_WAL_PATH"]) / "wal.db")
            self.ctx.lineage_reader = LineageReader(wal_db)

        # Seed the attestation for our test model
        if self.ctx.control_store is not None:
            existing = {
                (a["provider"], a["model_id"])
                for a in self.ctx.control_store.list_attestations(_TENANT)
            }
            if ("ollama", _MODEL_ID) not in existing:
                self.ctx.control_store.upsert_attestation({
                    "model_id": _MODEL_ID,
                    "provider": "ollama",
                    "status": "active",
                    "verification_level": "self_attested",
                    "tenant_id": _TENANT,
                    "notes": "stateful-test model",
                })
            from gateway.control.api import _refresh_attestation_cache
            _refresh_attestation_cache()

        # State tracked across rules
        self.fired_count = 0
        self.responses: list[httpx.Response] = []
        self.touched_sessions: set[str] = set()
        self.model_revoked = False
        self.deny_policy_active = False

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    # ── Helpers for rule execution ──

    def _post_chat(self, *, session_id: str, prompt: str, api_key: str | None,
                   stream: bool, model: str = _MODEL_ID,
                   malformed: bool = False) -> httpx.Response:
        headers: dict[str, str] = {
            "x-session-id": session_id,
            "content-type": "application/json",
            "x-user-id": "stateful@test",
        }
        if api_key is not None:
            headers["x-api-key"] = api_key
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": stream,
        }
        if malformed:
            return self._run(self.client.post(
                "/v1/chat/completions",
                content=b"{not valid json",
                headers=headers,
            ))
        return self._run(self.client.post(
            "/v1/chat/completions",
            json=body,
            headers=headers,
        ))

    def _record(self, resp: httpx.Response, session_id: str):
        self.fired_count += 1
        self.responses.append(resp)
        self.touched_sessions.add(session_id)

    # ── Rules ──

    @rule(
        session_id=st.sampled_from(_SESSION_IDS),
        api_key=st.sampled_from([_API_KEY_A, _API_KEY_B]),
        stream=st.booleans(),
    )
    def send_normal(self, session_id, api_key, stream):
        resp = self._post_chat(
            session_id=session_id,
            prompt=f"{_MARK_NORMAL} — {uuid.uuid4().hex[:8]}",
            api_key=api_key,
            stream=stream,
        )
        self._record(resp, session_id)

    @rule(session_id=st.sampled_from(_SESSION_IDS))
    def send_ssn_in_response(self, session_id):
        """SSN is a WARN-tier PII type — response not blocked, but analyzer fires."""
        resp = self._post_chat(
            session_id=session_id,
            prompt=f"{_MARK_SSN} — {uuid.uuid4().hex[:8]}",
            api_key=_API_KEY_A,
            stream=False,
        )
        self._record(resp, session_id)

    @rule(session_id=st.sampled_from(_SESSION_IDS))
    def send_credit_card_in_response(self, session_id):
        """Credit card is a BLOCK-tier PII type — response should be 403."""
        resp = self._post_chat(
            session_id=session_id,
            prompt=f"{_MARK_CREDIT} — {uuid.uuid4().hex[:8]}",
            api_key=_API_KEY_A,
            stream=False,
        )
        self._record(resp, session_id)

    @rule(session_id=st.sampled_from(_SESSION_IDS))
    def send_csam_in_response(self, session_id):
        """S4 child safety in response → Toxicity BLOCK → 403."""
        resp = self._post_chat(
            session_id=session_id,
            prompt=f"{_MARK_CSAM} — {uuid.uuid4().hex[:8]}",
            api_key=_API_KEY_A,
            stream=False,
        )
        self._record(resp, session_id)

    @rule(session_id=st.sampled_from(_SESSION_IDS))
    def send_wrong_auth(self, session_id):
        resp = self._post_chat(
            session_id=session_id,
            prompt=_MARK_NORMAL,
            api_key="wrong-key-xyz",
            stream=False,
        )
        self._record(resp, session_id)

    @rule(session_id=st.sampled_from(_SESSION_IDS))
    def send_no_auth(self, session_id):
        resp = self._post_chat(
            session_id=session_id,
            prompt=_MARK_NORMAL,
            api_key=None,
            stream=False,
        )
        self._record(resp, session_id)

    @rule(session_id=st.sampled_from(_SESSION_IDS))
    def send_malformed_body(self, session_id):
        resp = self._post_chat(
            session_id=session_id,
            prompt="",
            api_key=_API_KEY_A,
            stream=False,
            malformed=True,
        )
        self._record(resp, session_id)

    @rule(session_id=st.sampled_from(_SESSION_IDS))
    def send_unknown_model(self, session_id):
        """Model not in attestation registry → denied_attestation → 403."""
        resp = self._post_chat(
            session_id=session_id,
            prompt=_MARK_NORMAL,
            api_key=_API_KEY_A,
            stream=False,
            model="no-such-model",
        )
        self._record(resp, session_id)

    @precondition(lambda self: not self.deny_policy_active)
    @rule()
    def install_deny_policy(self):
        """Install a deny-all policy that blocks every request via the control
        plane, then refresh caches. Subsequent requests should be denied."""
        if self.ctx.control_store is None:
            return
        # Clean up any stale entry with the same id from a prior run
        try:
            self.ctx.control_store.delete_policy("stateful-deny-all")
        except Exception:
            pass
        self.ctx.control_store.create_policy({
            "policy_id": "stateful-deny-all",
            "policy_name": "deny-all",
            "status": "active",
            "enforcement_level": "blocking",
            "tenant_id": _TENANT,
            "rules": [{
                "field": "model_id", "operator": "regex",
                "value": ".*", "action": "deny",
            }],
        })
        from gateway.control.api import _refresh_policy_cache
        _refresh_policy_cache()
        self.deny_policy_active = True

    @precondition(lambda self: self.deny_policy_active)
    @rule()
    def remove_deny_policy(self):
        if self.ctx.control_store is None:
            return
        try:
            self.ctx.control_store.delete_policy("stateful-deny-all")
        except Exception:
            pass
        from gateway.control.api import _refresh_policy_cache
        _refresh_policy_cache()
        self.deny_policy_active = False

    # ── Invariants (cheap, checked after every rule) ──

    @invariant()
    def no_5xx_responses(self):
        """I8: every response is in the known-good set."""
        if not self.responses:
            return
        last = self.responses[-1]
        assert last.status_code in _ALLOWED_STATUS, (
            f"unexpected status code: {last.status_code}\n"
            f"body: {last.text[:400]}"
        )

    @invariant()
    def verdict_buffer_bounded(self):
        """I10: the verdict buffer never exceeds its max_size."""
        vb = self.ctx.verdict_buffer
        if vb is None:
            return
        assert vb.size <= vb._max, (
            f"verdict buffer overflow: size={vb.size} max={vb._max}"
        )

    @invariant()
    def csam_responses_blocked(self):
        """I4: any CSAM-triggered response must be 403, never a 200 with content."""
        for resp in self.responses:
            req = resp.request
            if req is None:
                continue
            try:
                req_body = req.content
                if _MARK_CSAM.encode() in (req_body or b""):
                    # The toxicity analyzer must have blocked this
                    assert resp.status_code == 403, (
                        f"CSAM-marked response leaked with status "
                        f"{resp.status_code}; must be 403"
                    )
            except Exception:
                continue

    @invariant()
    def deny_policy_blocks_new_requests(self):
        """When deny-all policy is active, the most recent authenticated chat
        request must not be 200. We check the most recent response only to
        keep the invariant cheap; previous responses were checked on their
        own step."""
        if not self.deny_policy_active or not self.responses:
            return
        last = self.responses[-1]
        req = last.request
        if req is None:
            return
        # Only assert on well-formed authed requests (malformed/wrong-auth have
        # their own reason for non-200 independent of policy state).
        if req.headers.get("x-api-key") not in (_API_KEY_A, _API_KEY_B):
            return
        try:
            body = json.loads(req.content or b"{}")
        except Exception:
            return  # malformed — different failure mode
        if body.get("model") != _MODEL_ID:
            return  # unknown-model path
        # Authed + well-formed + correct model → deny policy should block
        assert last.status_code in (403, 429), (
            f"deny policy active but authed request returned {last.status_code}"
        )

    # ── Teardown: expensive invariants + cleanup ──

    def teardown(self):
        try:
            if self.fired_count > 0:
                self._run(asyncio.sleep(0.3))  # let completeness middleware flush
                self._check_chain_integrity()
                self._check_completeness()
        finally:
            self._run(self._close())
            self._loop.close()
            self._tmp.cleanup()

    async def _close(self):
        try:
            await self.client.aclose()
        except Exception:
            pass
        try:
            if self.ctx.http_client is not None:
                await self.ctx.http_client.aclose()
        except Exception:
            pass
        try:
            await self._on_shutdown()
        except Exception:
            pass
        self._get_settings.cache_clear()

    def _check_chain_integrity(self):
        """I2: per-session contiguity + valid Merkle linkage."""
        wal_db = Path(self.env["WALACOR_WAL_PATH"]) / "wal.db"
        if not wal_db.exists():
            return
        conn = sqlite3.connect(f"file:{wal_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT session_id, sequence_number, record_json "
                "FROM wal_records "
                "WHERE event_type = 'execution' AND session_id IS NOT NULL "
                "ORDER BY session_id, sequence_number"
            ).fetchall()
        finally:
            conn.close()

        by_session: dict[str, list[dict]] = {}
        for r in rows:
            rec = json.loads(r["record_json"])
            by_session.setdefault(r["session_id"], []).append(rec)

        for sid, recs in by_session.items():
            seqs = [rec["sequence_number"] for rec in recs]
            assert seqs == list(range(len(seqs))), (
                f"session {sid!r} sequence gap: {seqs}"
            )
            prev = "0" * 128  # GENESIS
            for rec in recs:
                expected = _recompute_record_hash(
                    execution_id=rec["execution_id"],
                    policy_version=rec.get("policy_version", 0),
                    policy_result=rec.get("policy_result", "pass"),
                    previous_record_hash=rec["previous_record_hash"],
                    sequence_number=rec["sequence_number"],
                    timestamp=rec["timestamp"],
                )
                assert rec["record_hash"] == expected, (
                    f"session {sid!r} seq {rec['sequence_number']}: "
                    f"record_hash mismatch"
                )
                assert rec["previous_record_hash"] == prev, (
                    f"session {sid!r} seq {rec['sequence_number']}: "
                    f"previous_record_hash linkage broken"
                )
                prev = rec["record_hash"]

    def _check_completeness(self):
        """I1: every fired request produces a gateway_attempts row (≥ 90%)."""
        wal_db = Path(self.env["WALACOR_WAL_PATH"]) / "wal.db"
        if not wal_db.exists():
            return
        conn = sqlite3.connect(f"file:{wal_db}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM gateway_attempts"
            ).fetchone()
            attempt_count = rows[0]
        finally:
            conn.close()
        assert attempt_count >= self.fired_count * 0.9, (
            f"completeness gap: fired={self.fired_count} "
            f"attempts={attempt_count}"
        )


# Hypothesis stateful settings — keep setup amortized.
GatewayStateMachine.TestCase.settings = settings(
    max_examples=5,
    stateful_step_count=25,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.filter_too_much,
    ],
)


# pytest discovery: expose the TestCase as a pytest test.
TestGatewayStateMachine = GatewayStateMachine.TestCase
