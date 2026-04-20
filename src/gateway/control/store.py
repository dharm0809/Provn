"""SQLite-backed persistence for the embedded control plane."""

from __future__ import annotations

import fnmatch
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
    model_hash TEXT DEFAULT '',
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

CREATE TABLE IF NOT EXISTS shadow_policies (
    policy_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    rules_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_pricing (
    pricing_id TEXT PRIMARY KEY,
    model_pattern TEXT NOT NULL,
    input_cost_per_1k REAL NOT NULL DEFAULT 0.0,
    output_cost_per_1k REAL NOT NULL DEFAULT 0.0,
    currency TEXT NOT NULL DEFAULT 'USD',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(model_pattern)
);

CREATE TABLE IF NOT EXISTS key_policy_assignments (
    api_key_hash  TEXT NOT NULL,
    policy_id     TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (api_key_hash, policy_id)
);

CREATE TABLE IF NOT EXISTS key_tool_permissions (
    api_key_hash  TEXT NOT NULL,
    tool_name     TEXT NOT NULL,
    allowed       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (api_key_hash, tool_name)
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
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_SCHEMA_SQL)
            # Migrate existing DBs that predate the model_hash column
            try:
                self._conn.execute("ALTER TABLE attestations ADD COLUMN model_hash TEXT DEFAULT ''")
                self._conn.commit()
            except Exception:
                pass  # column already exists
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
        model_hash = data.get("model_hash", "") or ""

        conn.execute(
            """INSERT INTO attestations
                   (attestation_id, model_id, provider, status, verification_level,
                    tenant_id, notes, model_hash, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(tenant_id, provider, model_id) DO UPDATE SET
                   status = excluded.status,
                   verification_level = excluded.verification_level,
                   notes = excluded.notes,
                   model_hash = CASE WHEN excluded.model_hash != '' THEN excluded.model_hash ELSE model_hash END,
                   updated_at = excluded.updated_at
            """,
            (attestation_id, model_id, provider, status, verification_level,
             tenant_id, notes, model_hash, now, now),
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

    # ── Shadow Policy CRUD ─────────────────────────────────────

    def list_shadow_policies(self, tenant_id: str = "") -> list[dict[str, Any]]:
        conn = self._ensure_conn()
        if tenant_id:
            cur = conn.execute(
                "SELECT * FROM shadow_policies WHERE tenant_id = ? ORDER BY updated_at DESC",
                (tenant_id,),
            )
        else:
            cur = conn.execute("SELECT * FROM shadow_policies ORDER BY updated_at DESC")
        rows = []
        for row in cur.fetchall():
            d = dict(row)
            d["rules"] = json.loads(d.pop("rules_json", "[]"))
            rows.append(d)
        return rows

    def upsert_shadow_policy(
        self, policy_id: str, tenant_id: str, name: str, rules: list[dict[str, Any]],
    ) -> dict[str, Any]:
        conn = self._ensure_conn()
        now = self._now()
        policy_id = policy_id or self._new_id()
        conn.execute(
            """INSERT INTO shadow_policies
                   (policy_id, tenant_id, name, version, rules_json, created_at, updated_at)
               VALUES (?, ?, ?, 1, ?, ?, ?)
               ON CONFLICT(policy_id) DO UPDATE SET
                   name = excluded.name,
                   version = shadow_policies.version + 1,
                   rules_json = excluded.rules_json,
                   updated_at = excluded.updated_at
            """,
            (policy_id, tenant_id, name, json.dumps(rules), now, now),
        )
        conn.commit()
        cur = conn.execute(
            "SELECT * FROM shadow_policies WHERE policy_id = ?", (policy_id,),
        )
        row = cur.fetchone()
        if row:
            d = dict(row)
            d["rules"] = json.loads(d.pop("rules_json", "[]"))
            return d
        return {"policy_id": policy_id}

    def delete_shadow_policy(self, policy_id: str) -> bool:
        conn = self._ensure_conn()
        cur = conn.execute("DELETE FROM shadow_policies WHERE policy_id = ?", (policy_id,))
        conn.commit()
        return cur.rowcount > 0

    # ── Model Pricing CRUD ──────────────────────────────────────

    def list_model_pricing(self) -> list[dict[str, Any]]:
        conn = self._ensure_conn()
        cur = conn.execute("SELECT * FROM model_pricing ORDER BY updated_at DESC")
        return [dict(row) for row in cur.fetchall()]

    def upsert_model_pricing(self, data: dict[str, Any]) -> dict[str, Any]:
        conn = self._ensure_conn()
        now = self._now()
        pricing_id = data.get("pricing_id") or self._new_id()
        model_pattern = data.get("model_pattern", "")
        input_cost = float(data.get("input_cost_per_1k", 0.0))
        output_cost = float(data.get("output_cost_per_1k", 0.0))
        currency = data.get("currency", "USD")

        conn.execute(
            """INSERT INTO model_pricing
                   (pricing_id, model_pattern, input_cost_per_1k, output_cost_per_1k,
                    currency, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(model_pattern) DO UPDATE SET
                   input_cost_per_1k = excluded.input_cost_per_1k,
                   output_cost_per_1k = excluded.output_cost_per_1k,
                   currency = excluded.currency,
                   updated_at = excluded.updated_at
            """,
            (pricing_id, model_pattern, input_cost, output_cost, currency, now, now),
        )
        conn.commit()
        cur = conn.execute(
            "SELECT * FROM model_pricing WHERE model_pattern = ?",
            (model_pattern,),
        )
        row = cur.fetchone()
        return dict(row) if row else {"pricing_id": pricing_id}

    def delete_model_pricing(self, pricing_id: str) -> bool:
        conn = self._ensure_conn()
        cur = conn.execute("DELETE FROM model_pricing WHERE pricing_id = ?", (pricing_id,))
        conn.commit()
        return cur.rowcount > 0

    def get_model_pricing(self, model_id: str) -> dict[str, Any] | None:
        """Find first pricing row whose model_pattern fnmatch-matches model_id."""
        rows = self.list_model_pricing()
        for row in rows:
            if fnmatch.fnmatch(model_id, row["model_pattern"]):
                return row
        return None

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
                "model_hash": r.get("model_hash") or "",
            })
        return proofs

    def update_model_hash(self, provider: str, model_id: str, tenant_id: str, model_hash: str) -> None:
        """Backfill model_hash into an attestation that was created without one.

        Only writes if the existing model_hash is empty — never overwrites a known hash.
        """
        conn = self._ensure_conn()
        conn.execute(
            "UPDATE attestations SET model_hash = ?, updated_at = ? "
            "WHERE provider = ? AND model_id = ? AND tenant_id = ? "
            "AND (model_hash IS NULL OR model_hash = '')",
            (model_hash, self._now(), provider, model_id, tenant_id),
        )
        conn.commit()

    def get_active_policies(self, tenant_id: str) -> list[dict[str, Any]]:
        """Format active policies matching SyncClient expectation."""
        all_policies = self.list_policies(tenant_id)
        return [p for p in all_policies if p.get("status") == "active"]

    def get_policy(self, policy_id: str) -> dict[str, Any] | None:
        """Return a single policy by ID, or None if not found."""
        conn = self._ensure_conn()
        cur = conn.execute("SELECT * FROM policies WHERE policy_id = ?", (policy_id,))
        row = cur.fetchone()
        if row is None:
            return None
        d = dict(row)
        d["rules"] = json.loads(d.pop("rules_json", "[]"))
        d["prompt_rules"] = json.loads(d.pop("prompt_rules_json", "[]"))
        d["rag_rules"] = json.loads(d.pop("rag_rules_json", "[]"))
        return d

    # ── Key-Policy Assignment CRUD ────────────────────────────

    def get_key_policies(self, api_key_hash: str) -> list[str]:
        """Return list of policy_ids assigned to an API key."""
        conn = self._ensure_conn()
        rows = conn.execute(
            "SELECT policy_id FROM key_policy_assignments WHERE api_key_hash = ?",
            (api_key_hash,),
        ).fetchall()
        return [row[0] for row in rows]

    def set_key_policies(self, api_key_hash: str, policy_ids: list[str]) -> None:
        """Replace all policy assignments for a key."""
        conn = self._ensure_conn()
        with conn:
            conn.execute(
                "DELETE FROM key_policy_assignments WHERE api_key_hash = ?",
                (api_key_hash,),
            )
            for pid in policy_ids:
                conn.execute(
                    "INSERT OR REPLACE INTO key_policy_assignments (api_key_hash, policy_id) VALUES (?, ?)",
                    (api_key_hash, pid),
                )

    def remove_key_policy(self, api_key_hash: str, policy_id: str) -> bool:
        """Remove a single policy from a key. Returns True if it existed."""
        conn = self._ensure_conn()
        with conn:
            cursor = conn.execute(
                "DELETE FROM key_policy_assignments WHERE api_key_hash = ? AND policy_id = ?",
                (api_key_hash, policy_id),
            )
        return cursor.rowcount > 0

    def list_key_policy_assignments(self) -> list[dict]:
        """Return all key-policy assignments."""
        conn = self._ensure_conn()
        rows = conn.execute(
            "SELECT api_key_hash, policy_id, created_at FROM key_policy_assignments"
            " ORDER BY api_key_hash, policy_id"
        ).fetchall()
        return [{"api_key_hash": r[0], "policy_id": r[1], "created_at": r[2]} for r in rows]

    # ── Key-Tool Permission CRUD ──────────────────────────────

    def get_allowed_tools(self, api_key_hash: str) -> list[str] | None:
        """Return allowed tool names for a key, or None if no restrictions are set.

        Semantics:
          None  → key has no row at all → unrestricted (use all tools)
          []    → key has rows but none with allowed=1 → all tools blocked
          [...]  → list of tool names the key may use
        """
        conn = self._ensure_conn()
        rows = conn.execute(
            "SELECT tool_name FROM key_tool_permissions WHERE api_key_hash = ? AND allowed = 1",
            (api_key_hash,),
        ).fetchall()
        if not rows:
            # Distinguish "no rows at all" (unrestricted) from "rows but none allowed" (all denied)
            any_row = conn.execute(
                "SELECT 1 FROM key_tool_permissions WHERE api_key_hash = ? LIMIT 1",
                (api_key_hash,),
            ).fetchone()
            return None if any_row is None else []
        return [row[0] for row in rows]

    def set_tool_permission(self, api_key_hash: str, tool_name: str, allowed: bool) -> None:
        """Upsert a single tool permission for a key."""
        conn = self._ensure_conn()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO key_tool_permissions"
                " (api_key_hash, tool_name, allowed) VALUES (?, ?, ?)",
                (api_key_hash, tool_name, 1 if allowed else 0),
            )

    def set_allowed_tools(self, api_key_hash: str, tool_names: list[str]) -> None:
        """Replace all tool permissions for a key with the given allow-list.

        When tool_names is empty, a sentinel row (tool_name='', allowed=0) is
        inserted so that the key is marked as *explicitly restricted to nothing*,
        which is distinguishable from a key that has never had any restrictions
        set (no rows at all → unrestricted).
        """
        conn = self._ensure_conn()
        with conn:
            conn.execute(
                "DELETE FROM key_tool_permissions WHERE api_key_hash = ?",
                (api_key_hash,),
            )
            if tool_names:
                for name in tool_names:
                    conn.execute(
                        "INSERT INTO key_tool_permissions"
                        " (api_key_hash, tool_name, allowed) VALUES (?, ?, 1)",
                        (api_key_hash, name),
                    )
            else:
                # Sentinel row: key is restricted but all tools are blocked
                conn.execute(
                    "INSERT INTO key_tool_permissions"
                    " (api_key_hash, tool_name, allowed) VALUES (?, '', 0)",
                    (api_key_hash,),
                )

    def remove_tool_permission(self, api_key_hash: str, tool_name: str) -> bool:
        """Remove a tool permission entry. Returns True if it existed."""
        conn = self._ensure_conn()
        with conn:
            cursor = conn.execute(
                "DELETE FROM key_tool_permissions WHERE api_key_hash = ? AND tool_name = ?",
                (api_key_hash, tool_name),
            )
        return cursor.rowcount > 0
