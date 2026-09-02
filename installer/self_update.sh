#!/usr/bin/env sh
# Happy Hare MMU Software
#
# Updater script. Pull latest version of Happy Hare
#
# Copyright (C) 2022-2025 moggieuk#6538 (discord) moggieuk@hotmail.com
#

set -e # Exit immediately on error

self_update() {
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

    # Both check for updates but also help me not loose changes accidentally
    git fetch --quiet

    switch=0
    if [ -n "${BRANCH}" ] && [ "${BRANCH}" != "${current_branch}" ]; then
        # An explicit branch selection takes precedence over updating the branch we
        # happened to start on. Switch first; the common pull below updates only the
        # selected branch.
        current_branch=${BRANCH}
        echo "${C_NOTICE}Switching to '${current_branch}' branch${C_OFF}"
        if [ -n "$(git status --porcelain)" ]; then
            git stash push -m 'local changes stashed before self update' --quiet
        fi
        switch=1
    else
        echo "${C_NOTICE}Running on '${current_branch}' branch" \
            "Checking for updates...${C_OFF}"
        if ! git diff --quiet --exit-code "origin/${current_branch}"; then
            echo "${C_NOTICE}Found a new version of Happy Hare on github, updating...${C_OFF}"
            if [ -n "$(git status --porcelain)" ]; then
                git stash push -m 'local changes stashed before self update' --quiet
            fi
            switch=1
        fi
    fi

    if [ "${switch}" -eq 1 ]; then
        git checkout "${current_branch}" --quiet
        # A branch selected with -b may already exist locally without an upstream.
        # Point it at the remote branch explicitly so this and future pulls do not
        # depend on how the local branch was originally created.
        git branch --quiet --set-upstream-to="origin/${current_branch}" "${current_branch}"
        git pull --quiet --force
        git_version=$(git describe --tags)
        echo "${C_NOTICE}Now on git version: ${git_version}${C_OFF}"
    else
        git_version=$(git describe --tags)
        echo "${C_NOTICE}Already on the latest version: ${git_version}${C_OFF}"
    fi

    # Stashes are never popped automatically, here or anywhere else -- a stash from this
    # run, or a forgotten one from a previous run, will otherwise sit invisible until
    # something (e.g. a hand-edited Kconfig file) mysteriously looks like it reverted
    stash_count=$(git stash list | wc -l | tr -d ' ')
    if [ "${stash_count}" -gt 0 ]; then
        echo "${C_WARNING}WARNING: you have ${stash_count} stashed change(s) in this repo." \
            "self_update never restores these automatically." \
            "Run 'git stash list' to see them and 'git stash pop' to restore the most recent.${C_OFF}"
    fi
}

self_update
