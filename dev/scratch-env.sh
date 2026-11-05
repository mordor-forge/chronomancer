#!/bin/sh
# Run one command in a throwaway Hermes environment for integration tests.
#
#   dev/scratch-env.sh <command> [args...]
#
# What it does:
# - A fresh HERMES_HOME, Kanban DB and TMPDIR under $CW_SCRATCH (default: a new mktemp dir).
#   The Kanban board path follows the default Hermes root, not HERMES_HOME, so it is pinned too.
# - KUBECONFIG=/dev/null and stub kubectl/oc/helm/argocd/tofu/terraform/terragrunt/pulumi/gh/tea/glab
#   first in PATH: a guard miss runs a stub (exit 99), never a real cluster or forge command.
# - git is wrapped: push, send-email, svn dcommit and p4 submit exit 99; global and system git config
#   are ignored, so stored credential helpers are not used; SSH transport is disabled.
# - A clean environment (env -i): no tokens or API keys from your shell reach the test.
# - A 2 GiB memory cap through a transient systemd scope. Without systemd it refuses to run unless
#   CW_ALLOW_UNCAPPED=1 is set.
#
# What it does not do: HOME stays your real home, because Hermes finds its installed dependency
# environment through it. Files under HOME stay readable. Absolute paths (/usr/bin/kubectl) bypass
# the stubs. Use synthetic fixtures only.
set -eu

[ "$#" -ge 1 ] || { echo "usage: $0 <command> [args...]" >&2; exit 2; }

REAL_GIT=$(command -v git || true)
SCRATCH=${CW_SCRATCH:-$(mktemp -d "${TMPDIR:-/tmp}/cw-scratch.XXXXXX")}
mkdir -p "$SCRATCH/home" "$SCRATCH/kanban" "$SCRATCH/tmp" "$SCRATCH/stubbin"
for b in kubectl oc helm argocd tofu terraform terragrunt pulumi gh tea glab; do
  printf '#!/bin/sh\necho "STUB %s $*" >&2\nexit 99\n' "$b" > "$SCRATCH/stubbin/$b"
  chmod +x "$SCRATCH/stubbin/$b"
done
cat > "$SCRATCH/stubbin/git" <<EOF
#!/bin/sh
for a in "\$@"; do
  case "\$a" in
    push|send-email|dcommit|submit) echo "STUB git: '\$a' is refused in the scratch env" >&2; exit 99 ;;
  esac
done
exec ${REAL_GIT:-/bin/false} "\$@"
EOF
chmod +x "$SCRATCH/stubbin/git"
echo "scratch: $SCRATCH" >&2

set -- env -i \
  HOME="$HOME" \
  HERMES_HOME="$SCRATCH/home" \
  HERMES_KANBAN_DB="$SCRATCH/kanban/kanban.db" \
  HERMES_KANBAN_BOARD=scratch \
  KUBECONFIG=/dev/null \
  GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND=/bin/false \
  PATH="$SCRATCH/stubbin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
  LANG=C.UTF-8 TERM=dumb TMPDIR="$SCRATCH/tmp" \
  ${XDG_RUNTIME_DIR:+XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR"} \
  "$@"

if command -v systemd-run >/dev/null 2>&1 && systemd-run --user --scope -q true 2>/dev/null; then
  exec systemd-run --user --scope -q -p MemoryMax=2G -p MemorySwapMax=0 -- "$@"
fi
if [ "${CW_ALLOW_UNCAPPED:-0}" = 1 ]; then
  echo "scratch-env: WARNING: no memory cap (CW_ALLOW_UNCAPPED=1)" >&2
  exec "$@"
fi
echo "scratch-env: refusing to run without a memory cap (no systemd user scope); set CW_ALLOW_UNCAPPED=1 to override" >&2
exit 3
