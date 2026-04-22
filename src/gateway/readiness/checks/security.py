"""Security readiness checks: SEC-01 through SEC-07."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from gateway.config import get_settings
from gateway.readiness.protocol import Category, CheckResult, Severity
from gateway.readiness.registry import register

if TYPE_CHECKING:
    from gateway.pipeline.context import PipelineContext


def _is_prod_host(settings) -> bool:
    """Heuristic: host binds to a non-loopback address (§11 decision 4, reduced form)."""
    host = (settings.gateway_host or "").strip()
    return host not in ("", "127.0.0.1", "localhost", "::1")


class _Sec01ApiKeyEnforced:
    id = "SEC-01"
    name = "API key enforced"
    category = Category.security
    severity = Severity.sec

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        settings = get_settings()
        keys = settings.api_keys_list
        elapsed = int((time.monotonic() - t0) * 1000)
        if not keys:
            return CheckResult(
                status="amber",
                detail="No API keys configured — gateway accepts all requests",
                remediation="Set WALACOR_GATEWAY_API_KEYS to one or more keys",
                evidence={"key_count": 0},
                elapsed_ms=elapsed,
            )
        auto_generated = [k for k in keys if k.startswith("wgk-")]
        if len(auto_generated) == len(keys):
            from gateway.auth.bootstrap_key import bootstrap_key_stable
            stable = bootstrap_key_stable(settings.wal_path)
            detail = (
                f"{len(keys)} auto-generated wgk-* key(s) in use — "
                + ("recommend moving to a secret store" if stable
                   else "key rotating on every restart (persistence failed)")
            )
            return CheckResult(
                status="amber",
                detail=detail,
                remediation="Replace auto-generated keys with stable credentials from your secret manager",
                evidence={
                    "key_count": len(keys),
                    "auto_generated": len(auto_generated),
                    "bootstrap_key_stable": stable,
                },
                elapsed_ms=elapsed,
            )
        return CheckResult(
            status="green",
            detail=f"{len(keys)} API key(s) configured",
            evidence={"key_count": len(keys)},
            elapsed_ms=elapsed,
        )


class _Sec02LineageAuthActive:
    id = "SEC-02"
    name = "Lineage auth active"
    category = Category.security
    severity = Severity.sec

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        settings = get_settings()
        elapsed_ms = lambda: int((time.monotonic() - t0) * 1000)

        if not settings.api_keys_list:
            return CheckResult(
                status="green",
                detail="No API keys configured — lineage auth not applicable",
                evidence={"api_keys_set": False},
                elapsed_ms=elapsed_ms(),
            )
        if not settings.lineage_auth_required:
            return CheckResult(
                status="red",
                detail="/v1/lineage/* is publicly accessible (lineage_auth_required=false)",
                remediation="Set WALACOR_LINEAGE_AUTH_REQUIRED=true to gate lineage endpoints",
                evidence={"lineage_auth_required": False},
                elapsed_ms=elapsed_ms(),
            )

        try:
            from starlette.testclient import TestClient
            from gateway.main import app
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/v1/lineage/sessions")
            got_401 = resp.status_code == 401
        except Exception as exc:
            return CheckResult(
                status="amber",
                detail=f"Could not probe /v1/lineage/sessions: {exc}",
                elapsed_ms=elapsed_ms(),
            )
        if got_401:
            return CheckResult(
                status="green",
                detail="/v1/lineage/sessions returns 401 without API key",
                evidence={"probe_status_code": 401},
                elapsed_ms=elapsed_ms(),
            )
        return CheckResult(
            status="red",
            detail=f"/v1/lineage/sessions returned {resp.status_code} without API key (expected 401)",
            remediation="Ensure lineage_auth_required=true and api_keys_list is non-empty",
            evidence={"probe_status_code": resp.status_code},
            elapsed_ms=elapsed_ms(),
        )


class _Sec03JwtIssuerAudience:
    id = "SEC-03"
    name = "JWT issuer & audience set"
    category = Category.security
    severity = Severity.sec

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        settings = get_settings()
        elapsed = int((time.monotonic() - t0) * 1000)

        if settings.auth_mode not in ("jwt", "both"):
            return CheckResult(status="green", detail=f"auth_mode={settings.auth_mode} — JWT not in use", elapsed_ms=elapsed)

        missing = []
        if not settings.jwt_issuer: missing.append("jwt_issuer")
        if not settings.jwt_audience: missing.append("jwt_audience")
        if missing:
            return CheckResult(
                status="red",
                detail=f"auth_mode={settings.auth_mode} but missing: {', '.join(missing)}",
                remediation="Set WALACOR_JWT_ISSUER and WALACOR_JWT_AUDIENCE",
                evidence={"missing": missing},
                elapsed_ms=elapsed,
            )
        return CheckResult(
            status="green",
            detail="JWT issuer and audience set",
            evidence={"issuer": settings.jwt_issuer, "audience": settings.jwt_audience},
            elapsed_ms=elapsed,
        )


class _Sec04JwtKeyMaterial:
    id = "SEC-04"
    name = "JWT key material present"
    category = Category.security
    severity = Severity.sec

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        settings = get_settings()
        elapsed = int((time.monotonic() - t0) * 1000)

        if settings.auth_mode not in ("jwt", "both"):
            return CheckResult(status="green", detail=f"auth_mode={settings.auth_mode} — JWT not in use", elapsed_ms=elapsed)

        secret_ok = len(settings.jwt_secret or "") >= 32
        jwks_ok = bool(settings.jwt_jwks_url)
        if not (secret_ok or jwks_ok):
            return CheckResult(
                status="red",
                detail="No JWT key material: jwt_secret too short (<32) and jwt_jwks_url empty",
                remediation="Set WALACOR_JWT_SECRET (≥32 chars) or WALACOR_JWT_JWKS_URL",
                evidence={"secret_chars": len(settings.jwt_secret or ""), "jwks_url_set": jwks_ok},
                elapsed_ms=elapsed,
            )
        return CheckResult(
            status="green",
            detail="JWT key material present",
            evidence={"secret_ok": secret_ok, "jwks_url_set": jwks_ok},
            elapsed_ms=elapsed,
        )


class _Sec05JwksReachable:
    id = "SEC-05"
    name = "JWKS reachable"
    category = Category.security
    severity = Severity.sec

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        settings = get_settings()
        elapsed_ms = lambda: int((time.monotonic() - t0) * 1000)

        if settings.auth_mode not in ("jwt", "both") or not settings.jwt_jwks_url:
            return CheckResult(status="green", detail="JWKS not configured — skipping", elapsed_ms=elapsed_ms())

        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as http:
                resp = await http.get(settings.jwt_jwks_url)
            if resp.status_code != 200:
                return CheckResult(
                    status="red",
                    detail=f"JWKS returned HTTP {resp.status_code}",
                    evidence={"jwks_url": settings.jwt_jwks_url, "status_code": resp.status_code},
                    elapsed_ms=elapsed_ms(),
                )
            body = resp.json()
            if not isinstance(body, dict) or "keys" not in body:
                return CheckResult(
                    status="red",
                    detail="JWKS response not parseable (missing 'keys')",
                    evidence={"jwks_url": settings.jwt_jwks_url},
                    elapsed_ms=elapsed_ms(),
                )
            return CheckResult(
                status="green",
                detail=f"JWKS reachable with {len(body['keys'])} key(s)",
                evidence={"key_count": len(body["keys"])},
                elapsed_ms=elapsed_ms(),
            )
        except Exception as exc:
            return CheckResult(
                status="red",
                detail=f"JWKS fetch failed: {exc}",
                remediation="Check jwt_jwks_url and network egress",
                evidence={"jwks_url": settings.jwt_jwks_url, "error": str(exc)},
                elapsed_ms=elapsed_ms(),
            )


class _Sec06EnforcementMode:
    id = "SEC-06"
    name = "Enforcement mode"
    category = Category.security
    severity = Severity.sec

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        settings = get_settings()
        elapsed = int((time.monotonic() - t0) * 1000)

        if getattr(settings, "skip_governance", False):
            return CheckResult(
                status="amber",
                detail="skip_governance=true — transparent proxy mode, no policy enforcement",
                remediation="Disable skip_governance for production",
                evidence={"skip_governance": True},
                elapsed_ms=elapsed,
            )
        mode = settings.enforcement_mode
        if mode == "enforced":
            return CheckResult(status="green", detail="enforcement_mode=enforced", elapsed_ms=elapsed)
        return CheckResult(
            status="amber",
            detail=f"enforcement_mode={mode} — blocks recorded but not applied",
            remediation="Set WALACOR_ENFORCEMENT_MODE=enforced for production",
            evidence={"enforcement_mode": mode},
            elapsed_ms=elapsed,
        )


class _Sec07TenantIdSet:
    id = "SEC-07"
    name = "Tenant ID set"
    category = Category.security
    severity = Severity.warn

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        settings = get_settings()
        elapsed = int((time.monotonic() - t0) * 1000)

        if settings.gateway_tenant_id:
            return CheckResult(status="green", detail=f"tenant_id={settings.gateway_tenant_id}", elapsed_ms=elapsed)

        is_prod = _is_prod_host(settings)
        return CheckResult(
            status="red" if is_prod else "amber",
            detail="gateway_tenant_id empty" + (" (prod host)" if is_prod else " (dev)"),
            remediation="Set WALACOR_GATEWAY_TENANT_ID",
            evidence={"is_prod_host": is_prod, "host": settings.gateway_host},
            elapsed_ms=elapsed,
        )


register(_Sec01ApiKeyEnforced())
register(_Sec02LineageAuthActive())
register(_Sec03JwtIssuerAudience())
register(_Sec04JwtKeyMaterial())
register(_Sec05JwksReachable())
register(_Sec06EnforcementMode())
register(_Sec07TenantIdSet())
