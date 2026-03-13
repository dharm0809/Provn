"""SQLite-backed persistence for the embedded control plane."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS attestations (
    attestation_id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'ollama',
    status TEXT NOT NULL DEFAULT 'active',
    verification_level TEXT NOT NULL DEFAULT 'admin_attested',
    tenant_id TEXT NOT NULL,
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, provider, model_id)
);

CREATE TABLE IF NOT EXISTS policies (
    policy_id TEXT PRIMARY KEY,
    policy_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    enforcement_level TEXT NOT NULL DEFAULT 'blocking',
    rules_json TEXT NOT NULL DEFAULT '[]',
    prompt_rules_json TEXT NOT NULL DEFAULT '[]',
    rag_rules_json TEXT NOT NULL DEFAULT '[]',
    tenant_id TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budgets (
    budget_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user TEXT DEFAULT '',
    period TEXT NOT NULL DEFAULT 'monthly',
    max_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, user, period)
);

CREATE TABLE IF NOT EXISTS content_policies (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT '*',
    analyzer_id TEXT NOT NULL,
    category TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'warn',
    threshold REAL DEFAULT 0.5,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, analyzer_id, category)
);
"""


class ControlPlaneStore:
    """SQLite-backed CRUD store for embedded control plane state.

    Same WAL pattern as WALWriter: journal_mode=WAL, synchronous=FULL, lazy init.
    """

    def __init__(self, db_path: str) -> None:
        self._path = db_path
        self._conn: sqlite3.Connection | None = None

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._path)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_SCHEMA_SQL)
        return self._conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Helpers ────────────────────────────────────────────────

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _new_id() -> str:
        return str(uuid.uuid4())

    # ── Attestation CRUD ──────────────────────────────────────

    def list_attestations(self, tenant_id: str = "") -> list[dict[str, Any]]:
        conn = self._ensure_conn()
        if tenant_id:
            cur = conn.execute(
                "SELECT * FROM attestations WHERE tenant_id = ? ORDER BY updated_at DESC",
                (tenant_id,),
            )
        else:
            cur = conn.execute("SELECT * FROM attestations ORDER BY updated_at DESC")
        return [dict(row) for row in cur.fetchall()]

    def upsert_attestation(self, data: dict[str, Any]) -> dict[str, Any]:
        conn = self._ensure_conn()
        now = self._now()
        attestation_id = data.get("attestation_id") or self._new_id()
        tenant_id = data.get("tenant_id", "")
        model_id = data.get("model_id", "")
        provider = data.get("provider", "ollama")
        status = data.get("status", "active")
        verification_level = data.get("verification_level", "admin_attested")
        notes = data.get("notes", "")

        conn.execute(
            """INSERT INTO attestations
                   (attestation_id, model_id, provider, status, verification_level,
                    tenant_id, notes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(tenant_id, provider, model_id) DO UPDATE SET
                   status = excluded.status,
                   verification_level = excluded.verification_level,
                   notes = excluded.notes,
                   updated_at = excluded.updated_at
            """,
            (attestation_id, model_id, provider, status, verification_level,
             tenant_id, notes, now, now),
        )
        conn.commit()
        # Return the actual row (may have existing attestation_id on conflict)
        cur = conn.execute(
            "SELECT * FROM attestations WHERE tenant_id = ? AND provider = ? AND model_id = ?",
            (tenant_id, provider, model_id),
        )
        row = cur.fetchone()
        return dict(row) if row else {"attestation_id": attestation_id}

    def delete_attestation(self, attestation_id: str) -> bool:
        conn = self._ensure_conn()
        cur = conn.execute("DELETE FROM attestations WHERE attestation_id = ?", (attestation_id,))
        conn.commit()
        return cur.rowcount > 0

    # ── Policy CRUD ───────────────────────────────────────────

    def list_policies(self, tenant_id: str = "") -> list[dict[str, Any]]:
        conn = self._ensure_conn()
        if tenant_id:
            cur = conn.execute(
                "SELECT * FROM policies WHERE tenant_id = ? ORDER BY updated_at DESC",
                (tenant_id,),
            )
        else:
            cur = conn.execute("SELECT * FROM policies ORDER BY updated_at DESC")
        rows = []
        for row in cur.fetchall():
            d = dict(row)
            d["rules"] = json.loads(d.pop("rules_json", "[]"))
            d["prompt_rules"] = json.loads(d.pop("prompt_rules_json", "[]"))
            d["rag_rules"] = json.loads(d.pop("rag_rules_json", "[]"))
            rows.append(d)
        return rows

    def create_policy(self, data: dict[str, Any]) -> dict[str, Any]:
        conn = self._ensure_conn()
        now = self._now()
        policy_id = data.get("policy_id") or self._new_id()
        conn.execute(
            """INSERT INTO policies
                   (policy_id, policy_name, status, enforcement_level,
                    rules_json, prompt_rules_json, rag_rules_json,
                    tenant_id, description, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                policy_id,
                data.get("policy_name", "Untitled"),
                data.get("status", "active"),
                data.get("enforcement_level", "blocking"),
                json.dumps(data.get("rules", [])),
                json.dumps(data.get("prompt_rules", [])),
                json.dumps(data.get("rag_rules", [])),
                data.get("tenant_id", ""),
                data.get("description", ""),
                now,
                now,
            ),
        )
        conn.commit()
        return {"policy_id": policy_id}

    def update_policy(self, policy_id: str, data: dict[str, Any]) -> bool:
        conn = self._ensure_conn()
        now = self._now()
        sets: list[str] = []
        vals: list[Any] = []
        for field in ("policy_name", "status", "enforcement_level", "description"):
            if field in data:
                sets.append(f"{field} = ?")
                vals.append(data[field])
        for json_field, key in (("rules_json", "rules"), ("prompt_rules_json", "prompt_rules"), ("rag_rules_json", "rag_rules")):
            if key in data:
                sets.append(f"{json_field} = ?")
                vals.append(json.dumps(data[key]))
        if not sets:
            return False
        sets.append("updated_at = ?")
        vals.append(now)
        vals.append(policy_id)
        cur = conn.execute(
            f"UPDATE policies SET {', '.join(sets)} WHERE policy_id = ?",
            vals,
        )
        conn.commit()
        return cur.rowcount > 0

    def delete_policy(self, policy_id: str) -> bool:
        conn = self._ensure_conn()
        cur = conn.execute("DELETE FROM policies WHERE policy_id = ?", (policy_id,))
        conn.commit()
        return cur.rowcount > 0

    # ── Budget CRUD ───────────────────────────────────────────

    def list_budgets(self, tenant_id: str = "") -> list[dict[str, Any]]:
        conn = self._ensure_conn()
        if tenant_id:
            cur = conn.execute(
                "SELECT * FROM budgets WHERE tenant_id = ? ORDER BY updated_at DESC",
                (tenant_id,),
            )
        else:
            cur = conn.execute("SELECT * FROM budgets ORDER BY updated_at DESC")
        return [dict(row) for row in cur.fetchall()]

    def upsert_budget(self, data: dict[str, Any]) -> dict[str, Any]:
        conn = self._ensure_conn()
        now = self._now()
        budget_id = data.get("budget_id") or self._new_id()
        tenant_id = data.get("tenant_id", "")
        user = data.get("user", "")
        period = data.get("period", "monthly")
        max_tokens = data.get("max_tokens", 0)

        conn.execute(
            """INSERT INTO budgets
                   (budget_id, tenant_id, user, period, max_tokens, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(tenant_id, user, period) DO UPDATE SET
                   max_tokens = excluded.max_tokens,
                   updated_at = excluded.updated_at
            """,
            (budget_id, tenant_id, user, period, max_tokens, now, now),
        )
        conn.commit()
        cur = conn.execute(
            "SELECT * FROM budgets WHERE tenant_id = ? AND user = ? AND period = ?",
            (tenant_id, user, period),
        )
        row = cur.fetchone()
        return dict(row) if row else {"budget_id": budget_id}

    def delete_budget(self, budget_id: str) -> bool:
        conn = self._ensure_conn()
        cur = conn.execute("DELETE FROM budgets WHERE budget_id = ?", (budget_id,))
        conn.commit()
        return cur.rowcount > 0

    # ── Content Policy CRUD ──────────────────────────────────

    def list_content_policies(self, analyzer_id: str | None = None) -> list[dict]:
        conn = self._ensure_conn()
        if analyzer_id:
            cur = conn.execute(
                "SELECT * FROM content_policies WHERE analyzer_id = ? ORDER BY category",
                (analyzer_id,))
        else:
            cur = conn.execute("SELECT * FROM content_policies ORDER BY analyzer_id, category")
        return [dict(row) for row in cur.fetchall()]

    def upsert_content_policy(self, tenant_id: str, analyzer_id: str,
                              category: str, action: str,
                              threshold: float = 0.5) -> dict:
        conn = self._ensure_conn()
        now = self._now()
        pid = self._new_id()
        cur = conn.execute(
            """INSERT INTO content_policies (id, tenant_id, analyzer_id, category, action, threshold, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(tenant_id, analyzer_id, category) DO UPDATE SET
                 action = excluded.action, threshold = excluded.threshold, updated_at = excluded.updated_at
               RETURNING *""",
            (pid, tenant_id, analyzer_id, category, action, threshold, now, now))
        row = cur.fetchone()
        conn.commit()
        return dict(row)

    def delete_content_policy(self, policy_id: str) -> bool:
        conn = self._ensure_conn()
        cur = conn.execute("DELETE FROM content_policies WHERE id = ?", (policy_id,))
        conn.commit()
        return cur.rowcount > 0

    def seed_default_content_policies(self) -> None:
        """Seed default content policies if table is empty."""
        existing = self.list_content_policies()
        if existing:
            return
        defaults = [
            # PII
            ("*", "walacor.pii.v1", "credit_card", "block"),
            ("*", "walacor.pii.v1", "ssn", "block"),
            ("*", "walacor.pii.v1", "aws_access_key", "block"),
            ("*", "walacor.pii.v1", "api_key", "block"),
            ("*", "walacor.pii.v1", "email_address", "warn"),
            ("*", "walacor.pii.v1", "phone_number", "warn"),
            ("*", "walacor.pii.v1", "ip_address", "warn"),
            # Llama Guard
            *[("*", "walacor.llama_guard.v3", f"S{i}",
               "block" if i == 4 else "warn") for i in range(1, 15)],
            # Toxicity
            ("*", "walacor.toxicity.v1", "child_safety", "block"),
            ("*", "walacor.toxicity.v1", "self_harm", "warn"),
            ("*", "walacor.toxicity.v1", "violence", "warn"),
        ]
        for tenant, analyzer, category, action in defaults:
            self.upsert_content_policy(tenant, analyzer, category, action)

    # ── Sync-contract formatters ──────────────────────────────

    def get_attestation_proofs(self, tenant_id: str) -> list[dict[str, Any]]:
        """Format attestations as proof dicts matching SyncClient expectation."""
        rows = self.list_attestations(tenant_id)
        proofs = []
        for r in rows:
            proofs.append({
                "attestation_id": r["attestation_id"],
                "model_id": r["model_id"],
                "provider": r["provider"],
                "status": r["status"],
                "verification_level": r["verification_level"],
                "tenant_id": r["tenant_id"],
            })
        return proofs

    def get_active_policies(self, tenant_id: str) -> list[dict[str, Any]]:
        """Format active policies matching SyncClient expectation."""
        all_policies = self.list_policies(tenant_id)
        return [p for p in all_policies if p.get("status") == "active"]
