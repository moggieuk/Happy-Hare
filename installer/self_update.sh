#!/usr/bin/env sh
# Happy Hare MMU Software
#
# Updater script
#
# Copyright (C) 2022  moggieuk#6538 (discord) moggieuk@hotmail.com
#
# Creality K1 Support
#               2024  hamyy <oudy_1999@hotmail.com>
#               2024  Unsweeticetea <iamzevle@gmail.com>
#               2024  Dmitry Kychanov <k1-801@mail.ru>
#

set -e # Exit immediately on error

self_update() {
    if [ -n "${SKIP_UPDATE+x}" ]; then
        printf "%sSkipping self update%s\n" "${C_NOTICE}" "${C_OFF}"
        return
    fi

    git_cmd="git branch --show-current"
    if which timeout >/dev/null 2>&1; then
        # timeout is unavailable on some systems (e.g. Creality K1). So only add it if found
        git_cmd="timeout 3s ${git_cmd}"
    fi

    if ! current_branch=$(${git_cmd}); then
        printf "%sError updating from github\nYou might have an old version of gitn\nSkipping automatic update...%s\n" "${C_ERROR}" "${C_OFF}"
        return
    fi

    if [ -z "${current_branch}" ]; then
        printf "%sTimeout talking to github. Skipping upgrade check%s\n" "${C_ERROR}" "${C_OFF}"
        return
    fi

    printf "%sRunning on '${current_branch}' branch\nChecking for updates...%s\n" "${C_NOTICE}" "${C_OFF}"
    # Both check for updates but also help me not loose changes accidently
    git fetch --quiet

    switch=0
    if ! git diff --quiet --exit-code "origin/${current_branch}"; then
        printf "%sFound a new version of Happy Hare on github, updating...%s\n" "${C_NOTICE}" "${C_OFF}"
        if [ -n "$(git status --porcelain)" ]; then
            git stash push -m 'local changes stashed before self update' --quiet
        fi
        switch=1
    fi

    if [ -n "${BRANCH}" ] && [ "${BRANCH}" != "${current_branch}" ]; then
        printf "%sSwitching to '${current_branch}' branch%s\n" "${C_NOTICE}" "${C_OFF}"
        current_branch=${BRANCH}
        switch=1
    fi

    if [ "${switch}" -eq 1 ]; then
        git checkout "${current_branch}" --quiet
        git pull --quiet --force
        git_version=$(git describe --tags)
        printf "%sNow on git version: ${git_version}%s\n" "${C_NOTICE}" "${C_OFF}"
    else
        git_version=$(git describe --tags)
        printf "%sAlready on the latest version: ${git_version}%s\n" "${C_NOTICE}" "${C_OFF}"
    fi
}

self_update
