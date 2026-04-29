"""Token-based rate limiter middleware.

Uses a sliding window counter keyed by (scope_value, window_start).
Returns HTTP 429 with Retry-After header when the token budget for
the current window is exhausted.

This is distinct from the BudgetTracker (total quota) — this is
a per-period rate limit that resets every window_seconds.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)


class TokenRateLimiter(BaseHTTPMiddleware):
    """Sliding-window token rate limiter.

    Tracks tokens consumed per (scope_key, window_bucket) pair.
    scope_key is derived from the request based on `scope` setting:
      - "user"   → X-User-Id header or "anonymous"
      - "key"    → Authorization header value (hashed)
      - "tenant" → X-Team-Id header or "default"
      - "global" → single shared bucket
    """

    def __init__(
        self,
        app: ASGIApp,
        max_tokens: int,
        window_seconds: int,
        scope: str = "user",
        enabled: bool = True,
    ) -> None:
        super().__init__(app)
        self._max_tokens = max_tokens
        self._window_seconds = window_seconds
        self._scope = scope
        self._enabled = enabled
        # {(scope_key, window_bucket): token_count}
        self._counters: dict[tuple[str, int], int] = defaultdict(int)
        self._last_cleanup = time.monotonic()

    def _get_scope_key(self, request: Request) -> str:
        # SECURITY: never trust X-User-Id / X-Team-Id headers directly here — those
        # headers are caller-supplied input to the AUTH pipeline. Once auth runs,
        # the authenticated identity lands on `request.state.caller_identity`
        # (see api_key_middleware → _resolve_header_identity_fallback / JWT path).
        # Read identity from there. If no auth ran, fall back to per-IP buckets so
        # a header-spoofed user can't escape another user's bucket.
        if self._scope == "user":
            ident = getattr(getattr(request, "state", None), "caller_identity", None)
            user_id = getattr(ident, "user_id", "") if ident else ""
            if user_id:
                return f"user:{user_id}"
            client = getattr(request, "client", None)
            host = getattr(client, "host", None) or "unknown"
            return f"ip:{host}"
        if self._scope == "key":
            import hashlib
            auth = request.headers.get("authorization", "")
            return hashlib.sha256(auth.encode()).hexdigest()[:16]
        if self._scope == "tenant":
            ident = getattr(getattr(request, "state", None), "caller_identity", None)
            team = getattr(ident, "team", None) if ident else None
            if team:
                return f"team:{team}"
            client = getattr(request, "client", None)
            host = getattr(client, "host", None) or "unknown"
            return f"ip:{host}"
        return "global"

    def _current_window(self) -> int:
        return int(time.monotonic() // self._window_seconds)

    def _cleanup_old_windows(self) -> None:
        now = time.monotonic()
        if now - self._last_cleanup < self._window_seconds:
            return
        current = self._current_window()
        stale = [k for k in self._counters if k[1] < current - 1]
        for k in stale:
            del self._counters[k]
        self._last_cleanup = now

    def record_tokens(self, scope_key: str, tokens: int) -> None:
        """Record token consumption after a completed request."""
        bucket = (scope_key, self._current_window())
        self._counters[bucket] += tokens

    def check_limit(self, scope_key: str) -> tuple[bool, int]:
        """Check if scope_key is within limit. Returns (allowed, tokens_used)."""
        bucket = (scope_key, self._current_window())
        used = self._counters[bucket]
        return used < self._max_tokens, used

    async def dispatch(self, request: Request, call_next) -> Response:
        if not self._enabled:
            return await call_next(request)

        # Only rate-limit LLM inference paths
        if not request.url.path.rstrip("/").endswith(
            ("/chat/completions", "/completions", "/generate", "/messages")
        ):
            return await call_next(request)

        self._cleanup_old_windows()
        scope_key = self._get_scope_key(request)
        allowed, used = self.check_limit(scope_key)

        if not allowed:
            retry_after = self._window_seconds - (int(time.monotonic()) % self._window_seconds)
            logger.warning(
                "Token rate limit exceeded: scope=%s used=%d max=%d",
                scope_key, used, self._max_tokens,
            )
            return JSONResponse(
                {
                    "error": {
                        "message": (
                            f"Token rate limit exceeded. "
                            f"Used {used}/{self._max_tokens} tokens in current window."
                        ),
                        "type": "rate_limit_exceeded",
                        "code": "token_rate_limit",
                    }
                },
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        # Store scope_key on request state so orchestrator can record tokens after response
        request.state.rate_limit_scope_key = scope_key
        response = await call_next(request)

        # Best-effort token recording from request.state (set by orchestrator after inference)
        tokens_used = getattr(request.state, "walacor_total_tokens", None)
        if tokens_used and isinstance(tokens_used, int) and tokens_used > 0:
            self.record_tokens(scope_key, tokens_used)
            logger.debug(
                "Token rate limiter recorded: scope=%s tokens=%d window=%d",
                scope_key, tokens_used, self._current_window(),
            )

        return response
