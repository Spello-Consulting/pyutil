#!/usr/bin/env bash
# help.sh
# Print a short description of each utility script in this folder.
#
# Run with --help for options.

set -uo pipefail

PROG="$(basename "$0")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<EOF
Usage: $PROG [OPTIONS]

Print a short description of each utility script (*.sh) in this folder.

Options:
  -h, --help    Show this help and exit.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "$PROG: unknown option '$1'" >&2
      usage >&2
      exit 1
      ;;
  esac
done

# Known scripts and their descriptions. Scripts not listed here fall back to
# the first descriptive comment line found in the file.
declare -A DESCRIPTIONS=(
  [check_git_changes.sh]="Scan the subfolders of a dev directory and report, for each Git repo found, any local uncommitted changes and any remote changes not yet pulled."
  [git_dev_env_check.sh]="Shared library (not run directly) sourced by gitrefresh.sh and gitpush.sh to refuse running against a dev checkout instead of a deployed clone."
  [gitpush.sh]="Commit and push locally modified, already-tracked files back to the remote repo. For use in deployed environments, e.g. to push a modified config file back."
  [gitrefresh.sh]="Reset a deployed clone to the latest version from GitHub, optionally stopping/starting a service around the refresh. Works on Python projects (runs 'uv sync') and plain repos without a pyproject.toml. For use in deployed environments."
  [help.sh]="Print a short description of each utility script in this folder (this script)."
  [launch.sh]="Application launcher. Resolves the project home directory, optionally injects secrets via 1Password's 'op run', then launches the app with Python/uv."
  [new_project.sh]="Scaffold a new project from a template, optionally creating and linking a GitHub remote repository."
  [nopy_release.sh]="Stage, commit, tag and push a new release for a non-Python project. Version tracking is done via git tags only."
  [release.sh]="Stage, commit, tag and push a new release for a Python project, using the version in pyproject.toml. Hands off to nopy_release.sh if no pyproject.toml is present."
  [servicectrl.sh]="Start, stop, or restart the app's systemd service. Linux only, not supported on macOS."
  [showver.sh]="Print the current project name and version, as defined in pyproject.toml."
  [sync_dev_files.sh]="Push or pull development environment files between the current project and a remote/shared folder."
)

# Fallback: pull the first useful descriptive comment line out of a script
# that isn't in the DESCRIPTIONS table above.
fallback_description() {
  local file="$1"
  local name="$2"
  awk -v name="$name" '
    NR == 1 && /^#!/ { next }
    /^: ?['\''"]?=+/ { next }
    /^[[:space:]]*$/ { next }
    /^#/ {
      line = $0
      sub(/^#[[:space:]]*/, "", line)
      if (line == "" || line == name) { next }
      print line; exit
    }
    {
      line = $0
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", line)
      if (line != "") { print line; exit }
    }
  ' "$file"
}

echo "Utility scripts in $SCRIPT_DIR:"
echo

for file in "$SCRIPT_DIR"/*.sh; do
  [[ -f "$file" ]] || continue
  name="$(basename "$file")"
  desc="${DESCRIPTIONS[$name]:-}"
  if [[ -z "$desc" ]]; then
    desc="$(fallback_description "$file" "$name")"
    [[ -z "$desc" ]] && desc="(no description found)"
  fi
  printf "%-22s %s\n" "$name" "$desc"
done

echo
echo "Run any script with --help for full usage details (where supported)."
