#!/bin/sh
# Run one command in a throwaway Hermes environment for integration tests.
#
#   dev/scratch-env.sh <command> [args...]
#
# - A fresh HERMES_HOME, Kanban DB and TMPDIR under $CW_SCRATCH (default: a new mktemp dir).
#   The Kanban board path follows the default Hermes root, not HERMES_HOME, so it is pinned too.
# - KUBECONFIG=/dev/null and stub kubectl/oc/helm/argocd/tofu/terraform/pulumi/gh/tea/glab first
#   in PATH: a guard miss runs a stub (exit 99), never a real cluster or forge command.
# - A clean environment (env -i): no tokens or API keys from your shell reach the test.
# - A 2 GiB memory cap through a transient systemd scope when systemd-run is available.
set -eu

[ "$#" -ge 1 ] || { echo "usage: $0 <command> [args...]" >&2; exit 2; }

SCRATCH=${CW_SCRATCH:-$(mktemp -d "${TMPDIR:-/tmp}/cw-scratch.XXXXXX")}
mkdir -p "$SCRATCH/home" "$SCRATCH/kanban" "$SCRATCH/tmp" "$SCRATCH/stubbin"
for b in kubectl oc helm argocd tofu terraform terragrunt pulumi gh tea glab; do
  printf '#!/bin/sh\necho "STUB %s $*" >&2\nexit 99\n' "$b" > "$SCRATCH/stubbin/$b"
  chmod +x "$SCRATCH/stubbin/$b"
done
echo "scratch: $SCRATCH" >&2

set -- env -i \
  HOME="$HOME" \
  HERMES_HOME="$SCRATCH/home" \
  HERMES_KANBAN_DB="$SCRATCH/kanban/kanban.db" \
  HERMES_KANBAN_BOARD=scratch \
  KUBECONFIG=/dev/null \
  PATH="$SCRATCH/stubbin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
  LANG=C.UTF-8 TERM=dumb TMPDIR="$SCRATCH/tmp" \
  ${XDG_RUNTIME_DIR:+XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR"} \
  "$@"

if command -v systemd-run >/dev/null 2>&1 && systemd-run --user --scope -q true 2>/dev/null; then
  exec systemd-run --user --scope -q -p MemoryMax=2G -p MemorySwapMax=0 -- "$@"
fi
exec "$@"
