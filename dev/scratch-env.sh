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
# - Optional pinned core: CW_HERMES_CORE=<hermes-agent checkout with a uv-synced .venv> puts that
#   checkout's `hermes` first in PATH (after the stubs), drops ~/.local/bin from PATH and sets HOME to
#   a scratch directory too. Use it when the installed Hermes is older than requires_hermes, or to
#   test against an exact release. Set it up once (uv only, nothing global changes):
#     git clone --filter=blob:none https://github.com/NousResearch/hermes-agent <dir>
#     git -C <dir> checkout <release tag or commit>
#     (cd <dir> && uv sync --locked --extra messaging --python 3.14)
#
# What it does not do without CW_HERMES_CORE: HOME stays your real home, because an installed Hermes
# finds its dependency environment through it. Files under HOME stay readable. In both modes,
# absolute paths (/usr/bin/kubectl) bypass the stubs. Use synthetic fixtures only.
set -eu

[ "$#" -ge 1 ] || { echo "usage: $0 <command> [args...]" >&2; exit 2; }

REAL_GIT=$(command -v git || true)
case "$REAL_GIT" in *"'"*) echo "scratch-env: git path contains a single quote; refusing" >&2; exit 2 ;; esac
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
exec '${REAL_GIT:-/bin/false}' "\$@"
EOF
chmod +x "$SCRATCH/stubbin/git"
RUN_HOME=$HOME
RUN_PATH="$SCRATCH/stubbin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
if [ -n "${CW_HERMES_CORE:-}" ]; then
  case "$CW_HERMES_CORE" in
    /*) ;;
    *) echo "scratch-env: CW_HERMES_CORE must be an absolute path" >&2; exit 2 ;;
  esac
  case "$CW_HERMES_CORE" in *:*|*"'"*) echo "scratch-env: CW_HERMES_CORE contains ':' or a quote; refusing" >&2; exit 2 ;; esac
  [ -x "$CW_HERMES_CORE/.venv/bin/hermes" ] || { echo "scratch-env: no $CW_HERMES_CORE/.venv/bin/hermes (run uv sync there first)" >&2; exit 2; }
  RUN_HOME="$SCRATCH/userhome"
  mkdir -p "$RUN_HOME"
  RUN_PATH="$SCRATCH/stubbin:$CW_HERMES_CORE/.venv/bin:/usr/local/bin:/usr/bin:/bin"
  echo "scratch: pinned core $CW_HERMES_CORE ($(git -C "$CW_HERMES_CORE" rev-parse --short HEAD 2>/dev/null || echo unknown)), HOME=$RUN_HOME" >&2
fi
echo "scratch: $SCRATCH" >&2

set -- env -i \
  HOME="$RUN_HOME" \
  HERMES_HOME="$SCRATCH/home" \
  HERMES_KANBAN_DB="$SCRATCH/kanban/kanban.db" \
  HERMES_KANBAN_BOARD=scratch \
  KUBECONFIG=/dev/null \
  GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND=/bin/false \
  PATH="$RUN_PATH" \
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
