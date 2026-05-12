#!/usr/bin/env bash
# Add / rotate a gateway API key for a named owner.
#
# Naming convention:
#   wgk-walacor-<12-hex>          Walacor team key (short, well-known internally)
#   wgk-<label>-<43-base64url>    External / customer keys (256-bit entropy)
#
# Usage:
#   scripts/rotate-key.sh walacor              # team key (24-char tail) — only one allowed
#   scripts/rotate-key.sh acme-corp            # external key for acme-corp (256-bit)
#   scripts/rotate-key.sh acme-corp --revoke   # remove all keys owned by acme-corp
#
# Side effects:
#   ~/Gateway/.env                  — WALACOR_GATEWAY_API_KEYS updated (backup first)
#   ~/Gateway/.keys-registry.yaml   — owner, created_at, last_rotated_at tracked
#   docker compose up -d gateway    — container recreated so new keys take effect
#
# The script is idempotent for the same label: re-running rotates (issues new key,
# revokes previous one for that label). Walacor team key is rotated in place.

set -euo pipefail

LABEL="${1:?usage: rotate-key.sh <label> [--revoke] | rotate-key.sh --revoke-bootstrap}"
ACTION="${2:-rotate}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"
REGISTRY="$REPO_ROOT/.keys-registry.yaml"

[ -f "$ENV_FILE" ] || { echo "ERROR: $ENV_FILE not found" >&2; exit 2; }

# ── Bootstrap revoke path ──────────────────────────────────────────────────
# Removes every key matching the auto-generated bootstrap pattern
# `wgk-<32 hex>` (one dash after the prefix). Managed keys
# (`wgk-<label>-<entropy>` — two+ dashes) are preserved.
#
# Refuses to run if it would leave WALACOR_GATEWAY_API_KEYS empty, since
# that would lock the gateway out — at least one managed key must be in
# place first.
if [[ "$LABEL" == "--revoke-bootstrap" ]]; then
    current_line=$(grep -E '^WALACOR_GATEWAY_API_KEYS=' "$ENV_FILE" | head -1)
    current_keys=${current_line#WALACOR_GATEWAY_API_KEYS=}
    IFS=',' read -ra keys <<< "$current_keys"
    new_keys=()
    revoked=()
    for k in "${keys[@]}"; do
        # bootstrap pattern: wgk-<32 hex>, exactly one dash. Use word count
        # of dash-separated segments rather than regex to keep this portable.
        dash_count=$(awk -F- '{print NF-1}' <<< "$k")
        if [[ "$k" == wgk-* ]] && [[ "$dash_count" -eq 1 ]]; then
            revoked+=("$k")
        else
            new_keys+=("$k")
        fi
    done
    if [[ ${#revoked[@]} -eq 0 ]]; then
        echo "no bootstrap-format keys found — nothing to revoke"; exit 0
    fi
    if [[ ${#new_keys[@]} -eq 0 ]]; then
        echo "ERROR: refusing to revoke — no managed keys would remain." >&2
        echo "  Mint a managed key first: scripts/rotate-key.sh <owner>" >&2
        exit 3
    fi
    cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)"
    sed -i.tmp -E "s|^WALACOR_GATEWAY_API_KEYS=.*$|WALACOR_GATEWAY_API_KEYS=$(IFS=','; echo "${new_keys[*]}")|" "$ENV_FILE"
    rm -f "$ENV_FILE.tmp"
    # Drop matching registry entries
    if [[ -f "$REGISTRY" ]]; then
        for rk in "${revoked[@]}"; do
            python3 - "$REGISTRY" "$rk" <<'PY'
import re, sys
path, key = sys.argv[1], sys.argv[2]
with open(path) as f: lines = f.readlines()
out, skip = [], False
for line in lines:
    if line.strip() == f"- key: {key}":
        skip = True
        continue
    if skip and (line.startswith("    ") or line.startswith("\t")):
        continue
    skip = False
    out.append(line)
with open(path, "w") as f: f.writelines(out)
PY
        done
    fi
    echo "revoked ${#revoked[@]} bootstrap key(s); ${#new_keys[@]} managed key(s) remain"
    cd "$REPO_ROOT" && docker compose up -d gateway >/dev/null 2>&1
    echo "gateway recreated"
    exit 0
fi

# ── Validate label ──────────────────────────────────────────────────────────
if [[ ! "$LABEL" =~ ^[a-z0-9-]+$ ]]; then
    echo "ERROR: label must match [a-z0-9-]+ (got: $LABEL)" >&2
    exit 2
fi

# ── Read current key set ────────────────────────────────────────────────────
current_line=$(grep -E '^WALACOR_GATEWAY_API_KEYS=' "$ENV_FILE" | head -1)
current_keys=${current_line#WALACOR_GATEWAY_API_KEYS=}
IFS=',' read -ra keys <<< "$current_keys"

ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
backup="$ENV_FILE.bak.$(date +%s)"
cp "$ENV_FILE" "$backup"

# ── Helper: rewrite the env line ───────────────────────────────────────────
write_keys() {
    local joined="$1"
    sed -i.tmp -E "s|^WALACOR_GATEWAY_API_KEYS=.*$|WALACOR_GATEWAY_API_KEYS=$joined|" "$ENV_FILE"
    rm -f "$ENV_FILE.tmp"
}

# ── Initialize registry if missing ──────────────────────────────────────────
if [ ! -f "$REGISTRY" ]; then
    cat > "$REGISTRY" <<EOF
# Gateway API key registry. Per-key metadata. Auto-edited by scripts/rotate-key.sh.
# Never check this into git.
schema_version: 1
keys: []
EOF
    chmod 600 "$REGISTRY"
fi

# ── Revoke path ─────────────────────────────────────────────────────────────
if [ "$ACTION" = "--revoke" ]; then
    new_keys=()
    revoked=0
    for k in "${keys[@]}"; do
        if [[ "$k" == "wgk-$LABEL-"* ]]; then
            revoked=$((revoked+1))
            continue
        fi
        new_keys+=("$k")
    done
    if [ $revoked -eq 0 ]; then
        echo "no keys matched 'wgk-$LABEL-*' — nothing to revoke"
        rm -f "$backup"
        exit 0
    fi
    write_keys "$(IFS=','; echo "${new_keys[*]}")"
    # Drop registry entries
    python3 - <<PY
import re
with open("$REGISTRY") as f: lines = f.readlines()
out, skip = [], False
for line in lines:
    if re.match(r'^  - key: wgk-'+r'$LABEL'+'-', line):
        skip = True
        continue
    if skip and (line.startswith('    ') or line.startswith('\t')):
        continue
    skip = False
    out.append(line)
with open("$REGISTRY", "w") as f: f.writelines(out)
PY
    echo "revoked $revoked key(s) for '$LABEL'  (backup: $backup)"
    cd "$REPO_ROOT" && docker compose up -d gateway >/dev/null
    echo "gateway recreated"
    exit 0
fi

# ── Rotate / create path ────────────────────────────────────────────────────
# Generate new key with appropriate entropy
if [ "$LABEL" = "walacor" ]; then
    # Walacor team key — 12-hex tail (48 bits, easily shareable internally)
    new_key="wgk-walacor-$(openssl rand -hex 12)"
    purpose="Walacor team — internal use (dashboard, control-plane CRUD)"
else
    # External / customer — 256 bits, base64url
    entropy=$(openssl rand -base64 32 | tr -d '\n=' | tr '/+' '_-')
    new_key="wgk-$LABEL-$entropy"
    purpose="External tenant: $LABEL"
fi

# Build new key list: drop any existing entry for this label, then append
new_keys=()
rotated=0
for k in "${keys[@]}"; do
    if [[ "$k" == "wgk-$LABEL-"* ]]; then
        rotated=1
        continue
    fi
    new_keys+=("$k")
done
new_keys+=("$new_key")
write_keys "$(IFS=','; echo "${new_keys[*]}")"

# Update registry
python3 - <<PY
import re, sys
new_key  = "$new_key"
label    = "$LABEL"
purpose  = """$purpose"""
ts       = "$ts"
rotated  = bool($rotated)
path     = "$REGISTRY"
with open(path) as f: lines = f.readlines()
# Remove old entries for this label
out, skip = [], False
for line in lines:
    if re.match(r'^  - key: wgk-'+label+'-', line):
        skip = True
        continue
    if skip and (line.startswith('    ') or line.startswith('\t')):
        continue
    skip = False
    out.append(line)
# Append new entry under 'keys:'
entry = (
    f"  - key: {new_key}\n"
    f"    owner: {label}\n"
    f"    purpose: {purpose}\n"
    f"    created_at: {ts}\n"
)
# Find 'keys:' line (or append if missing)
inserted = False
for i, line in enumerate(out):
    if line.rstrip() in ('keys:', 'keys: []'):
        out[i] = 'keys:\n'
        out.insert(i+1, entry)
        inserted = True
        break
if not inserted:
    out.append('keys:\n')
    out.append(entry)
with open(path, 'w') as f: f.writelines(out)
PY
chmod 600 "$REGISTRY" 2>/dev/null || true

# Recreate gateway so the new key is loaded
cd "$REPO_ROOT" && docker compose up -d gateway >/dev/null

echo
echo "── new key ────────────────────────────────────────────────"
echo "  label:   $LABEL"
echo "  key:     $new_key"
echo "  action:  $([ $rotated -eq 1 ] && echo rotated || echo created)"
echo "  backup:  $backup"
echo "  registry: $REGISTRY"
echo "  gateway recreated; new key live now"
echo "───────────────────────────────────────────────────────────"
