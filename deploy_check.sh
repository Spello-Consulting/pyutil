#!/usr/bin/env bash
: '=======================================================
Deployment readiness check

Checks that this host, and the project in the current folder, are set up for
auto-deployment as described in the deployment guides
(https://spello-consulting.github.io/automation/deployment/). Read-only: it
never changes anything, and prints the fix for each problem it finds.

Run it from the project folder, using the deployed pyutil:

    cd ~/scripts/PowerController
    ~/scripts/pyutil/deploy_check.sh

The checks are in deploy_check.py; this wrapper finds uv and runs it with the
dependencies declared in that file. Run with --help for options.
=========================================================='

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

UV=""
for candidate in "$(command -v uv 2>/dev/null)" "$HOME/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
  if [[ -n "$candidate" && -x "$candidate" ]]; then
    UV="$candidate"
    break
  fi
done

if [[ -z "$UV" ]]; then
  echo "  FAIL  uv is installed" >&2
  echo "        uv was not found on PATH or in ~/.local/bin, /opt/homebrew/bin or /usr/local/bin," >&2
  echo "        and it is needed both for deploys and to run this check." >&2
  if [[ "$(uname -s)" == "Darwin" ]]; then
    echo "        Fix: brew install uv" >&2
  else
    echo "        Fix: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  fi
  exit 1
fi

exec "$UV" run --quiet --script "$SCRIPT_DIR/deploy_check.py" "$@"
