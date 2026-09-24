#!/bin/bash
: '=======================================================
Service Control

Starts, stops, or restarts the app service(s).

A project may define one service in pyproject.toml:

    service_name = "safe-share"

or several:

    service_name = ["safe-share", "cloudflared"]

Commands act on every defined service by default. Pass --service <name>
(or -s <name>) to act on a single one; the name must be one of the
service_name entries in pyproject.toml.
=========================================================='

PYPROJECT="pyproject.toml"

# Basic sanity checks
if [ "$(uname -s)" = "Darwin" ]; then
	echo "Error: servicectrl.sh is not supported on macOS because it requires systemd." >&2
	exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
	echo "Error: this script requires root privileges. Please run with sudo." >&2
	exit 1
fi

# Get the current working directory
CURRENT_DIR=$(pwd)

# -------------------------------------------------------------------------
# Parse the service_name entry from pyproject.toml into the SERVICES array.
# Accepts either a quoted scalar (service_name = "safe-share") or a TOML array
# (service_name = ["safe-share", "cloudflared"]) for backwards compatibility.
# -------------------------------------------------------------------------
SERVICES=()
parse_service_names() {
	local line raw inner part
	line=$(grep -E '^service_name *=' "$PYPROJECT" | head -1)
	[ -z "$line" ] && return 0
	# Everything after the first '=', trimmed of surrounding whitespace.
	raw="${line#*=}"
	raw="$(printf '%s' "$raw" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')"
	[ -z "$raw" ] && return 0
	if [ "${raw:0:1}" = "[" ]; then
		# Array form: strip the brackets, split on commas, unquote each element.
		inner="${raw#\[}"
		inner="${inner%\]}"
		local IFS=','
		for part in $inner; do
			part="$(printf '%s' "$part" | sed -E 's/^[[:space:]]*"?//; s/"?[[:space:]]*$//')"
			[ -n "$part" ] && SERVICES+=("$part")
		done
	else
		# Scalar form: strip surrounding quotes.
		part="$(printf '%s' "$raw" | sed -E 's/^"//; s/"$//')"
		[ -n "$part" ] && SERVICES+=("$part")
	fi
}

# Get the current version and name from pyproject.toml
if [ -f "$PYPROJECT" ]; then
    CURRENT_VERSION=$(grep -E '^version *= *"' "$PYPROJECT" | head -1 | sed -E 's/^version *= *"([^"]+)".*$/\1/')
    PROJECT_NAME=$(grep -E '^name *= *"' "$PYPROJECT" | head -1 | sed -E 's/^name *= *"([^"]+)".*$/\1/')
    parse_service_names
else
    echo "Error: $PYPROJECT not found."
    exit 1
fi

if [ ${#SERVICES[@]} -eq 0 ]; then
	echo "Error: service_name not defined in $PYPROJECT."
	exit 1
fi

UserID=${SUDO_USER:-$USER}

usage() {
	echo "Usage: $0 [--service <name>] {start|stop|restart|reload|disable|enable|status|is-active|logs|edit|help}"
	echo "       $0 [--service <name>] deploy <environment>"
	echo ""
	echo "  --service <name>, -s <name>   Act on a single service (must be one of: ${SERVICES[*]})."
	echo "                                Without it, the command acts on every defined service."
	exit 1
}

service_file_path() {
	echo "/etc/systemd/system/$1.service"
}

edit_service_file() {
	local svc service_file
	for svc in "${TARGET_SERVICES[@]}"; do
		service_file="$(service_file_path "$svc")"
		if [ ! -f "$service_file" ]; then
			echo "Service file '$service_file' does not exist."
			echo "Use the deploy command to create it, or edit it manually."
		fi
		nano "$service_file"
	done
}

deploy_service() {
	local environment="$1" svc template service_file
	for svc in "${TARGET_SERVICES[@]}"; do
		template="$CURRENT_DIR/deploy/$environment/$svc.service"
		service_file="$(service_file_path "$svc")"
		if [ ! -f "$template" ]; then
			echo "Error: service template '$template' not found." >&2
			exit 1
		fi
		echo "Deploying service '$svc' from '$template' to '$service_file', then reloading, enabling, and starting it."
		cp "$template" "$service_file"
		systemctl daemon-reexec
		systemctl daemon-reload
		systemctl enable "$svc"
		systemctl restart "$svc"
	done
}

help() {
	echo "Service Control - manage the systemd service(s) for project '$PROJECT_NAME'"
	echo ""
	echo "Defined services: ${SERVICES[*]}"
	echo ""
	echo "Usage: $0 [--service <name>] <command>"
	echo ""
	echo "Options:"
	echo "  --service <name>, -s <name>  Act on a single service (must be one of the defined services)."
	echo "                               Without it, the command acts on every defined service."
	echo ""
	echo "Commands:"
	echo "  edit     			  Create or edit the systemd service file(s)"
	echo "  deploy <environment>  Deploy the service(s) (create service file, enable, and start) using the specified environment template (e.g., sydneyapp)"
	echo "  start    			  Start the service(s)"
	echo "  stop    			  Stop the service(s)"
	echo "  restart 			  Stop then start the service(s)"
	echo "  reload  			  Reload the systemd daemon configuration (daemon-reexec + daemon-reload)"
	echo "  disable 			  Disable the service(s) from starting at boot"
	echo "  enable  			  Enable the service(s) to start at boot"
	echo "  status  			  Show the current status of the service(s)"
	echo "  is-active			  Show whether the service(s) are active (running)"
	echo "  logs    			  Tail the live service logs (journalctl -f)"
	echo "  help     			  Show this help message"
	exit 0
}

# -------------------------------------------------------------------------
# Pull the optional --service/-s flag out of the arguments (it may appear in
# any position), leaving the command and its operands as positional args.
# -------------------------------------------------------------------------
SERVICE_OVERRIDE=""
POSARGS=()
while [ $# -gt 0 ]; do
	case "$1" in
		-s|--service)
			if [ $# -lt 2 ]; then
				echo "Error: $1 requires a service name." >&2
				usage
			fi
			SERVICE_OVERRIDE="$2"
			shift 2
			;;
		*)
			POSARGS+=("$1")
			shift
			;;
	esac
done
set -- "${POSARGS[@]}"

# Resolve which services to act on. An explicit --service must match one of the
# entries declared in pyproject.toml, otherwise we bail rather than guessing.
if [ -n "$SERVICE_OVERRIDE" ]; then
	matched=0
	for s in "${SERVICES[@]}"; do
		if [ "$s" = "$SERVICE_OVERRIDE" ]; then
			matched=1
			break
		fi
	done
	if [ "$matched" -ne 1 ]; then
		echo "Error: service '$SERVICE_OVERRIDE' is not defined in $PYPROJECT (defined: ${SERVICES[*]})." >&2
		exit 1
	fi
	TARGET_SERVICES=("$SERVICE_OVERRIDE")
else
	TARGET_SERVICES=("${SERVICES[@]}")
fi

# Argument-count validation (after the service flag has been removed).
if [ "${1:-}" = "deploy" ]; then
	if [ $# -ne 2 ]; then
		usage
	fi
elif [ $# -ne 1 ]; then
	usage
fi

if [ "$1" = "help" ]; then
	help
fi

echo "Managing service(s) '${TARGET_SERVICES[*]}' for project '$PROJECT_NAME' (v$CURRENT_VERSION) - action: $1"

# Apply a per-service systemctl action across every target service.
for_each_service() {
	local action="$1" svc
	for svc in "${TARGET_SERVICES[@]}"; do
		echo "--- $svc ---"
		case "$action" in
			start)     systemctl start "$svc" ;;
			stop)      systemctl stop "$svc" ;;
			restart)   systemctl stop "$svc"; systemctl start "$svc" ;;
			disable)   systemctl disable "$svc" ;;
			enable)    systemctl enable "$svc" ;;
			status)    systemctl status "$svc.service" ;;
			is-active) systemctl is-active "$svc" ;;
		esac
	done
}

case "$1" in
	start|stop|restart|disable|enable|status|is-active)
		for_each_service "$1"
		;;
	reload)
		# Daemon-level, not per-service: run once regardless of how many services.
		systemctl daemon-reexec
		systemctl daemon-reload
		;;
	logs)
		# Follow all target services in a single journalctl invocation.
		journal_args=()
		for svc in "${TARGET_SERVICES[@]}"; do
			journal_args+=(-u "$svc")
		done
		journalctl "${journal_args[@]}" -f
		;;
	edit)
		edit_service_file
		;;
	deploy)
		deploy_service "$2"
		;;
	*)
		usage
		;;
esac
