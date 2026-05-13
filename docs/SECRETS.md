# Secret management — SSM Parameter Store

## Why

Before this design, every secret the gateway needs (Walacor admin password, OpenAI/Anthropic provider keys, the gateway's own API keys) lived in `.env.gateway` on the EC2 host. Compose's `env_file:` directive expanded those into the container's `Config.Env`, so anyone who could run `docker inspect gateway-app` on the box could read them in cleartext. The 2026-05-13 audit flagged this as a P0.

This page describes the SSM-backed flow that replaces it.

## Architecture

```
┌─────────────────────┐                ┌──────────────────────────────┐
│ AWS SSM Param Store │  GetParameter  │ EC2 instance i-09…ca259314d   │
│ /walacor-gateway/*  │ <───────────── │   (IAM role: walacor-gw-ssm)  │
│   walacor_password  │                │   ENTRYPOINT python -m       │
│   openai_api_key    │                │     gateway.entrypoint        │
│   …                 │                │      ↓                       │
└─────────────────────┘                │   exec uvicorn gateway.main  │
                                       └──────────────────────────────┘
```

1. Secrets live as `SecureString` parameters in SSM under a configurable prefix (default `/walacor-gateway`).
2. The EC2 instance has an IAM role attached with `ssm:GetParameter`/`ssm:GetParameters` on that prefix and `kms:Decrypt` on the encryption key.
3. The gateway container starts via `python -m gateway.entrypoint`. The entrypoint fetches every parameter that matches an entry in `SECRET_MAP` (`src/gateway/entrypoint.py`) and exports each into the process environment.
4. The entrypoint then `exec`s `uvicorn`. The exec'd process inherits the env vars but `docker inspect`'s `Config.Env` is frozen at container-creation time and does **not** see them.
5. The gateway's pydantic `Settings` loader reads the env vars exactly as before.

## What's not in SSM (by design)

| Secret | Where it lives | Why |
|---|---|---|
| `WEBUI_SECRET_KEY` (OpenWebUI) | `<webui-volume>/.webui_secret_key` (chmod 600, root-owned) | OpenWebUI is upstream; we don't control its entrypoint. The volume file is its native pattern. |
| `WALACOR_RECORD_SIGNING_KEY` (Ed25519 PEM) | Mounted file at the path in `WALACOR_RECORD_SIGNING_KEY_PATH` | Already file-based; key material doesn't need shape-changing. |

## Operator runbook — first-time setup

### 1. Create the SSM parameters

```bash
PREFIX="/walacor-gateway"
REGION="us-east-1"
KMS_KEY="alias/aws/ssm"   # or your own CMK alias

for entry in \
  "walacor_password=<WALACOR_PW>" \
  "walacor_gateway_api_keys=<comma-separated-wgk-keys>" \
  "openai_api_key=<sk-…>" \
  "anthropic_api_key=<sk-ant-…>" \
  "control_plane_api_key=<wgk-…>" \
; do
  name="${entry%%=*}"
  value="${entry#*=}"
  aws ssm put-parameter --region "$REGION" \
    --name "$PREFIX/$name" \
    --value "$value" \
    --type SecureString \
    --key-id "$KMS_KEY" \
    --overwrite
done
```

Verify:
```bash
aws ssm describe-parameters --region "$REGION" \
  --parameter-filters Key=Name,Option=BeginsWith,Values=$PREFIX
```

### 2. Create the IAM policy and role

Policy `walacor-gateway-ssm-read`:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["ssm:GetParameter", "ssm:GetParameters"],
      "Resource": "arn:aws:ssm:us-east-1:446643828961:parameter/walacor-gateway/*"
    },
    {
      "Effect": "Allow",
      "Action": "kms:Decrypt",
      "Resource": "arn:aws:kms:us-east-1:446643828961:alias/aws/ssm",
      "Condition": {
        "StringEquals": { "kms:ViaService": "ssm.us-east-1.amazonaws.com" }
      }
    }
  ]
}
```

Create the role with EC2 as the trusted principal:
```bash
aws iam create-role --role-name walacor-gateway-ssm \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

aws iam put-role-policy --role-name walacor-gateway-ssm \
  --policy-name walacor-gateway-ssm-read \
  --policy-document file://policy.json

aws iam create-instance-profile --instance-profile-name walacor-gateway-ssm
aws iam add-role-to-instance-profile \
  --instance-profile-name walacor-gateway-ssm --role-name walacor-gateway-ssm
```

Attach to the EC2 instance:
```bash
aws ec2 associate-iam-instance-profile \
  --instance-id i-0968bbb8ca259314d \
  --iam-instance-profile Name=walacor-gateway-ssm
```

Verify from inside the instance:
```bash
ssh ec2-user@<host> aws sts get-caller-identity
# Should now resolve to the role ARN, not "<no role>"
```

### 3. Activate the entrypoint

In `/home/ec2-user/Gateway/.env` on the box:

```ini
# Enable SSM-backed secret hydration
WALACOR_SSM_PREFIX=/walacor-gateway
# AWS_REGION is required for the boto3 client; default us-east-1.
AWS_REGION=us-east-1
```

Then remove the secret entries from `.env.gateway` (they're now redundant and re-introducing the leak we just closed):

```bash
sed -i '/^WALACOR_PASSWORD=/d'           .env.gateway
sed -i '/^WALACOR_GATEWAY_API_KEYS=/d'   .env.gateway
sed -i '/^OPENAI_API_KEY=/d'             .env.gateway
sed -i '/^ANTHROPIC_API_KEY=/d'          .env.gateway
sed -i '/^WALACOR_CONTROL_PLANE_API_KEY=/d' .env.gateway
```

Restart:
```bash
docker compose up -d gateway
docker logs gateway-app 2>&1 | grep entrypoint
# Should see: entrypoint: loaded WALACOR_PASSWORD from SSM   (×5)
```

Verify `docker inspect` no longer leaks:
```bash
docker inspect gateway-app --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep -iE 'password|api_key|secret'
# Should return ZERO sensitive values.
```

### 4. Rotate the secrets

Once SSM is the source of truth, every secret that was in `.env.gateway` should be considered compromised (anyone with sudo on the host has seen it). Generate new values and `put-parameter --overwrite` them. The gateway picks them up on next restart.

## Operator runbook — rotating one secret

```bash
aws ssm put-parameter --region us-east-1 \
  --name /walacor-gateway/openai_api_key \
  --value "sk-new-…" --type SecureString --overwrite
docker compose restart gateway        # entrypoint re-fetches on boot
```

The container restart picks up the new value. There's no hot-reload — secret rotation requires a container restart by design (avoids any in-flight request seeing inconsistent state).

## Local development

Without `WALACOR_SSM_PREFIX` set, the entrypoint logs once and skips SSM. Secrets continue to load from `.env.gateway` as before. No changes needed for local dev.

If you want to test the SSM hydration path locally:

```bash
export WALACOR_SSM_PREFIX=/walacor-gateway-dev
export AWS_REGION=us-east-1
# AWS_PROFILE / AWS_ACCESS_KEY_ID etc. configured for an account with SSM read.
python -m gateway.entrypoint
```

## Failure modes

| Condition | Entrypoint behaviour | Gateway behaviour |
|---|---|---|
| `WALACOR_SSM_PREFIX` unset | Logs once, skips SSM | Reads secrets from env / .env (legacy path) |
| `boto3` not installed (extra not requested) | Logs warning, returns 0 secrets fetched | Same as above |
| No IAM role attached | Each `get_parameter` returns `UnauthorizedOperation`; logged per-param, no values | Fails on first secret-dependent code path (Walacor login, provider auth) |
| Single parameter missing in SSM | Logged as `ParameterNotFound`, others proceed | Fails on that specific dependency (e.g. Anthropic key missing → /v1/messages 502) |
| Operator pre-sets a var via env | Entrypoint sees env value, skips SSM fetch | Uses operator value — escape hatch for emergency rotation |

In all failure cases, secret values are never logged.
