#!/bin/sh
# Scratch-only phone check. The bot's commands, menu and webhook are never changed.
# Required: CW_HERMES_CORE, CW_MINIAPP_URL. Optional: CW_DEMO_TOKEN_FILE, CW_KIND_KUBECONFIG.
# --smoke initializes the demo bot, proves loopback HTTP, then exits without polling or sending.
set -eu
umask 077
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
: "${CW_HERMES_CORE:?Set CW_HERMES_CORE to the pinned Hermes checkout}"
: "${CW_MINIAPP_URL:?Set CW_MINIAPP_URL to the public HTTPS URL}"
TOKEN_FILE=${CW_DEMO_TOKEN_FILE:-$ROOT/../.secrets/demo-bot.env}
[ -f "$TOKEN_FILE" ] || { echo 'Demo token file missing' >&2; exit 2; }
# Always a new profile; an old pairing/allow-list is never reused accidentally.
CW_SCRATCH=$(mktemp -d /tmp/cw-scratch.demo.XXXXXX)
export CW_SCRATCH
exec "$ROOT/dev/scratch-env.sh" env \
  CW_DEMO_SCRATCH="$CW_SCRATCH" CW_DEMO_URL="$CW_MINIAPP_URL" \
  CW_DEMO_TOKEN_FILE="$TOKEN_FILE" CW_DEMO_SOURCE="$ROOT" \
  uv run --no-project --python "$CW_HERMES_CORE/.venv/bin/python" "$ROOT/dev/run_demo.py" "$@"
