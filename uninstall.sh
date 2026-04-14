#!/bin/bash
# Happy Hare MMU Software
#
# Dedicated uninstaller wrapper
#

set -e

SCRIPT="$(readlink -f "$0" 2>/dev/null || realpath "$0")"
SCRIPTPATH="$(dirname "$SCRIPT")"
INSTALLER="${SCRIPTPATH}/install.sh"

if [ ! -f "${INSTALLER}" ]; then
    echo "install.sh not found next to uninstall.sh"
    exit 1
fi

exec bash "${INSTALLER}" -d "$@"
