"""Container entrypoint that hydrates secrets from AWS SSM Parameter Store
before exec'ing the gateway.

Why this exists
---------------
Before this module landed, every secret the gateway needed (Walacor admin
password, OpenAI/Anthropic provider keys, the gateway's own API keys)
lived in ``.env.gateway`` on the EC2 host. Compose's ``env_file:``
directive expanded those into the container's ``Config.Env``, so anyone
who could run ``docker inspect gateway-app`` on the box could read them
in cleartext. The 2026-05-13 audit flagged this as a P0.

This entrypoint moves the secrets to SSM Parameter Store (SecureString,
KMS-encrypted) and pulls them at boot using the EC2 instance's IAM role.
The values are exported into the gateway process's environment with
``os.environ[...] = ...`` at runtime, so they reach the gateway's
``Settings`` loader exactly as before but never appear in
``docker inspect`` output (``Config.Env`` is frozen at container
creation time).

Activation
----------
Set ``WALACOR_SSM_PREFIX`` (e.g. ``/walacor-gateway``) in the container
env. The entrypoint will fetch every parameter under that prefix that
matches an entry in ``SECRET_MAP``. If ``WALACOR_SSM_PREFIX`` is unset,
the entrypoint exits straight into ``uvicorn`` without touching SSM —
preserves the legacy ``.env.gateway`` path for local dev and for prod
deployments that haven't migrated yet.

Failure modes
-------------
- ``boto3`` not installed         → logs once, skips SSM (gateway will
                                     fail later if secrets aren't
                                     already set via env or .env files).
- No IAM role / SSM unreachable   → per-param error logged to stderr
                                     (no secret values), entrypoint
                                     continues.
- Individual ``ParameterNotFound`` → logged, that one skipped, rest
                                     proceed.

Logging discipline: secret values NEVER appear in logs. Only parameter
names + a "loaded N secrets" summary.
"""
from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger("gateway.entrypoint")

# SSM parameter (suffix) → environment variable name the gateway expects.
# Keep this list aligned with the Settings fields whose
# ``validation_alias`` accepts the right-hand name.
SECRET_MAP: dict[str, str] = {
    "walacor_password": "WALACOR_PASSWORD",
    "walacor_gateway_api_keys": "WALACOR_GATEWAY_API_KEYS",
    "openai_api_key": "OPENAI_API_KEY",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "control_plane_api_key": "WALACOR_CONTROL_PLANE_API_KEY",
}


def _hydrate_from_ssm(prefix: str, region: str) -> int:
    """Fetch each :data:`SECRET_MAP` entry from SSM and set the env var.

    Returns the count of secrets successfully loaded. Never raises:
    individual failures are logged and skipped so a single missing
    parameter doesn't take down the whole container.
    """
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        logger.warning(
            "boto3 not installed; cannot fetch SSM secrets. Install with "
            "`pip install walacor-gateway[secrets]` or set "
            "WALACOR_SSM_PREFIX='' to skip."
        )
        return 0

    ssm = boto3.client("ssm", region_name=region)
    loaded = 0
    for suffix, env_var in SECRET_MAP.items():
        # Don't clobber values the operator has explicitly set via env
        # or .env files — useful escape hatch when a single secret is
        # being rotated out-of-band.
        if os.environ.get(env_var):
            logger.info("entrypoint: %s already in env, skipping SSM fetch", env_var)
            continue
        name = f"{prefix.rstrip('/')}/{suffix}"
        try:
            resp = ssm.get_parameter(Name=name, WithDecryption=True)
            os.environ[env_var] = resp["Parameter"]["Value"]
            loaded += 1
            logger.info("entrypoint: loaded %s from SSM", env_var)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "?")
            logger.warning("entrypoint: SSM fetch failed for %s (%s)", name, code)
        except BotoCoreError as e:
            logger.warning("entrypoint: SSM connection error for %s: %s", name, type(e).__name__)
    return loaded


def _configure_logging() -> None:
    """Bare-minimum stderr logging for the entrypoint itself, ahead of
    the gateway's own logging setup. Stays out of the way of structured
    JSON logging configured by the app's lifespan handler.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    ))
    root = logging.getLogger("gateway.entrypoint")
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = False


def main() -> None:
    _configure_logging()
    prefix = os.environ.get("WALACOR_SSM_PREFIX", "").strip()
    if prefix:
        region = os.environ.get("AWS_REGION", "us-east-1")
        n = _hydrate_from_ssm(prefix, region)
        logger.info("entrypoint: hydrated %d secrets from SSM prefix %s", n, prefix)
    else:
        logger.info(
            "entrypoint: WALACOR_SSM_PREFIX not set; skipping SSM hydration "
            "(reading secrets from env / .env files instead)"
        )

    # exec uvicorn so it inherits PID 1, signals propagate cleanly, and
    # the entrypoint Python process is fully replaced (no double process).
    argv = [
        "uvicorn", "gateway.main:app",
        "--host", "0.0.0.0",
        "--port", "8000",
    ]
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main()
