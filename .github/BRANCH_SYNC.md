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
4. For unattended CI, create a fine-grained personal access token scoped to
   this repository with **Contents: read and write** and **Pull requests: read
   and write**, and store it as the Actions secret `BRANCH_SYNC_TOKEN`.
   Its owner must have access to the repository; renew it before it expires.
   If GitHub requires **Workflows: read and write** when synchronizing workflow
   file changes, grant that permission too.

Without `BRANCH_SYNC_TOKEN`, the workflow uses `GITHUB_TOKEN`. Enable
**Settings → Actions → General → Workflow permissions → Allow GitHub Actions
to create and approve pull requests** for that fallback. PR checks created
using this token require manual approval under GitHub's current behavior,
so the fallback is not fully unattended. See
[GitHub's workflow triggering documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).

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
