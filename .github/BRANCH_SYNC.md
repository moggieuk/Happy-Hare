# Synchronizing main into development

`sync-development.yml` runs after pushes to `main`. When `main` has commits
missing from `development`, it opens or reuses a PR with `main` as the source
and `development` as the destination. Further pushes update that same PR
naturally. Runs are serialized to prevent duplicate PR creation.

The workflow requests GitHub auto-merge using a **merge commit**. Required
checks and reviews still apply, and conflicts require manual resolution.
It never force-pushes, rewrites either branch, or bypasses branch protection.

## One-time repository setup

1. Merge these workflow changes into `main` and ensure `development` exists.
2. Under **Settings → General → Pull Requests**, enable **Allow merge commits**
   and **Allow auto-merge**.
3. Protect `development` with required checks from **Test Suite** (`test`) and
   **Lint** (`ruff`). Run CI first if GitHub needs to discover these checks.
   Do not require linear history: branch synchronization uses merge commits.
   Required reviews remain manual gates if you choose to require them.
4. Register a private GitHub App under the repository owner's account.
   Disable webhooks; no server, OAuth callback, or user authorization is needed.
   Grant repository **Contents**, **Pull requests**, and **Workflows** read/write
   permissions (Metadata read access is mandatory). Install it on **Happy-Hare
   only**. Do not grant it a branch-protection bypass.
5. Save the App's client ID as the Actions repository variable
   `BRANCH_SYNC_APP_CLIENT_ID`. Generate a private key and save it as the Actions
   repository secret `BRANCH_SYNC_APP_PRIVATE_KEY`. Never commit the key.

Each run uses the pinned official
[create-github-app-token action](https://github.com/actions/create-github-app-token)
to generate an installation token restricted to this repository and the three
permissions above. The token expires after one hour and is revoked at job
completion by default. App-created PRs trigger normal CI without the built-in
token's approval restriction. The workflow gives `GITHUB_TOKEN` no permissions
and has no personal-token fallback, so credential setup errors fail visibly.

The private key does not expire automatically: protect it and rotate it
periodically. To rotate, generate a new App key, replace the repository secret,
verify a manual run succeeds, then revoke the old key. No recurring personal
token renewal is needed. If migrating from the previous setup, keep
`BRANCH_SYNC_TOKEN` until the App workflow is verified, then remove that secret
and revoke its underlying personal access token.

If repository auto-merge or merge commits are disabled, the workflow still
opens the PR and reports a warning, leaving it for manual merging. Other API
errors fail the workflow visibly. Enable repository settings and required
checks before relying on automatic synchronization.

## Operation and conflicts

Use **Actions → Sync main into development → Run workflow**, selecting `main`,
to synchronize existing changes or retry after correcting settings. A run
does nothing when `development` already contains all of `main`.

For conflicts, create a temporary branch from `development`, merge `main`
into it locally, resolve conflicts, and open a PR back into `development`.
Merge that resolution PR with a merge commit. Do not use the synchronization
PR's conflict editor or update its source branch with `development`: its
source is the production `main` branch.

Use merge commits for releases from `development` into `main` as well.
Feature PRs may still use squash merges. Never delete either long-lived
branch after merging a synchronization or release PR.
