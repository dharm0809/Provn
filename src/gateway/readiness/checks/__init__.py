"""Import all check modules to populate the registry."""

from gateway.readiness.checks import security  # noqa: F401
from gateway.readiness.checks import integrity  # noqa: F401
from gateway.readiness.checks import persistence  # noqa: F401
from gateway.readiness.checks import dependencies  # noqa: F401
from gateway.readiness.checks import features  # noqa: F401
from gateway.readiness.checks import hygiene  # noqa: F401
