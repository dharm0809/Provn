"""Caller identity resolution from request headers and JWT claims."""

from __future__ import annotations

import dataclasses

from starlette.requests import Request


@dataclasses.dataclass(frozen=True)
class CallerIdentity:
    """Immutable caller identity resolved from JWT claims or request headers.

    ``tenant_id`` is None when the auth path could not derive one (e.g. an
    API key with no tenant binding in the control plane). Downstream code is
    responsible for falling back to ``settings.gateway_tenant_id`` — the
    identity object never invents a synthetic value.
    """

    user_id: str
    email: str = ""
    roles: list[str] = dataclasses.field(default_factory=list)
    team: str | None = None
    tenant_id: str | None = None
    source: str = "header_unverified"  # "jwt" (trusted) or "header_unverified" (advisory only)


def resolve_identity_from_headers(request: Request, body_metadata: dict | None = None) -> CallerIdentity | None:
    """Extract caller identity from headers, body metadata, or connection info.

    Priority: headers → body metadata (OpenWebUI plugin) → client IP.
    NEVER returns None — every request gets an identity for audit trail.
    """
    # User ID: generic headers → OpenWebUI headers
    user_id = (
        (request.headers.get("x-user-id") or "").strip()
        or (request.headers.get("x-openwebui-user-name") or "").strip()
        or (request.headers.get("x-openwebui-user-id") or "").strip()
    )

    # Email: generic → OpenWebUI
    email = (
        (request.headers.get("x-user-email") or "").strip()
        or (request.headers.get("x-openwebui-user-email") or "").strip()
    )

    # Roles: generic (comma-separated) → OpenWebUI (single role)
    roles_raw = (request.headers.get("x-user-roles") or "").strip()
    if roles_raw:
        roles = [r.strip() for r in roles_raw.split(",") if r.strip()]
    else:
        owui_role = (request.headers.get("x-openwebui-user-role") or "").strip()
        roles = [owui_role] if owui_role else []

    # Fallback: body metadata from OpenWebUI Walacor filter plugin
    source = "header_unverified"
    if body_metadata:
        if not user_id:
            user_id = (
                (body_metadata.get("user_id") or "").strip()
                or (body_metadata.get("openwebui_user_id") or "").strip()
                or (body_metadata.get("user_name") or "").strip()
            )
            if user_id:
                source = "openwebui_metadata"
        if not email:
            email = (body_metadata.get("user_email") or "").strip()
        if not roles:
            bm_role = (body_metadata.get("user_role") or "").strip()
            if bm_role:
                roles = [bm_role]

    # Fallback: client IP (always available)
    if not user_id:
        client = request.client
        user_id = f"anonymous@{client.host}" if client else "anonymous"
        source = "anonymous"

    # Team: generic only (no OpenWebUI equivalent)
    team = (request.headers.get("x-team-id") or "").strip() or None

    # Tenant: header-driven only.  Header-supplied tenant is advisory (the
    # source is "header_unverified") — it can be cache-isolating but should
    # never be used for trust decisions on its own.
    tenant_id = (request.headers.get("x-tenant-id") or "").strip() or None
    if tenant_id is None and body_metadata:
        bm_tenant = (body_metadata.get("tenant_id") or "").strip()
        tenant_id = bm_tenant or None

    return CallerIdentity(
        user_id=user_id,
        email=email,
        roles=roles,
        team=team,
        tenant_id=tenant_id,
        source=source,
    )
