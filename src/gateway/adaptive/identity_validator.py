# src/gateway/adaptive/identity_validator.py
"""Identity cross-validation — JWT claims vs header-claimed identity.

When both JWT and headers provide identity, JWT always wins on conflict.
Mismatches are logged as warnings and included in audit metadata but
do not block requests (fail-open).
"""
from __future__ import annotations

import logging
from typing import Any

from gateway.adaptive.interfaces import IdentityValidator, ValidationResult
from gateway.auth.identity import CallerIdentity

logger = logging.getLogger(__name__)


class DefaultIdentityValidator(IdentityValidator):
    """Cross-check header-claimed identity against JWT-proven identity."""

    def validate(self, jwt_identity: CallerIdentity | None,
                 header_identity: CallerIdentity | None,
                 request: Any) -> ValidationResult:
        # No JWT — return header identity as-is (unverified)
        if jwt_identity is None:
            return ValidationResult(
                valid=True, identity=header_identity,
                source="header_unverified" if header_identity else "none",
                warnings=[])

        # JWT present — cross-check headers
        warnings: list[str] = []
        headers = getattr(request, "headers", {})
        header_user = headers.get("x-user-id", "").strip()

        if header_user and header_user != jwt_identity.user_id:
            warnings.append(
                f"X-User-Id '{header_user}' does not match "
                f"JWT sub '{jwt_identity.user_id}'")
            client_ip = ""
            if hasattr(request, "client") and request.client:
                client_ip = request.client.host
            logger.warning(
                "Identity mismatch: header=%s jwt=%s ip=%s",
                header_user, jwt_identity.user_id, client_ip)

        # Merge: JWT fields take priority, headers fill gaps
        merged = CallerIdentity(
            user_id=jwt_identity.user_id,
            email=jwt_identity.email or (
                header_identity.email if header_identity else ""),
            roles=jwt_identity.roles or (
                header_identity.roles if header_identity else []),
            team=jwt_identity.team or (
                header_identity.team if header_identity else None),
            source="jwt_verified",
        )

        return ValidationResult(
            valid=len(warnings) == 0,
            identity=merged,
            source="jwt_verified",
            warnings=warnings)
