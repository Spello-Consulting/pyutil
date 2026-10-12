#!/usr/bin/env bash
: '=======================================================
Refresh from github

Gets the latest version of this app from github. To only be used in deployed environments.

Works on both Python projects and plain repos: if a pyproject.toml is present it
reports the project name/version and runs "uv sync" after the refresh; if not
(e.g. a deployed clone of pyutil itself) those Python-specific steps are skipped.
=========================================================='

# set -euo pipefail

print_help() {
  cat <<'EOF'
Usage: gitrefresh.sh [options]

Resets a deployed clone to the latest version of a branch from origin. If the
repo has a pyproject.toml, the project name/version are reported and 'uv sync'
runs after the refresh; without one (e.g. a deployed pyutil itself) those
Python-specific steps are skipped. On Linux, the service(s) listed by
servicectrl.sh are stopped (via sudo) before the refresh.

Options (CLI overrides environment variables):
  --branch <name>               Branch to reset to (env: BRANCH, default: main)
  --allow-dev-override          Allow refresh even if dev markers/patterns match (env: ALLOW_DEV_OVERRIDE=1)
  --block-markers <list>        Colon-separated list of marker files/dirs to block (env: BLOCK_MARKERS)
  --require-markers <list>      Colon-separated list; at least one must exist or abort (env: REQUIRE_MARKERS)
  --block-path-patterns <list>  Colon-separated substrings; if repo path contains any -> block (env: BLOCK_PATH_PATTERNS)
  --require-remote-host <host>  Require origin remote URL to contain this host (env: REQUIRE_REMOTE_HOST)
  --stash-before-refresh <0|1>  If 1, stash tracked changes before refresh (env: STASH_BEFORE_REFRESH, default: 1)
  --service <name>              Stop only this service (must be defined in pyproject service_name) instead of all of them
  --yes                         Non-interactive; skip confirmation prompt
  --help                        Show this help and exit

Examples:
  BRANCH=release -- yes:
    gitrefresh.sh --branch release --yes
  Using environment:
    export ALLOW_DEV_OVERRIDE=1
    gitrefresh.sh --block-markers ".dev_workspace:.development"
EOF
}

# --- Safety & Portability Guards -------------------------------------------
# Environment overrides:
#   ALLOW_DEV_OVERRIDE=1
#   BLOCK_MARKERS=".dev_workspace:.development"
#   REQUIRE_MARKERS=".deployment:.prod"
#   BLOCK_PATH_PATTERNS="pattern1:pattern2"
#   REQUIRE_REMOTE_HOST=github.com
#   STASH_BEFORE_REFRESH=1
#   BRANCH=main
#
# General parameters (defaults, then env, then CLI)
REPO_ROOT="$(pwd)"
BRANCH="${BRANCH:-main}"
PYPROJECT="pyproject.toml"
BLOCK_PATH_PATTERNS="${BLOCK_PATH_PATTERNS:-Development}"
BLOCK_MARKERS_DEFAULT=".dev_workspace:.development:.local_dev:.devcontainer:.ruff_cache"
BLOCK_MARKERS="${BLOCK_MARKERS:-$BLOCK_MARKERS_DEFAULT}"
REQUIRE_MARKERS_DEFAULT=""
REQUIRE_MARKERS="${REQUIRE_MARKERS:-$REQUIRE_MARKERS_DEFAULT}"
STASH_BEFORE_REFRESH="${STASH_BEFORE_REFRESH:-1}"
ALLOW_DEV_OVERRIDE="${ALLOW_DEV_OVERRIDE:-}"
REQUIRE_REMOTE_HOST="${REQUIRE_REMOTE_HOST:-}"
SERVICE_OVERRIDE=""
DEV_CHECK_PREFIX="[Refresh]"

# shellcheck source=git_dev_env_check.sh
source "$(dirname "$0")/git_dev_env_check.sh"

# Parse CLI args (override env/defaults)
YES_MODE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)
      print_help
      exit 0
      ;;
    --yes)
      YES_MODE=1
      shift
      ;;
    --branch)
      BRANCH="$2"
      shift 2
      ;;
    --allow-dev-override)
      ALLOW_DEV_OVERRIDE="1"
      shift
      ;;
    --block-markers)
      BLOCK_MARKERS="$2"
      shift 2
      ;;
    --require-markers)
      REQUIRE_MARKERS="$2"
      shift 2
      ;;
    --block-path-patterns)
      BLOCK_PATH_PATTERNS="$2"
      shift 2
      ;;
    --require-remote-host)
      REQUIRE_REMOTE_HOST="$2"
      shift 2
      ;;
    --stash-before-refresh)
      STASH_BEFORE_REFRESH="$2"
      shift 2
      ;;
    --service)
      SERVICE_OVERRIDE="$2"
      shift 2
      ;;
    *)
      echo "[Refresh] Unknown argument: $1" >&2
      echo "Use --help to see supported options." >&2
      exit 2
      ;;
  esac
done

# 1. Inspect the pyproject.toml file (if present) and extract the project name,
#    current version and service name. A pyproject.toml is optional so that this
#    script can also refresh non-Python repos (e.g. a deployed pyutil itself).
if [ -f "$PYPROJECT" ]; then
    HAS_PYPROJECT=1
    CURRENT_VERSION=$(grep -E '^version *= *"' "$PYPROJECT" | head -1 | sed -E 's/^version *= *"([^"]+)".*$/\1/')
    PROJECT_NAME=$(grep -E '^name *= *"' "$PYPROJECT" | head -1 | sed -E 's/^name *= *"([^"]+)".*$/\1/')
else
    HAS_PYPROJECT=0
    echo "[Refresh] No $PYPROJECT found; refreshing repo without a Python sync step."
    CURRENT_VERSION=""
    PROJECT_NAME=""
fi

if [[ "$HAS_PYPROJECT" == "1" && -z "$CURRENT_VERSION" ]]; then
    echo "Error: version not defined in $PYPROJECT."
    exit 1
fi

# 2. Ensure we are inside a git working tree
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "[Refresh] Error: Not inside a git working tree." >&2
  exit 3
fi

# 3. Determine repo root
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
if [[ -z "$REPO_ROOT" ]]; then
  echo "[Refresh] Error: Unable to determine repo root." >&2
  exit 3
fi

# 4-7. Dev-environment guard checks (shared with gitpush.sh)
check_dev_environment
remote_url="$REMOTE_URL"

# 8. Find uv reliably (systemd often has a minimal PATH). Only required when a
#    pyproject.toml is present, since that drives the 'uv sync' step below.
UVCmd=""
if [[ "$HAS_PYPROJECT" == "1" ]]; then
  if command -v uv >/dev/null 2>&1; then
    UVCmd="$(command -v uv)"
  elif [ -x "$HOME/.local/bin/uv" ]; then
    UVCmd="$HOME/.local/bin/uv"
  else
    echo "[Refresh from Github] Error: 'uv' not found in PATH or at \$HOME/.local/bin/uv" >&2
    exit 1
  fi
fi

if [[ "$HAS_PYPROJECT" == "1" ]]; then
  echo "Project $PROJECT_NAME (v$CURRENT_VERSION): Starting refresh from branch '$BRANCH'"
else
  echo "Repo $(basename "$REPO_ROOT"): Starting refresh from branch '$BRANCH'"
fi
# servicectrl.sh needs systemd and root, so it is only used on Linux.
SERVICECTRL="$(dirname "$0")/servicectrl.sh"
SERVICECTRL_ARGS=()
SERVICES=()
if [[ "$(uname)" == "Linux" ]]; then
  if [[ -n "$SERVICE_OVERRIDE" ]]; then
    SERVICECTRL_ARGS=(--service "$SERVICE_OVERRIDE")
    SERVICES=("$SERVICE_OVERRIDE")
  elif [[ "$HAS_PYPROJECT" == "1" ]]; then
    if ! SERVICE_LIST="$(sudo "$SERVICECTRL" list)"; then
      echo "[Refresh] Error: unable to list services via servicectrl.sh." >&2
      exit 1
    fi
    while IFS= read -r svc; do
      [[ -n "$svc" && "$svc" != "None" ]] && SERVICES+=("$svc")
    done <<< "$SERVICE_LIST"
  fi
fi
if [[ ${#SERVICES[@]} -gt 0 ]]; then
  echo "The following service(s) will be stopped before the refresh: ${SERVICES[*]}"
fi

if [[ "$YES_MODE" -ne 1 ]]; then
  read -p "Enter Y to continue, any other key to abort: " CONFIRM
  if [[ "$CONFIRM" != "Y" && "$CONFIRM" != "y" ]]; then
      echo "Aborted."
      exit 0
  fi
fi

if [[ ${#SERVICES[@]} -gt 0 ]]; then
  echo "[Refresh] Stopping service(s) before refresh: ${SERVICES[*]}"
  sudo "$SERVICECTRL" "${SERVICECTRL_ARGS[@]}" stop

  sleep 3

  for svc in "${SERVICES[@]}"; do
    if systemctl is-active --quiet "$svc"; then
      echo "[Refresh] Error: Service '$svc' is still running after stop command." >&2
      exit 1
    fi
  done
fi

# Stash tracked changes only (no -u) based on setting
if [[ "$STASH_BEFORE_REFRESH" == "1" ]]; then
  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "[Refresh] Stashing tracked changes..."
    git stash push -m "pre-refresh $(date -u +%Y%m%dT%H%M%SZ)" >/dev/null 2>&1 \
      || echo "[Refresh] Warning: git stash failed."
  fi
fi

# Fetch before branch switching so remote branches are discoverable.
# A failed fetch must be fatal: if we continued, the later 'reset --hard
# origin/$BRANCH' would silently pin the tree to the last-fetched (stale)
# ref and report success, hiding auth/network failures (see issue #16).
echo "[Refresh] Fetching origin (including branch '$BRANCH')..."
if ! git fetch origin "$BRANCH" --tags --force; then
  echo "[Refresh] Warning: fetch of specific branch failed; retrying with fetch-all."
  if ! git fetch origin --tags --force; then
    echo "[Refresh] Error: fetch from origin failed; aborting to avoid deploying stale code." >&2
    echo "[Refresh] Check remote access (e.g. credentials/network) and retry." >&2
    exit 7
  fi
fi

# Ensure we are on the requested branch; create local tracking if only remote exists
current_branch="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$current_branch" != "$BRANCH" ]]; then
  echo "[Refresh] Checking out branch '$BRANCH' (was on '$current_branch')."
  if git rev-parse --verify "refs/heads/$BRANCH" >/dev/null 2>&1; then
    # Local branch exists
    if ! git checkout "$BRANCH"; then
      echo "[Refresh] Error: Failed to checkout existing local branch '$BRANCH'." >&2
      exit 6
    fi
  elif git rev-parse --verify "refs/remotes/origin/$BRANCH" >/dev/null 2>&1; then
    # Create local branch tracking remote
    if ! git checkout -B "$BRANCH" "origin/$BRANCH"; then
      echo "[Refresh] Error: Remote branch 'origin/$BRANCH' exists, but checkout -B failed." >&2
      exit 6
    fi
  else
    echo "[Refresh] Error: Branch '$BRANCH' not found locally or on origin." >&2
    echo "[Refresh] Tip: Verify the branch name, e.g. 'git branch -r --list origin/*'." >&2
    exit 6
  fi
fi

echo "[Refresh] Resetting '$BRANCH' to origin/$BRANCH..."
git reset --hard "origin/$BRANCH"

if [[ "$HAS_PYPROJECT" == "1" ]]; then
  echo "[Refresh] Running 'uv sync'..."
  "$UVCmd" sync
else
  echo "[Refresh] No pyproject.toml; skipping 'uv sync'."
fi

echo "[Refresh] Done."
