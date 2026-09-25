#!/opt/homebrew/bin/bash
: '=======================================================
distribute.sh — fan pyutil master scripts out to client projects

pyutil is the source of truth for a small set of shared shell scripts. Each
client project opts in by declaring, in its own pyproject.toml:

[tool.pyutil]
managed_scripts = ["launch.sh"]

This script finds every opted-in project under the dev root and copies the
managed masters into <project>/scripts/, injecting a provenance header so a
stale copy (safe to refresh) can be told apart from a hand-edited one (refused
unless --force is given).

Usage:
    distribute.sh [--check] [--force] [--project DIR] [--root DIR]

Modes / flags:
    (no flags)        Copy managed scripts into every discovered project.
    --check           Dry run: report drift, write nothing, non-zero exit on any.
    --force           Overwrite copies that were edited locally / are unstamped.
    --project DIR     Limit to a single project directory.
    --root DIR        Dev root to scan (default: $PYUTIL_DEV_ROOT or ~/dev).
    -h, --help        Show this help.

Exit status:
    0  everything up to date (or all requested copies written).
    1  drift found in --check, refused local edits, or an error.
=========================================================='

set -euo pipefail

# ---------------------------------------------------------------------------
# Rollout allowlist: the scripts this distributor is allowed to push.
# Add entries here as more masters are ready to be centrally managed.
# ---------------------------------------------------------------------------
MANAGED_SCRIPTS=("launch.sh")

# Provenance-header sentinels. The block is inserted immediately after the
# shebang; stripping it from a copy must yield the master byte-for-byte.
STAMP_START="# >>>>> pyutil-managed >>>>>"
STAMP_END="# <<<<< pyutil-managed <<<<<"
SHA_PREFIX="# pyutil-source-sha256:"

# Directory holding this script and the master copies (pyutil root).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Colors (suppressed when not writing to a terminal).
if [ -t 1 ]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
    BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; BLUE=''; BOLD=''; NC=''
fi

info()    { echo -e "$1"; }
success() { echo -e "${GREEN}$1${NC}"; }
warning() { echo -e "${YELLOW}$1${NC}" >&2; }
error()   { echo -e "${RED}$1${NC}" >&2; }

usage() {
    cat <<'EOF'
Usage: distribute.sh [--check] [--force] [--project DIR] [--root DIR]
  --check         Dry run: report drift, write nothing, non-zero exit on any.
  --force         Overwrite copies edited locally / of unknown provenance.
  --project DIR   Limit to a single project directory.
  --root DIR      Dev root to scan (default: $PYUTIL_DEV_ROOT or ~/dev).
  -h, --help      Show this help.
EOF
}

# --- argument parsing -------------------------------------------------------
CHECK_ONLY=false
FORCE=false
ONE_PROJECT=""
DEV_ROOT="${PYUTIL_DEV_ROOT:-$HOME/dev}"

while [ $# -gt 0 ]; do
    case "$1" in
        --check)   CHECK_ONLY=true ;;
        --force)   FORCE=true ;;
        --project) shift; [ $# -gt 0 ] || { error "--project needs a directory"; exit 1; }; ONE_PROJECT="$1" ;;
        --root)    shift; [ $# -gt 0 ] || { error "--root needs a directory"; exit 1; }; DEV_ROOT="$1" ;;
        -h|--help) usage; exit 0 ;;
        *)         error "Unknown argument: $1"; usage; exit 1 ;;
    esac
    shift
done

# Expand a leading tilde in paths coming from the environment / flags.
DEV_ROOT="${DEV_ROOT/#\~/$HOME}"
[ -n "$ONE_PROJECT" ] && ONE_PROJECT="${ONE_PROJECT/#\~/$HOME}"

# --- helpers ----------------------------------------------------------------

# Print the sha256 of a file's contents.
sha_of_file() {
    shasum -a 256 "$1" | awk '{print $1}'
}

# Print the sha256 of text supplied on stdin.
sha_of_stdin() {
    shasum -a 256 | awk '{print $1}'
}

# Membership test: is "$1" one of the remaining args?
contains() {
    local needle="$1"; shift
    local item
    for item in "$@"; do
        [ "$item" = "$needle" ] && return 0
    done
    return 1
}

# Extract the managed_scripts array from a project's pyproject.toml [tool.pyutil]
# section. Emits one script name per line (empty output if none).
parse_managed_scripts() {
    local toml="$1"
    awk '
        /^\[/ { insec = ($0 ~ /^\[tool\.pyutil\][[:space:]]*$/) }
        insec { print }
    ' "$toml" \
        | tr '\n' ' ' \
        | grep -oE 'managed_scripts[[:space:]]*=[[:space:]]*\[[^]]*\]' \
        | grep -oE '"[^"]*"' \
        | tr -d '"'
}

# Write a stamped copy of a master to stdout. $1=master path, $2=script name.
render_stamped() {
    local master="$1" name="$2" master_sha
    master_sha="$(sha_of_file "$master")"
    head -n 1 "$master"
    echo "$STAMP_START"
    echo "# Managed by pyutil — do not edit this copy."
    echo "# Source of truth: pyutil/$name — edit there, then run pyutil/distribute.sh."
    echo "# Updates refuse to overwrite local edits unless --force is used."
    echo "$SHA_PREFIX $master_sha"
    echo "$STAMP_END"
    tail -n +2 "$master"
}

# Strip the provenance block from a copy and print the reconstructed body.
strip_stamp() {
    sed "/^${STAMP_START}$/,/^${STAMP_END}$/d" "$1"
}

# Read the embedded source sha from a copy (empty if unstamped).
embedded_sha() {
    grep -m1 "^${SHA_PREFIX} " "$1" 2>/dev/null | awk '{print $3}'
}

# Classify a destination copy against a master. Echoes one of:
#   new adopt stale uptodate edited foreign
# $1=dest path, $2=master path
classify() {
    local dest="$1" master="$2" master_sha emb recon
    master_sha="$(sha_of_file "$master")"
    if [ ! -f "$dest" ]; then
        echo "new"; return
    fi
    emb="$(embedded_sha "$dest")"
    recon="$(strip_stamp "$dest" | sha_of_stdin)"
    if [ -z "$emb" ]; then
        # No provenance header: a hand-copied or foreign file.
        if [ "$recon" = "$master_sha" ]; then echo "adopt"; else echo "foreign"; fi
        return
    fi
    if [ "$recon" != "$emb" ]; then
        echo "edited"          # body changed after we stamped it
    elif [ "$emb" = "$master_sha" ]; then
        echo "uptodate"
    else
        echo "stale"           # untouched, but from an older master
    fi
}

# --- project discovery ------------------------------------------------------
declare -a PROJECT_TOMLS=()

if [ -n "$ONE_PROJECT" ]; then
    if [ ! -f "$ONE_PROJECT/pyproject.toml" ]; then
        error "No pyproject.toml in --project directory: $ONE_PROJECT"
        exit 1
    fi
    PROJECT_TOMLS=("$ONE_PROJECT/pyproject.toml")
else
    if [ ! -d "$DEV_ROOT" ]; then
        error "Dev root does not exist: $DEV_ROOT"
        exit 1
    fi
    shopt -s nullglob
    PROJECT_TOMLS=("$DEV_ROOT"/*/pyproject.toml)
    shopt -u nullglob
fi

# --- main loop --------------------------------------------------------------
had_drift=false      # any not-up-to-date state seen (drives --check exit)
had_refusal=false    # a local edit was refused (drives normal-mode exit)
had_error=false      # a hard error (missing master, etc.)
touched=0

$CHECK_ONLY && info "${BOLD}Checking managed scripts (dry run)…${NC}" \
             || info "${BOLD}Distributing managed scripts…${NC}"
if [ -n "$ONE_PROJECT" ]; then
    info "Project:    $ONE_PROJECT"
else
    info "Dev root:   $DEV_ROOT"
fi
info "Managed:    ${MANAGED_SCRIPTS[*]}"
echo ""

for toml in "${PROJECT_TOMLS[@]}"; do
    project_dir="$(cd "$(dirname "$toml")" && pwd)"

    # Never distribute pyutil into itself.
    [ "$project_dir" = "$SCRIPT_DIR" ] && continue

    # What does this project opt into, intersected with the allowlist?
    mapfile -t requested < <(parse_managed_scripts "$toml")
    [ ${#requested[@]} -eq 0 ] && continue

    declare -a effective=()
    for s in "${requested[@]}"; do
        contains "$s" "${MANAGED_SCRIPTS[@]}" && effective+=("$s")
    done
    [ ${#effective[@]} -eq 0 ] && continue

    info "${BLUE}${project_dir}${NC}"

    for name in "${effective[@]}"; do
        master="$SCRIPT_DIR/$name"
        if [ ! -f "$master" ]; then
            error "  ✗ $name — master not found in pyutil"
            had_error=true
            continue
        fi
        dest_dir="$project_dir/scripts"
        dest="$dest_dir/$name"
        status="$(classify "$dest" "$master")"

        case "$status" in
            uptodate)
                info "  ${GREEN}=${NC} $name — up to date"
                ;;
            new|adopt|stale)
                had_drift=true
                local_label="updated"; [ "$status" = new ] && local_label="new"; [ "$status" = adopt ] && local_label="stamped existing"
                if $CHECK_ONLY; then
                    warning "  ~ $name — $local_label (would write)"
                else
                    mkdir -p "$dest_dir"
                    render_stamped "$master" "$name" > "$dest"
                    chmod +x "$dest"
                    success "  ↑ $name — $local_label"
                    touched=$((touched + 1))
                fi
                ;;
            edited|foreign)
                had_drift=true
                reason="local edit"; [ "$status" = foreign ] && reason="unknown provenance"
                if $CHECK_ONLY; then
                    warning "  ✗ $name — $reason (needs --force to overwrite)"
                elif $FORCE; then
                    mkdir -p "$dest_dir"
                    render_stamped "$master" "$name" > "$dest"
                    chmod +x "$dest"
                    warning "  ! $name — overwrote $reason (--force)"
                    touched=$((touched + 1))
                else
                    error "  ✗ $name — refused: $reason (use --force to overwrite)"
                    had_refusal=true
                fi
                ;;
        esac
    done
    echo ""
done

# --- summary & exit ---------------------------------------------------------
if $CHECK_ONLY; then
    if $had_drift || $had_error; then
        warning "Drift found — run distribute.sh to update (or --force for local edits)."
        exit 1
    fi
    success "All managed scripts are up to date."
    exit 0
fi

if $had_error; then
    error "Completed with errors."
    exit 1
fi
if $had_refusal; then
    warning "Wrote $touched file(s); some copies were refused (local edits). Re-run with --force to overwrite them."
    exit 1
fi
success "Done. Wrote $touched file(s)."
exit 0
