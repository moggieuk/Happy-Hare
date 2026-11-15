#!/usr/bin/env sh
# Happy Hare MMU Software
#
# Serve the Visualizer (the viewer/ folder) from an nginx site of its own, beside
# Mainsail/Fluidd. Usage: visualizer_nginx.sh install|remove <viewer_dir>
#
# The site shares no files with other web UIs, so removing it never affects them.
# Only the few commands that need root use sudo.
#
# Environment (for testing and unusual layouts):
#   NGINX_DIR (/etc/nginx)  NGINX_BIN (nginx)  NGINX_ARGS (extra args for nginx -t)
#   SUDO (sudo, or empty when root)  VISUALIZER_PORT (first port to try, 8090)
#   UPSTREAM_PORT (Moonraker, 7125)  F_YES (do not ask)  NO_RELOAD (skip the reload)
#
NGINX_DIR="${NGINX_DIR:-/etc/nginx}"
NGINX_BIN="${NGINX_BIN:-nginx}"
UPSTREAM_PORT="${UPSTREAM_PORT:-7125}"
FIRST_PORT="${VISUALIZER_PORT:-8090}"
NAME=happy-hare-visualizer
SITE="${NGINX_DIR}/sites-available/${NAME}"
LINK="${NGINX_DIR}/sites-enabled/${NAME}"
HERE=$(cd "$(dirname "$0")" && pwd -P)
if [ -z "${SUDO+x}" ]; then
    if [ "$(id -u)" = 0 ]; then SUDO=; else SUDO=sudo; fi
fi

say() { echo "${C_INFO}$*${C_OFF}"; }
fail() { echo "${C_ERROR}$*${C_OFF}" >&2; exit 1; }

have_nginx() {
    [ -d "${NGINX_DIR}/sites-available" ] && [ -d "${NGINX_DIR}/sites-enabled" ] &&
        command -v "${NGINX_BIN}" >/dev/null 2>&1
}

# Ports other sites already listen on, one per line (ours excluded)
used_ports() {
    for f in "${NGINX_DIR}/nginx.conf" "${NGINX_DIR}"/conf.d/*.conf "${NGINX_DIR}"/sites-enabled/*; do
        [ -f "${f}" ] || continue
        [ "$(basename "${f}")" = "${NAME}" ] && continue
        sed -n 's/^[[:space:]]*listen[[:space:]]\{1,\}\(\[[^]]*\]:\)\{0,1\}\([0-9]\{1,\}\).*/\2/p' "${f}"
    done
}

choose_port() {
    if [ -f "${SITE}" ]; then
        port=$(sed -n 's/^[[:space:]]*listen[[:space:]]\{1,\}\([0-9]\{1,\}\);.*/\1/p' "${SITE}" | head -n 1)
        [ -n "${port}" ] && { echo "${port}"; return; }
    fi
    taken=$(used_ports)
    port=${FIRST_PORT}
    while echo "${taken}" | grep -qx "${port}"; do port=$((port + 1)); done
    echo "${port}"
}

render() { # <viewer_dir> <port>
    sed -e "s#%ROOT%#$1#g" -e "s#%PORT%#$2#g" -e "s#%UPSTREAM%#${UPSTREAM_PORT}#g" "${HERE}/nginx_visualizer.conf"
}

reload_nginx() {
    [ -n "${NO_RELOAD:-}" ] && return 0
    ${SUDO} systemctl reload nginx 2>/dev/null || ${SUDO} "${NGINX_BIN}" -s reload
}

check_readable() { # <viewer_dir>
    user=$(sed -n 's/^[[:space:]]*user[[:space:]]\{1,\}\([A-Za-z0-9_.-]\{1,\}\).*/\1/p' "${NGINX_DIR}/nginx.conf" 2>/dev/null | head -n 1)
    user=${user:-www-data}
    id "${user}" >/dev/null 2>&1 || return 0
    if ! ${SUDO} -u "${user}" test -r "$1/index.html" 2>/dev/null; then
        echo "${C_WARNING}nginx (user '${user}') cannot read $1. Usually the home directory is private;" \
            "allow it to be traversed with:  chmod o+x ~${C_OFF}"
    fi
}

address() {
    host=$(hostname -I 2>/dev/null | awk '{print $1}')
    echo "${host:-$(hostname)}"
}

confirm() {
    [ -n "${F_YES:-}" ] && return 0
    printf '%s [y/N] ' "$1"
    read -r yn || return 1
    case "${yn}" in y | Y | yes) return 0 ;; *) return 1 ;; esac
}

do_install() { # <viewer_dir>
    viewer=$1
    [ -f "${viewer}/index.html" ] || fail "No Visualizer found at ${viewer}"
    if ! have_nginx; then
        say "The Visualizer needs a web server and nginx was not found (or is not laid out like a Debian install)."
        say "See viewer/README.md: 'Hosting on a printer' for installing nginx or serving the folder yourself."
        return 0
    fi
    port=$(choose_port)
    say "Serving ${viewer} on port ${port}, proxying Moonraker on port ${UPSTREAM_PORT}."
    confirm "This uses sudo to write ${SITE} and reload nginx. Continue?" || { say "Nothing changed."; return 0; }
    tmp=$(mktemp) || fail "Cannot create a temporary file"
    render "${viewer}" "${port}" >"${tmp}"
    previous=
    if [ -f "${SITE}" ]; then previous=$(mktemp); cp "${SITE}" "${previous}"; fi
    ${SUDO} cp "${tmp}" "${SITE}" && ${SUDO} ln -sf "${SITE}" "${LINK}" || { rm -f "${tmp}" "${previous}"; fail "Unable to write the nginx site"; }
    rm -f "${tmp}"
    if ! output=$(${SUDO} "${NGINX_BIN}" ${NGINX_ARGS:-} -t 2>&1); then
        if [ -n "${previous}" ]; then ${SUDO} cp "${previous}" "${SITE}"; else ${SUDO} rm -f "${LINK}" "${SITE}"; fi
        rm -f "${previous}"
        echo "${output}" >&2
        fail "nginx rejected the configuration, so nothing was changed."
    fi
    rm -f "${previous}"
    reload_nginx || fail "nginx could not be reloaded"
    check_readable "${viewer}"
    say "The Visualizer is at http://$(address):${port}/"
}

do_remove() {
    [ -e "${SITE}" ] || [ -L "${LINK}" ] || return 0
    say "Removing the Visualizer's nginx site."
    ${SUDO} rm -f "${LINK}" "${SITE}"
    if output=$(${SUDO} "${NGINX_BIN}" ${NGINX_ARGS:-} -t 2>&1); then
        reload_nginx
    else
        echo "${output}" >&2
    fi
}

case "$1" in
install) do_install "$2" ;;
remove) do_remove ;;
*) fail "Usage: $0 install|remove <viewer_dir>" ;;
esac
