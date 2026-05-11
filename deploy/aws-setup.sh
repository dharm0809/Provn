#!/usr/bin/env bash
# Walacor Gateway — end-to-end install for AWS EC2 (Amazon Linux 2023 / Ubuntu)
#
# What this does:
#   1. Installs Docker + Docker Compose plugin (if missing)
#   2. Walks you through .env setup (Walacor backend, provider keys, ports)
#      OR uses existing .env if present. Generates random API keys + WebUI secret.
#   3. Pulls/builds the gateway image
#   4. Runs scripts/provision-walacor.sh — creates Walacor ETIDs (idempotent)
#   5. docker compose up -d
#   6. Runs scripts/verify-install.sh — fails loudly if anything is wrong
#   7. Pulls a default Ollama model (llama3.1:8b)
#
# Usage:
#   ssh ec2-user@<IP>
#   git clone <repo> Gateway && cd Gateway
#   bash deploy/aws-setup.sh
#
# Flags:
#   --non-interactive    Don't prompt; require a complete .env up front
#   --skip-model-pull    Skip the Ollama model download
#   --image <ref>        Override GATEWAY_IMAGE (ECR or other registry)
#
# Idempotent: safe to re-run. Existing .env is preserved unless missing fields
# are detected and you opt into the interactive prompts.

set -euo pipefail

INTERACTIVE=1
SKIP_MODEL=0
IMAGE_OVERRIDE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --non-interactive) INTERACTIVE=0; shift;;
        --skip-model-pull) SKIP_MODEL=1; shift;;
        --image) IMAGE_OVERRIDE="$2"; shift 2;;
        *) echo "Unknown flag: $1"; exit 2;;
    esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "=========================================="
echo "  Walacor Gateway — Install"
echo "=========================================="

# ── 1. OS detection ────────────────────────────────────────────────────────
if [[ ! -f /etc/os-release ]]; then
    echo "ERROR: cannot detect OS." >&2; exit 1
fi
. /etc/os-release
OS_ID="$ID"
echo "OS: $OS_ID $VERSION_ID"

# ── 2. Docker + compose plugin ─────────────────────────────────────────────
echo
echo "[1/7] Docker"
if command -v docker &>/dev/null; then
    echo "  already installed: $(docker --version)"
else
    case "$OS_ID" in
        amzn)
            sudo dnf update -y -q
            sudo dnf install -y -q docker
            sudo systemctl enable --now docker
            sudo usermod -aG docker "$USER"
            ;;
        ubuntu)
            sudo apt-get update -qq
            sudo apt-get install -y -qq ca-certificates curl
            sudo install -m 0755 -d /etc/apt/keyrings
            curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo tee /etc/apt/keyrings/docker.asc >/dev/null
            echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
                | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
            sudo apt-get update -qq
            sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
            sudo usermod -aG docker "$USER"
            ;;
        *)
            echo "ERROR: unsupported OS '$OS_ID'. Install Docker manually then re-run." >&2
            exit 1
            ;;
    esac
    echo "  installed: $(sudo docker --version)"
fi

# Plugin presence
if ! docker compose version &>/dev/null && ! sudo docker compose version &>/dev/null; then
    echo "  installing compose plugin..."
    case "$OS_ID" in
        amzn)
            COMPOSE_VERSION=$(curl -s https://api.github.com/repos/docker/compose/releases/latest | grep '"tag_name"' | head -1 | cut -d'"' -f4)
            sudo mkdir -p /usr/local/lib/docker/cli-plugins
            sudo curl -sSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-$(uname -s)-$(uname -m)" \
                -o /usr/local/lib/docker/cli-plugins/docker-compose
            sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
            ;;
    esac
fi

DOCKER="docker"
docker info &>/dev/null 2>&1 || DOCKER="sudo docker"

# ── 3. .env preparation ────────────────────────────────────────────────────
echo
echo "[2/7] .env"

ENV_FILE="$REPO_ROOT/.env"
ENV_EXAMPLE="$REPO_ROOT/.env.example"

# Generate one wgk-* key (32 hex chars)
gen_wgk_key()    { echo "wgk-$(openssl rand -hex 16)"; }
gen_webui_secret() { openssl rand -base64 32 | tr -d '\n=' | tr '/+' '_-'; }

prompt() {
    # prompt <var> <label> [secret]
    local var="$1" label="$2" secret="${3:-}"
    local current="${!var:-}"
    local shown="$current"
    [[ -n "$secret" && -n "$shown" ]] && shown="<redacted>"
    [[ -z "$current" ]] && shown="(empty)"
    local input
    if [[ -n "$secret" ]]; then
        read -r -p "  $label [$shown]: " -s input; echo
    else
        read -r -p "  $label [$shown]: " input
    fi
    [[ -n "$input" ]] && eval "$var=\"\$input\""
}

if [[ -f "$ENV_FILE" ]]; then
    echo "  $ENV_FILE exists — preserving (use --non-interactive to skip prompts)"
    # shellcheck disable=SC1090
    set -a; source <(grep -E '^[A-Z_]+=' "$ENV_FILE" | sed 's/=\(.*\)/="\1"/'); set +a
else
    echo "  $ENV_FILE missing — creating from template"
    cp "$ENV_EXAMPLE" "$ENV_FILE"
fi

if [[ $INTERACTIVE -eq 1 && -t 0 ]]; then
    echo "  Press Enter to keep current values, or type new values:"

    # Tenant + image
    : "${WALACOR_GATEWAY_TENANT_ID:=walacor-prod}"
    prompt WALACOR_GATEWAY_TENANT_ID "Tenant ID"

    GATEWAY_IMAGE="${IMAGE_OVERRIDE:-${GATEWAY_IMAGE:-}}"
    prompt GATEWAY_IMAGE "Gateway image (blank = build locally)"

    # API keys — auto-generate if empty
    if [[ -z "${WALACOR_GATEWAY_API_KEYS:-}" ]]; then
        WALACOR_GATEWAY_API_KEYS="$(gen_wgk_key),$(gen_wgk_key)"
        echo "  generated WALACOR_GATEWAY_API_KEYS: $WALACOR_GATEWAY_API_KEYS"
    fi
    WEBUI_GATEWAY_API_KEY="${WEBUI_GATEWAY_API_KEY:-${WALACOR_GATEWAY_API_KEYS%%,*}}"

    if [[ -z "${WEBUI_SECRET_KEY:-}" || "${WEBUI_SECRET_KEY}" == "change-me-to-a-long-random-string" ]]; then
        WEBUI_SECRET_KEY="$(gen_webui_secret)"
        echo "  generated WEBUI_SECRET_KEY"
    fi

    # Walacor backend
    : "${WALACOR_SERVER:=https://sandbox.walacor.com/api}"
    prompt WALACOR_SERVER       "Walacor server URL"
    prompt WALACOR_USERNAME     "Walacor username"
    prompt WALACOR_PASSWORD     "Walacor password" secret

    # Provider keys
    prompt OPENAI_API_KEY       "OpenAI API key (blank if not using OpenAI)" secret
    prompt ANTHROPIC_API_KEY    "Anthropic API key (blank if not using Anthropic)" secret

    # Ports
    : "${GATEWAY_PORT:=8002}"
    prompt GATEWAY_PORT         "Gateway host port"
    : "${OPENWEBUI_PORT:=3000}"
    prompt OPENWEBUI_PORT       "OpenWebUI host port"
fi

# Write back resolved values (preserves any keys not in our prompt set)
write_kv() {
    local k="$1" v="${2:-}"
    if grep -qE "^${k}=" "$ENV_FILE" 2>/dev/null; then
        # macOS-compatible sed in-place
        sed -i.bak -E "s|^${k}=.*$|${k}=${v}|" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
    else
        echo "${k}=${v}" >> "$ENV_FILE"
    fi
}
write_kv WALACOR_GATEWAY_TENANT_ID  "${WALACOR_GATEWAY_TENANT_ID:-}"
write_kv WALACOR_GATEWAY_API_KEYS   "${WALACOR_GATEWAY_API_KEYS:-}"
write_kv WEBUI_GATEWAY_API_KEY      "${WEBUI_GATEWAY_API_KEY:-}"
write_kv WEBUI_SECRET_KEY           "${WEBUI_SECRET_KEY:-}"
write_kv WALACOR_SERVER             "${WALACOR_SERVER:-}"
write_kv WALACOR_USERNAME           "${WALACOR_USERNAME:-}"
write_kv WALACOR_PASSWORD           "${WALACOR_PASSWORD:-}"
write_kv OPENAI_API_KEY             "${OPENAI_API_KEY:-}"
write_kv ANTHROPIC_API_KEY          "${ANTHROPIC_API_KEY:-}"
write_kv GATEWAY_PORT               "${GATEWAY_PORT:-8002}"
write_kv OPENWEBUI_PORT             "${OPENWEBUI_PORT:-3000}"
[[ -n "${GATEWAY_IMAGE:-}" ]] && write_kv GATEWAY_IMAGE "$GATEWAY_IMAGE"

chmod 600 "$ENV_FILE"
echo "  .env written (mode 0600)"

# ── 4. Image: pull if pinned, else build ───────────────────────────────────
echo
echo "[3/7] Image"
if [[ -n "${GATEWAY_IMAGE:-}" ]]; then
    echo "  pulling $GATEWAY_IMAGE"
    $DOCKER pull "$GATEWAY_IMAGE" || {
        echo "  ERROR: image pull failed. If this is ECR, run 'aws ecr get-login-password ... | docker login ...' first." >&2
        exit 1
    }
else
    echo "  building from deploy/Dockerfile"
    $DOCKER compose build gateway
fi

# ── 5. Provision Walacor schemas (idempotent) ──────────────────────────────
echo
echo "[4/7] Walacor schemas"
if [[ -n "${WALACOR_SERVER:-}" && -n "${WALACOR_USERNAME:-}" && -n "${WALACOR_PASSWORD:-}" ]]; then
    bash "$REPO_ROOT/scripts/provision-walacor.sh" "$ENV_FILE" || {
        echo "  ERROR: schema provisioning failed. Fix and re-run before going live." >&2
        exit 1
    }
else
    echo "  Walacor creds not set — skipping (records will stay in local WAL only)"
fi

# ── 6. Compose up ──────────────────────────────────────────────────────────
echo
echo "[5/7] docker compose up"
$DOCKER compose up -d
sleep 3

# ── 7. Verify ──────────────────────────────────────────────────────────────
echo
echo "[6/7] verify-install"
bash "$REPO_ROOT/scripts/verify-install.sh" "$ENV_FILE" || {
    echo
    echo "  Install completed with gaps — see above. Run 'docker compose logs gateway' for details." >&2
    exit 1
}

# ── 8. Pull default Ollama model ───────────────────────────────────────────
if [[ $SKIP_MODEL -eq 0 ]]; then
    echo
    echo "[7/7] Default Ollama model (llama3.1:8b)"
    $DOCKER exec gateway-ollama ollama pull llama3.1:8b 2>&1 | tail -3 || \
        echo "  model pull failed — pull manually later: docker exec gateway-ollama ollama pull <model>"
else
    echo
    echo "[7/7] Skipping model pull (--skip-model-pull)"
fi

PUBLIC_IP=$(curl -sf --max-time 2 http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo "<this-host>")

echo
echo "=========================================="
echo "  Install complete"
echo "=========================================="
echo
echo "  Chat UI:           http://${PUBLIC_IP}:${OPENWEBUI_PORT:-3000}"
echo "  Gateway health:    http://${PUBLIC_IP}:${GATEWAY_PORT:-8002}/health"
echo "  Lineage dashboard: http://${PUBLIC_IP}:${GATEWAY_PORT:-8002}/lineage/"
echo
echo "  API keys (record these):"
echo "    WALACOR_GATEWAY_API_KEYS=${WALACOR_GATEWAY_API_KEYS:-<see .env>}"
echo
echo "  Re-verify any time:  make verify-install"
echo "  Upgrade image:       scripts/pull-and-deploy.sh"
echo "=========================================="
