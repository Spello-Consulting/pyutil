#!/bin/bash
: '=======================================================
User Agent Control

Deploys and manages a macOS launchd user agent (LaunchAgent) plist.
=========================================================='

PYPROJECT="pyproject.toml"

# Basic sanity checks
if [ "$(uname -s)" != "Darwin" ]; then
	echo "Error: useragentctrl.sh is only supported on macOS because it requires launchd." >&2
	exit 1
fi

if [ "$(id -u)" -eq 0 ]; then
	echo "Error: this script must not be run with sudo/root. LaunchAgents run per-user." >&2
	exit 1
fi

# Get the current working directory
CURRENT_DIR=$(pwd)

# Get the current version and user agent label from pyproject.toml
if [ -f "$PYPROJECT" ]; then
    CURRENT_VERSION=$(grep -E '^version *= *"' "$PYPROJECT" | head -1 | sed -E 's/^version *= *"([^"]+)".*$/\1/')
    PROJECT_NAME=$(grep -E '^name *= *"' "$PYPROJECT" | head -1 | sed -E 's/^name *= *"([^"]+)".*$/\1/')
    LABEL=$(grep -E '^user_agent_name *= *"' "$PYPROJECT" | head -1 | sed -E 's/^user_agent_name *= *"([^"]+)".*$/\1/')
else
    echo "Error: $PYPROJECT not found."
    exit 1
fi

if [ -z "$LABEL" ]; then
	echo "Error: user_agent_name not defined in $PYPROJECT."
	exit 1
fi

PLIST_NAME="$LABEL.plist"
TEMPLATE="$CURRENT_DIR/deploy/$PLIST_NAME"
TARGET_PLIST="$HOME/Library/LaunchAgents/$PLIST_NAME"
SERVICE_TARGET="gui/$(id -u)/$LABEL"

YES_MODE=0

usage() {
	echo "Usage: $0 [--yes] {deploy|status|run|reload|disable|remove|edit|help}"
	exit 1
}

confirm() {
	if [ "$YES_MODE" -eq 1 ]; then
		return 0
	fi
	read -r -p "$1 Enter Y to continue, any other key to abort: " CONFIRM
	if [[ "$CONFIRM" != "Y" && "$CONFIRM" != "y" ]]; then
		echo "Aborted."
		exit 0
	fi
}

edit_plist() {
	if [ ! -f "$TARGET_PLIST" ]; then
		echo "Plist '$TARGET_PLIST' does not exist."
		echo "Use the deploy command to create it, or edit it manually."
		exit 1
	fi

	nano "$TARGET_PLIST"
}

deploy_agent() {
	if [ ! -f "$TEMPLATE" ]; then
		echo "Error: user agent template '$TEMPLATE' not found." >&2
		exit 1
	fi

	confirm "This will deploy '$TEMPLATE' to '$TARGET_PLIST' and (re)load it."
	cp "$TEMPLATE" "$TARGET_PLIST"
	chmod 644 "$TARGET_PLIST"
	launchctl unload "$TARGET_PLIST" 2>/dev/null
	launchctl load "$TARGET_PLIST"
	echo "Deployed and loaded '$LABEL'."
}

reload_agent() {
	if [ ! -f "$TARGET_PLIST" ]; then
		echo "Error: '$TARGET_PLIST' does not exist. Use the deploy command first." >&2
		exit 1
	fi

	confirm "This will unload and reload '$TARGET_PLIST'."
	launchctl unload "$TARGET_PLIST"
	launchctl load "$TARGET_PLIST"
	echo "Reloaded '$LABEL'."
}

disable_agent() {
	if [ ! -f "$TARGET_PLIST" ]; then
		echo "Error: '$TARGET_PLIST' does not exist." >&2
		exit 1
	fi

	confirm "This will unload (disable) '$LABEL'."
	launchctl unload "$TARGET_PLIST"
	echo "Disabled '$LABEL'."
}

remove_agent() {
	if [ ! -f "$TARGET_PLIST" ]; then
		echo "Error: '$TARGET_PLIST' does not exist." >&2
		exit 1
	fi

	confirm "This will unload '$LABEL' and remove '$TARGET_PLIST'."
	launchctl unload "$TARGET_PLIST" 2>/dev/null
	rm -f "$TARGET_PLIST"
	echo "Removed '$LABEL'."
}

run_agent() {
	confirm "This will run '$LABEL' now (launchctl kickstart -k)."
	launchctl kickstart -k "$SERVICE_TARGET"
}

help() {
	echo "User Agent Control - manage the '$LABEL' launchd user agent"
	echo ""
	echo "Usage: $0 [--yes] <command>"
	echo ""
	echo "Options:"
	echo "  --yes                    Non-interactive; skip confirmation prompt"
	echo ""
	echo "Commands:"
	echo "  deploy   			  Deploy the user agent plist from deploy/ and load it"
	echo "  status   			  Show the current status of the user agent"
	echo "  run      			  Run the user agent now (launchctl kickstart -k)"
	echo "  reload   			  Unload then reload the plist"
	echo "  disable  			  Unload the plist"
	echo "  remove   			  Unload and remove the plist"
	echo "  edit     			  Edit the deployed plist file"
	echo "  help     			  Show this help message"
	exit 0
}

if [ $# -lt 1 ]; then
	usage
fi

# Parse --yes flag if present
if [ "$1" = "--yes" ]; then
	YES_MODE=1
	shift
fi

if [ $# -ne 1 ]; then
	usage
fi

if [ "$1" = "help" ]; then
	help
fi

echo "Managing user agent '$LABEL' for project '$PROJECT_NAME' (v$CURRENT_VERSION) - action: $1"
case "$1" in
	deploy)
		deploy_agent
		;;
	status)
		launchctl print "$SERVICE_TARGET"
		;;
	run)
		run_agent
		;;
	reload)
		reload_agent
		;;
	disable)
		disable_agent
		;;
	remove)
		remove_agent
		;;
	edit)
		edit_plist
		;;
	*)
		usage
		;;
esac
