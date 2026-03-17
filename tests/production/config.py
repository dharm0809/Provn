"""Shared config for all production tier tests.

Designed to run ON the EC2 instance (localhost).
Override via env vars if running from elsewhere.
"""
import json
import os
from pathlib import Path

GATEWAY_IP = os.environ.get("GATEWAY_IP", "localhost")
GATEWAY_PORT = os.environ.get("GATEWAY_PORT", "8002")
BASE_URL = f"http://{GATEWAY_IP}:{GATEWAY_PORT}"
CHAT_URL = f"{BASE_URL}/v1/chat/completions"
HEALTH_URL = f"{BASE_URL}/health"
METRICS_URL = f"{BASE_URL}/metrics"
LINEAGE_URL = f"{BASE_URL}/v1/lineage"

# Set GATEWAY_API_KEY env var after adding a key to .env and restarting gateway
API_KEY = os.environ.get("GATEWAY_API_KEY", "")
HEADERS = {"Content-Type": "application/json"}
if API_KEY:
    HEADERS["X-API-Key"] = API_KEY

MODEL = os.environ.get("GATEWAY_MODEL", "qwen3:1.7b")
ARTIFACTS_DIR = Path(__file__).parent.parent / "artifacts"


def save_artifact(name: str, data: dict) -> Path:
    ARTIFACTS_DIR.mkdir(exist_ok=True)
    path = ARTIFACTS_DIR / f"{name}.json"
    path.write_text(json.dumps(data, indent=2))
    print(f"  Artifact saved: {path}")
    return path
