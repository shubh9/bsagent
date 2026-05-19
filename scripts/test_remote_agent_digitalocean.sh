#!/usr/bin/env bash
# Smoke-test running bsagent on a fresh DigitalOcean Droplet.
#
# Required env, usually loaded from .env:
#   DIGITALOCEAN_ACCESS_TOKEN
#   DO_SSH_KEY_ID
#   GITHUB_TOKEN
#
# Useful overrides:
#   DO_REGION=nyc3
#   DO_SIZE=s-2vcpu-4gb
#   DO_IMAGE=ubuntu-24-04-x64
#   BSAGENT_REPO_URL=https://github.com/owner/bsagent.git
#   BSAGENT_BRANCH=main
#   REMOTE_PROMPT='Run pwd, list files, and print python --version.'
#   KEEP_REMOTE_AGENT=1

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

require() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "ERROR: missing required env var: $name" >&2
    exit 1
  fi
}

require DIGITALOCEAN_ACCESS_TOKEN
require DO_SSH_KEY_ID
require GITHUB_TOKEN
require OPENAI_API_KEY

if ! command -v doctl >/dev/null 2>&1; then
  echo "ERROR: doctl is not installed. Install with: brew install doctl" >&2
  exit 1
fi

if ! command -v ssh >/dev/null 2>&1; then
  echo "ERROR: ssh is not available on PATH" >&2
  exit 1
fi

DO_REGION="${DO_REGION:-nyc3}"
DO_SIZE="${DO_SIZE:-s-2vcpu-4gb}"
DO_IMAGE="${DO_IMAGE:-ubuntu-24-04-x64}"
BSAGENT_BRANCH="${BSAGENT_BRANCH:-$(git -C "$ROOT_DIR" branch --show-current 2>/dev/null || echo main)}"
REMOTE_PROMPT="${REMOTE_PROMPT:-Run pwd, list files, print python --version, and summarize what you found.}"
KEEP_REMOTE_AGENT="${KEEP_REMOTE_AGENT:-0}"
REMOTE_WORKDIR="/workspace/bsagent"

default_repo_url="$(git -C "$ROOT_DIR" remote get-url origin 2>/dev/null || true)"
if [[ "$default_repo_url" =~ ^git@github.com:(.+)\.git$ ]]; then
  default_repo_url="https://github.com/${BASH_REMATCH[1]}.git"
fi
BSAGENT_REPO_URL="${BSAGENT_REPO_URL:-$default_repo_url}"

if [[ -z "$BSAGENT_REPO_URL" ]]; then
  echo "ERROR: BSAGENT_REPO_URL is not set and no git origin remote was found" >&2
  exit 1
fi

name="bsagent-smoke-$(date +%Y%m%d%H%M%S)"
droplet_id=""
ip=""

cleanup() {
  local exit_code=$?
  if [[ -n "$droplet_id" && "$KEEP_REMOTE_AGENT" != "1" ]]; then
    echo
    echo "Cleaning up Droplet $droplet_id ($name)..."
    doctl compute droplet delete "$droplet_id" --force >/dev/null || true
  elif [[ -n "$droplet_id" ]]; then
    echo
    echo "Keeping Droplet for debugging:"
    echo "  id: $droplet_id"
    echo "  ip: $ip"
    echo "  ssh: ssh root@$ip"
  fi
  exit "$exit_code"
}
trap cleanup EXIT

echo "Creating DigitalOcean Droplet..."
echo "  name:   $name"
echo "  region: $DO_REGION"
echo "  size:   $DO_SIZE"
echo "  image:  $DO_IMAGE"

create_output="$(
  doctl compute droplet create "$name" \
    --size "$DO_SIZE" \
    --image "$DO_IMAGE" \
    --region "$DO_REGION" \
    --ssh-keys "$DO_SSH_KEY_ID" \
    --tag-names bsagent,bsagent-smoke \
    --wait \
    --format ID,PublicIPv4 \
    --no-header
)"

droplet_id="$(awk '{print $1}' <<<"$create_output")"
ip="$(awk '{print $2}' <<<"$create_output")"

if [[ -z "$droplet_id" || -z "$ip" ]]; then
  echo "ERROR: could not parse Droplet ID/IP from doctl output:" >&2
  echo "$create_output" >&2
  exit 1
fi

echo "Droplet is ready:"
echo "  id: $droplet_id"
echo "  ip: $ip"

echo "Waiting for SSH..."
for _ in {1..60}; do
  if ssh \
    -o BatchMode=yes \
    -o ConnectTimeout=5 \
    -o StrictHostKeyChecking=accept-new \
    "root@$ip" 'true' >/dev/null 2>&1; then
    break
  fi
  sleep 5
done

ssh \
  -o BatchMode=yes \
  -o ConnectTimeout=10 \
  -o StrictHostKeyChecking=accept-new \
  "root@$ip" 'true' >/dev/null

echo "Running remote bsagent smoke test..."

remote_env="$(
  python3 - "$GITHUB_TOKEN" "$OPENAI_API_KEY" "$BSAGENT_REPO_URL" "$BSAGENT_BRANCH" "$REMOTE_PROMPT" "$REMOTE_WORKDIR" <<'PY'
import shlex
import sys

keys = [
    "GITHUB_TOKEN",
    "OPENAI_API_KEY",
    "BSAGENT_REPO_URL",
    "BSAGENT_BRANCH",
    "REMOTE_PROMPT",
    "REMOTE_WORKDIR",
]
for key, value in zip(keys, sys.argv[1:]):
    print(f"export {key}={shlex.quote(value)}")
PY
)"

{
  printf '%s\n' "$remote_env"
  cat <<'REMOTE_SCRIPT'
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

echo "Waiting for cloud-init and apt locks..."
cloud-init status --wait || true
while fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/cache/apt/archives/lock >/dev/null 2>&1; do
  sleep 3
done

echo "Installing system dependencies..."
apt-get update -y
apt-get install -y git python3 python3-venv python3-pip build-essential ripgrep tmux

mkdir -p /workspace
cd /workspace

git config --global credential.helper 'store --file /root/.git-credentials'
printf 'https://x-access-token:%s@github.com\n' "$GITHUB_TOKEN" > /root/.git-credentials
chmod 0600 /root/.git-credentials

echo "Cloning bsagent..."
rm -rf "$REMOTE_WORKDIR"
git clone --branch "$BSAGENT_BRANCH" "$BSAGENT_REPO_URL" "$REMOTE_WORKDIR"

cd "$REMOTE_WORKDIR"
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

export AGENT_MODEL="${AGENT_MODEL:-gpt-5.5}"
export AGENT_WORKDIR="$REMOTE_WORKDIR"

echo
echo "Starting bsagent..."
echo "Prompt: $REMOTE_PROMPT"
echo

set +e
timeout 180s python3 agent.py "$REMOTE_PROMPT" > /tmp/bsagent-remote.stdout 2> /tmp/bsagent-remote.stderr
status=$?
set -e

echo
echo "===== bsagent stdout ====="
cat /tmp/bsagent-remote.stdout || true
echo
echo "===== bsagent stderr ====="
cat /tmp/bsagent-remote.stderr || true
echo
echo "===== remote exit status: $status ====="

exit "$status"
REMOTE_SCRIPT
} | ssh \
  -o BatchMode=yes \
  -o ConnectTimeout=10 \
  -o StrictHostKeyChecking=accept-new \
  "root@$ip" 'bash -s'

echo
echo "Remote bsagent smoke test completed successfully."
