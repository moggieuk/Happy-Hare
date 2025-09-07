#!/usr/bin/env sh
# Happy Hare MMU Software
#
# Updater script. Pull latest version of Happy Hare
#
# Copyright (C) 2022-2025 moggieuk#6538 (discord) moggieuk@hotmail.com
#

set -e # Exit immediately on error

self_update() {
    if [ -n "${F_SKIP_UPDATE+x}" ]; then
        echo "${C_NOTICE}Skipping self update${C_OFF}"
        return
    fi

    git_cmd="git branch --show-current"
    if which timeout >/dev/null 2>&1; then
        # timeout is unavailable on some systems (e.g. Creality K1). So only add it if found
        git_cmd="timeout 3s ${git_cmd}"
    fi

    if ! current_branch=$(${git_cmd}); then
        echo "${C_ERROR}Error updating from github" \
            "You might have an old version of git" \
            "Skipping automatic update...${C_OFF}"
        return
    fi

    if [ -z "${current_branch}" ]; then
        echo "${C_ERROR}Timeout talking to github. Skipping upgrade check${C_OFF}"
        return
    fi

    echo "${C_NOTICE}Running on '${current_branch}' branch" \
        "Checking for updates...${C_OFF}"
    # Both check for updates but also help me not loose changes accidently
    git fetch --quiet

    switch=0
    if ! git diff --quiet --exit-code "origin/${current_branch}"; then
        echo "${C_NOTICE}Found a new version of Happy Hare on github, updating...${C_OFF}"
        if [ -n "$(git status --porcelain)" ]; then
            git stash push -m 'local changes stashed before self update' --quiet
        fi
        switch=1
    fi

    if [ -n "${BRANCH}" ] && [ "${BRANCH}" != "${current_branch}" ]; then
        echo "${C_NOTICE}Switching to '${current_branch}' branch${C_OFF}"
        current_branch=${BRANCH}
        switch=1
    fi

    if [ "${switch}" -eq 1 ]; then
        git checkout "${current_branch}" --quiet
        git pull --quiet --force
        git_version=$(git describe --tags)
        echo "${C_NOTICE}Now on git version: ${git_version}${C_OFF}"
    else
        git_version=$(git describe --tags)
        echo "${C_NOTICE}Already on the latest version: ${git_version}${C_OFF}"
    fi
}

# PAUL TEMP self_update
