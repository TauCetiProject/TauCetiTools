# Tau Ceti project architecture and manual setup

Up to date as of **2026-10-21**. Roadmap authoring is an optional work in progress;
recheck its upstream guides when updating this manual, and advance this date after review.

This document describes a project using the shared Tau Ceti workflow. Commands below use
`ExampleOrg/ExampleCeti`. Substitute the organization, project name and human owner once
when preparing an installation. Each project has its own repositories, configuration,
credentials and merge authority. An organization can contain several projects.

## Architecture

The content repository holds Lean source and reproducible dependency pins. Human-owned
configuration defines the source paths agents may edit, the dependencies, review policy and
required checks. The roadmap repository supplies the mathematical targets. Humans review and
merge changes to those targets; optional roadmap authoring workers can prepare drafts.

The example worker selects work and runs the operator's agent subscriptions. It authenticates
as that operator, authors through a fork and opens PRs against the content repository's default
branch. Independent harnesses can call the same tools without using the example worker.

The review engine publishes head-bound scoreboards and findings. Trusted merge automation
checks the current head, build, scope and review policy before enqueueing work. GitHub's merge
queue or Bors validates the integrated candidate when selected. Direct GitHub merging uses
the passing build and review at the current PR head, without requiring that head to include the
latest default branch. An archive is evidence for analysis, not merge
authority. The optional GitHub state service mirrors observations and reduces read traffic; it
does not receive worker write credentials or decide what merges.

TauCetiTools owns common script implementations and project setup. TauCetiReview and
TauCetiProgress own their respective engines. Project repositories contain thin callers,
human-owned policy and project data. Copies of shared scripts are generated installation
artifacts, not independent implementations to edit.

## Repositories

The supported deployment uses **public project repositories**, including content, roadmap,
archives, claims and custom rubrics. Private and internal projects are not currently supported.
Authenticated GitHub API access alone is insufficient: canonical configuration is fetched
without authentication, and worker forks, dependency fetching, reusable workflows, Pages and
hosted-service access also need a coordinated private-project design. Do not install this setup
on a private project on the assumption that adding a token makes the entire deployment private.
Do not change visibility without the owner's deliberate review of the contents and history.

Install [GitHub CLI](https://cli.github.com/) and sign in as a human maintainer:

```sh
gh auth login --hostname github.com --git-protocol https --web --scopes workflow
gh auth setup-git
# For an existing browser-based login:
gh auth refresh --hostname github.com --scopes workflow
```

The OAuth `workflow` scope is needed to push generated workflow files. `GH_TOKEN` or
`GITHUB_TOKEN`, when set, overrides saved credentials; unset it to use the browser login.
Scopes do not grant repository permissions the human account lacks. Setup PRs need repository
write access, rules and secrets need administration permission, and organization App
installation may require an organization owner.

Run as an organization owner or someone permitted to create repositories:

```sh
gh auth status
gh repo create ExampleOrg/ExampleCeti --public --add-readme
gh repo create ExampleOrg/ExampleCetiRoadmap --public --add-readme
```

Existing repositories can be used. Inspect `gh repo view ExampleOrg/ExampleCeti` before
creation. A 404 can mean missing access; confirm absence with the owner before creating a
replacement. No new organization is required.

For the complete deployment, create archives and a separate claims repository as required:

```sh
gh repo create ExampleOrg/ExampleCetiData --public --add-readme
gh repo create ExampleOrg/ExampleCetiCI --public --add-readme
gh repo create ExampleOrg/ExampleCeti-claims --public --add-readme
gh api --method PUT repos/ExampleOrg/ExampleCeti-claims/actions/permissions -F enabled=false
```

The claims repository holds lease refs and has Actions disabled: contributors with write access
must not be able to install workflows that run with additional credentials.

Use the default TauCetiReview rubrics or create `ExampleOrg/ExampleCetiReview` containing a
`rubrics/` directory. A custom rubric repository need not fork the engine. Its rubric version
must be pinned separately when it differs from the selected engine repository.

## Configuration and ownership

Install the tools, clone the content repository and create `tauceti.toml` at its root:

```sh
uv tool install git+https://github.com/TauCetiProject/TauCetiTools
gh repo clone ExampleOrg/ExampleCeti
cd ExampleCeti
tauceti-tools init --repo ExampleOrg/ExampleCeti --owner-login ExampleMaintainer
```

`init` can fill an empty `tauceti.toml`; it refuses to replace a nonempty file.
For an existing configuration, use `--output tauceti.toml.example` to generate a separate
reference and compare the settings. The template selects `merge.backend = "github-queue"`.
Use GitHub's merge queue or explicitly configure Bors; these are the two supported merge setups.
Check the generated Lean module roots,
roadmap directory names and human ownership against the existing project.

The [complete example](../examples/epsilon-eridani.toml) shows the full configuration.
Its main fields are `repositories.content`, `repositories.roadmap`, `project.branch`,
`lean.roots` and the pinned `[tools.tools]`, `[tools.review]`, `[tools.progress]` sections.
Use actual repository names and immutable full commit SHAs. Do not place passwords,
tokens, PEM keys or database URLs in the public configuration.

Commands select configuration in this order: explicit `--conf PATH_OR_HTTPS_URL`,
`TAUCETI_CONF`, `./tauceti.toml`, then `tauceti.toml` at the current Git checkout's root.
For example, `tauceti doctor` works from the content checkout or any of its subdirectories.
Git worktrees use their own root. Other parent directories are not searched.
In a Lean checkout with no project configuration, `tauceti doctor` reports how to
create one; elsewhere it retains the worker-host diagnostics. `tauceti-tools doctor`
always diagnoses project setup, including a missing configuration.

`doctor --fix CHECK_ID` previews a repair; add `--apply` to perform it. A successful
repair exits zero even when other setup checks remain unresolved. Run doctor separately
afterward to inspect the remaining work; setup PRs must be merged before published-file
checks can pass. A failed repair still returns an error.

Doctor checks the published Lean package and workflows on GitHub at the configured
content branch and the roadmap's default branch. A successful local `lake build` does
not clear these checks: commit the package files and merge the setup PR into that
branch, then rerun doctor. A missing-file report means doctor could read both the
repository and the selected revision; otherwise it reports the access problem.

Use `.github/CODEOWNERS` to assign human ownership, for example:

```text
/tauceti.toml @ExampleMaintainer
/.github/ @ExampleMaintainer
/scripts/ @ExampleMaintainer
/lakefile.* @ExampleMaintainer
/lean-toolchain @ExampleMaintainer
/lake-manifest.json @ExampleMaintainer
```

The content tree is machine-editable only within the configured source scope. Human-owned
configuration changes require human review. Preserve existing project ownership rules when
adding the file. Ordinary content PRs cannot change the policy that judges their own changes.

An online configuration is the same canonical file, normally its raw GitHub URL:

```text
https://raw.githubusercontent.com/ExampleOrg/ExampleCeti/main/tauceti.toml
```

Workers save its contents locally when starting. They pass that saved path to children. Remote
edits take effect on restart or an explicit configuration refresh; they do not interrupt a round.

## Lean and upstream dependencies

Initialize the content package with the intended Lean toolchain and Lake dependencies.
Content roots use `lakefile.toml`; roadmap packages and upstream dependencies can use either Lake configuration
format. To convert an existing root Lean lakefile, run `lake translate-config toml`. Lake writes
`lakefile.toml` and renames the original to `lakefile.lean.bak`. Review the conversion, run
`lake update` and `lake build`, then commit the TOML file and removal of the active Lean file.
If both formats already exist, compare them and retain the reviewed TOML configuration;
move the Lean file to an unused backup name so it is no longer active.
Commit both `lean-toolchain` and `lake-manifest.json`. Set the Lean library root to the
configuration's `lean.roots`. The supplied build harness currently renders one top-level
Lean library root. Projects with several roots keep their existing custom build workflow. A project following Tau Ceti's empty-root policy uses a glob
that builds every source module; its root module does not need individual imports.

Before running Lake in either a content or roadmap checkout, add `/.lake/` to
`.gitignore` if it is not already covered. Preserve existing ignore rules and commit
`.gitignore` with the package files. This keeps build artifacts and downloaded
dependencies out of `git add .`:

```sh
git check-ignore -q .lake/ || printf '\n/.lake/\n' >> .gitignore
```

For a new Mathlib-based starter, obtain the matching toolchain without guessing its version:

```sh
mathlib_sha=$(gh api repos/leanprover-community/mathlib4/commits/master --jq .sha)
gh api "repos/leanprover-community/mathlib4/contents/lean-toolchain?ref=$mathlib_sha" \
  --jq .content | base64 --decode > lean-toolchain
elan toolchain install "$(cat lean-toolchain)"
mkdir -p ExampleCeti
printf 'module\n' > ExampleCeti.lean
cat > ExampleCeti/Basic.lean <<'LEAN'
/-
Copyright (c) 2026 Example Maintainer. All rights reserved.
Released under Apache 2.0 license as described in the file LICENSE.
Authors: Example Maintainer
-/
module
LEAN
```

Write `lakefile.toml` as follows, then run `lake update`, `lake exe cache get`, and `lake build`:

```toml
name = "ExampleCeti"
defaultTargets = ["ExampleCeti"]
platformIndependent = true

[[require]]
name = "mathlib"
git = "https://github.com/leanprover-community/mathlib4"
rev = "master"

[[lean_lib]]
name = "ExampleCeti"
globs = ["ExampleCeti.*"]
leanOptions = { weak.linter.mathlibStandardSet = true, warningAsError = true }

[[lean_exe]]
name = "axioms"
root = "scripts.Axioms"

[[lean_exe]]
name = "module-system"
root = "scripts.ModuleSystem"
```

The executable sources arrive with the pinned project templates. Project-specific lint baselines
and roadmap maps live in `scripts/`; new projects start with empty policy files. An optional
`scripts/lint-docscan-sentinels.txt` sets a minimum declaration count on its first line and lists
known documented declarations, one per subsequent line.

For CSLib or another
upstream, obtain the toolchain from that upstream's selected commit instead and add its Lake
dependency before resolving the manifest. Do not copy a Mathlib-only manifest into a project
with additional direct dependencies.

Declare each relevant upstream using a `[[upstreams]]` table: its GitHub repository,
Lake package name and nominated branch, and whether to update it or scan for overlaps.
The upstreaming destination is a separate property. A project may depend on both CSLib
and TauCeti; Mathlib can be a transitive dependency. Set `update = true` only for direct
dependencies already present in the manifest. For inherited Mathlib set `update = false`;
`overlap` and `upstream_destination` remain independent choices. The Mathlib-only starter
must be adapted to the actual package rather than replacing its existing lakefile.

Lake resolves shared dependencies using its normal precedence rules, including direct
requirements taking priority over inherited ones. Different Mathlib pins in CSLib and Tau Ceti's
manifests are allowed. They are not proof that a combination is incompatible: the required build
and review decide that. Choose direct requirements and any overrides deliberately in the
human-owned lakefile. An existing direct override is preserved by automatic updates.

The updater selects forward commits on the configured branches, keeps other direct dependencies
fixed, and passes those immutable pins to Lake. It chooses the newest official Lean release or
release candidate declared by the base or any direct dependency; it never downgrades Lean.
Nightly and custom toolchains require a human decision. The resolver rebuilds the inherited
closure without running post-update hooks or fetching build caches. It does not edit lakefiles.

Inspect nominated branch heads without executing Lake or changing the checkout:

```sh
tauceti-tools upstreams inspect --conf tauceti.toml --checkout .
```

On Linux, install elan using <https://lean-lang.org/install/manual/> and bubblewrap using the
operating system's package manager (`sudo apt-get install bubblewrap` on Ubuntu). Commit the
project's Lake configuration and pins before preparing an update:

```sh
tauceti-tools upstreams update --conf tauceti.toml --checkout . --apply
git diff -- lake-manifest.json lean-toolchain
```

Alternatively open the content repository's **Actions → Update configured upstreams → Run
workflow**. The scheduled workflow installs its pinned elan and bubblewrap runtime, resolves
first, then obtains the automation App token and opens a dependency PR. Merge the regenerated
workflow when upgrading an existing project's Tools pin; updating an operator's CLI alone
does not change installed workflow callers.

Resolution runs with the committed base `lakefile.toml`, a seed manifest containing only the
authorized direct pins, and the selected toolchain file in a disposable bubblewrap workspace.
Only that workspace is writable; the official toolchain is read-only, credentials and host workspace are not
mounted, and the launcher has an empty environment. Network access is enabled for public Git
fetches. The small trusted Lean driver calls Lake's resolver with direct entries preseeded;
upstream Lean lakefiles are evaluated inside this boundary. Post-update hooks are not run.

For a pin-changing PR, the guard authorizes each changed direct revision as a forward commit
on its nominated branch. It then resolves the trusted base TOML with those exact direct revisions
and compares the complete resulting manifest (ignoring package-list order) and selected toolchain with the PR. Candidate
lakefiles, source, caches and inherited manifest entries are never inputs to resolution.
The comparison does not require that the PR include the latest default branch. The ordinary
build and review must pass before autonomous merging.

If resolution fails, inspect its Lake log and review the existing requirements. If the build
fails, source adaptation can proceed on the dependency PR while preserving the protected
requirements. To pause automatic bumps, set `update = false` on every enabled upstream entry
and merge that configuration change. This does not disable the guard. Lakefile edits still
require a human and are rejected by the automatic guard; there is no approval-based bypass.
Upstreams without reproducible dependency pins may need a new update PR if resolving their
unpinned requirements later produces a different manifest.

Keep forward-only dependency changes separate from source authoring. Protected lakefile
changes remain human-owned. Configure the build policy rather than removing guards to
make an update pass.

## Roadmaps

First complete the content Lean package and merge its toolchain, Lake configuration,
manifest and Lean sources into the configured content branch. A new roadmap package
copies that published toolchain and resolves the content repository as a dependency;
an unmerged content setup PR or local build does not satisfy this prerequisite.

In the roadmap repository create an active directory, for example `ExampleCetiRoadmap/`,
with one subdirectory per area. For a new empty roadmap, track `.gitkeep` files under both
`ExampleCetiRoadmap/` and `Completed/` so Git retains them. The labeling script reports
“No roadmap areas yet; nothing to label” until an area is added. Each area has `README.md`; `Suggested.lean` can give
target statements and examples. Completed areas may live under `Completed/`.

Human ownership applies to roadmap content. Generated `STATUS.md` and `PROGRESS.md`
have a narrowly constrained update/merge path. General authoring workers must not acquire
authority to edit roadmap targets through the reporting feature.

Where the content project uses the roadmap as a Lake dependency, pin it in the normal
manifest and update that dependency deliberately. Do not merge the two repositories merely
to avoid the dependency update.

## Versions

Discover the full SHA of the desired release or reviewed commit:

```sh
gh api repos/TauCetiProject/TauCetiTools/commits/main --jq .sha
gh api repos/TauCetiProject/TauCetiReview/commits/main --jq .sha
gh api repos/TauCetiProject/TauCetiProgress/commits/main --jq .sha
```

Enter those SHAs as `ref` in the respective configuration tables. Configuration is the
authority for version selection. GitHub workflow `uses:` references must contain literal
SHAs, so install/update those references together with the configuration. There is no
additional lock file.

Keep the progress generator and its roadmap merge gate at the same version. Keep the
review engine and its merge policy compatible. Do not use floating `@main` references in
workflows that receive private App credentials.

## Workflows

Clone the selected Tools revision into a separate trusted checkout. The project templates
live in `tauceti_tools/assets/project/`; shared script implementations are under
`tauceti_tools/assets/scripts/`. Use the selected version's template renderer to substitute
the project configuration, then inspect and commit the resulting callers and policy files:

```sh
python3 - <<'PY'
from pathlib import Path
from tauceti_tools.project import load
from tauceti_tools.templates import write_project
write_project(load('tauceti.toml'), Path.cwd())
PY
git diff
```

The selected Tools package must be installed in the Python environment used for this command.
Using `uv run --with git+https://github.com/TauCetiProject/TauCetiTools@SHA python ...`
selects a specific Tools version. Replace `SHA` with the actual configured full SHA.

Downstream setup includes Lean helper sources and project policy files. CI loads shared
script implementations from the pinned Tools checkout instead of committing compatibility
scripts for every feature. For local execution use `tauceti-tools script SCRIPT_NAME`.
Disabled optional workflows and infrastructure test fixtures are omitted; existing project
files are preserved. Generated external action references use fixed commit SHAs.
Content and roadmap setup also add the Lake build directory to `.gitignore`, preserving
existing rules.

The doctor's workflow preview prints the proposed diff and removes its temporary clone.
Preview and apply use the same branch name when the upstream base and generated changes
are unchanged. Apply targets the repository's default branch and removes the temporary
clone after opening the PR. If push or PR creation fails after committing, it prints and
retains the prepared checkout for recovery.

Commit the project configuration, CODEOWNERS and workflow changes on a branch and open a
PR against the upstream default branch. Review the generated changes as a human. If existing
branch rules block human-owned setup files, use the selected merger's explicit scope override
described under [Branch rules](#branch-rules). On a new repository, install setup before
requiring its check names.

The trusted build workflow selects immutable PR and base commits, establishes source scope,
attests Lake configuration, prepares integration and runs candidate Lean offline under the
existing sandbox. Shared drivers come from the workflow-pinned Tools version. Candidate
scripts, workflow edits and cached audit binaries cannot replace those trusted drivers.

The PR-status workflow maintains the labels workers use to select work. It is core
automation even when Zulip is off; the same reconciler skips notifications in that mode.

Project lint baselines and exceptions remain in project `scripts/`. The setup action overlays
shared implementations but preserves the selected project's policy data.

Review the generated diff before opening or merging a setup PR. Generated workflow callers
replace files at the same paths; preserve any additional existing project build or review
requirements in the reviewed result. A setup clone uses the maintainer's Git identity, including
checkout-local `user.name` and `user.email`. If these are unset, configure them in the content
checkout before preparing the PR:

```sh
git config user.name "YOUR FULL NAME"
git config user.email "YOUR VERIFIED GITHUB EMAIL"
```

Use your own name and a verified or GitHub-provided noreply address from
<https://github.com/settings/emails>.

## Actions

Reading or changing repository Actions settings requires repository admin access.
Use `gh repo view --json viewerPermission` to check your permission. If it is not
`ADMIN`, ask a repository administrator or organization owner to configure Actions.
Broader token scopes cannot grant permissions your account lacks. If you are already
an admin and the API returns 403, check your token's repository administration
permissions, organization authorization and policy.

Open `https://github.com/ExampleOrg/ExampleCeti/settings/actions`:

1. Under **Actions permissions**, enable Actions. If the organization allows only selected
   actions, allow the exact actions and reusable workflow repositories named by the installed
   templates, including TauCetiTools and TauCetiReview.
2. Keep the normal workflow token read-only by default. Individual trusted workflows declare
   the narrower permissions they require. Do not grant all PR jobs write access to compensate
   for a broken App setup.
3. Review external-contributor approval policy at the organization level. Trusted workflows
   use the base definition and sandbox candidate builds; do not move secret-bearing execution
   into an untrusted PR checkout.

Inspect the effective repository policy:

```sh
gh api repos/ExampleOrg/ExampleCeti/actions/permissions
gh api repos/ExampleOrg/ExampleCeti/actions/permissions/workflow
```

Organization restrictions take precedence. If the controls are unavailable, hand these exact
URLs and requested action/workflow repository names to the organization owner.

## Apps and credentials

Use project-owned Apps for write automation. Public App visibility permits installation on
other organizations; it does not grant that installation credentials or hosted infrastructure.
Do not give a downstream the private key of a shared multi-organization App.

Register at `https://github.com/organizations/ExampleOrg/settings/apps/new` as an owner or
App manager. For a personal owner use `https://github.com/settings/apps/new`.
Repository maintain or admin access alone does not grant access to organization App
registration. If the page returns 404, ask an organization owner to create the App or
grant the appropriate App manager role. App management and installation permission are
separate; an organization owner may also need to approve the installation. See
[GitHub's App manager permissions](https://docs.github.com/en/organizations/managing-programmatic-access-to-your-organization/adding-and-removing-github-app-managers-in-your-organization).

| Role | Repository permissions | Install on | Webhook |
|---|---|---|---|
| Automation | Contents, pull requests, issues, checks, statuses, Actions and workflows: write; variables: read | Content; roadmap if progress enabled; Data if CI review persistence enabled | Inactive unless a selected feature needs it |
| Claims admission | Administration and contents: write | Claims repository only | Inactive |
| Collector | Contents, Actions and pull requests: read | Configured source repositories | Inactive |
| State mirror | Contents, issues, pull requests, checks, statuses and Actions: read | Enrolled content repository | State service endpoint |
| Bors | Contents, pull requests, issues and statuses: write; checks, Actions and variables: read | Its content repository | Bors endpoint |

All roles also have GitHub's implicit Metadata read permission. The manifest generator lists
the exact API permission names. Separate roles prevent claims Administration authority from
being installed on the content repository.

For each App enter:

- **GitHub App name:** a unique name such as `ExampleCeti-automation`.
- **Homepage URL:** the content repository, or the service URL for Bors/state.
- **Where can this GitHub App be installed?** **Any account**.
- For non-webhook roles, clear **Active** in Webhook.
- For Bors, **Callback URL:** `https://bors.example.org/auth/github/callback`;
  **Webhook URL:** `https://bors.example.org/webhook/github`; enter a privately generated
  webhook secret and keep **Active** enabled.
- For a state mirror use its actual `/webhook/github` URL and private webhook secret.

Under Permissions choose the table's repository permissions. Select webhook events only after
permissions are set. Bors subscribes to PRs, issue comments, PR reviews, check runs/suites,
statuses and pushes; installation changes are delivered by GitHub. The state mirror also
subscribes to issues, review comments/threads, workflow runs/jobs, labels, milestones,
repository changes, create/delete and merge groups as supported by its setup manifest.

Click **Create GitHub App**, record the numeric App ID, click **Generate a private key**,
and save the downloaded PEM privately. Under **Install App**, choose ExampleOrg, **Only
select repositories**, select the exact repositories above, then **Install**. Approve later
permission changes on the installation when prompted; editing registration alone does not
upgrade an existing installation's grant.

### Secret and variable inventory

Store private source files under `~/.config/tauceti/projects/exampleorg__exampleceti/`, mode
0700 for the directory and 0600 for files. Values travel through stdin, not command arguments.

| Name | Source | Location / consumer |
|---|---|---|
| `APP_ID` | Automation App numeric ID | Content/roadmap Actions secrets; trusted automation |
| `APP_PRIVATE_KEY` | Downloaded automation App PEM | Content repository |
| `SYNC_BOT_APP_ID` | Automation App numeric ID | Roadmap Actions variable |
| `SYNC_BOT_APP_PRIVATE_KEY` | Automation App PEM | Roadmap Actions secret |
| `CLAIMS_APP_ID`, `CLAIMS_APP_PRIVATE_KEY` | Separate claims App | Content Actions secrets; admission workflow |
| `COLLECTOR_APP_ID`, `COLLECTOR_APP_PRIVATE_KEY` | Collector App, when needed | CI archive Actions secrets |
| `LAKE_CACHE_KEY` | Cache provider's project-scoped write credential | Trusted cache publication workflow only |
| `ZULIP_EMAIL`, `ZULIP_API_KEY` | Chosen bot's credentials | Notification workflow secrets |
| `FLEET_READ_TOKEN` | Downstream fleet's read credential | Runner selection when enabled |
| `DATABASE_URL` | Hosted grant or custom PostgreSQL | Bors runtime secret, never public configuration |
| `GITHUB_INTEGRATION_ID`, `GITHUB_INTEGRATION_PEM` | Bors App ID, base64 PEM | Bors runtime |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | Bors App OAuth credentials | Bors runtime |
| `GITHUB_WEBHOOK_SECRET` | Random private value also entered in App webhook | Bors runtime |
| `SECRET_KEY_BASE` | Random application secret | Bors runtime |
| State App key, webhook secret, client/admin tokens | State setup files | State deployment secret store |

For example, after saving the automation key as `automation/APP_PRIVATE_KEY`:

```sh
chmod 600 ~/.config/tauceti/projects/exampleorg__exampleceti/automation/APP_PRIVATE_KEY
gh secret set APP_PRIVATE_KEY --repo ExampleOrg/ExampleCeti \
  < ~/.config/tauceti/projects/exampleorg__exampleceti/automation/APP_PRIVATE_KEY
```

The browser equivalent is **Settings → Secrets and variables → Actions → New repository
secret**. Enter the table's exact name and the private file contents. Organization secrets
must include the destination repository under their access policy. Environment secrets are
available only to jobs selecting that environment and may require approval. Secrets are passed
explicitly to reusable workflows; they do not automatically follow a workflow call.

Use Actions variables for non-secret values, for example `MERGE_BACKEND` (`queue` or `bors`),
`LAKE_CACHE_ARTIFACT_ENDPOINT_PUBLIC`, `LAKE_CACHE_REVISION_ENDPOINT_PUBLIC` and deliberately
enabled fleet/runner settings. Set `CI_REVIEW_ENABLED` only if paid CI review is intended;
operator-run subscription review does not require provider API keys in the repository.

## Branch rules

The `scope` check restricts which files automation may merge. It does not turn ordinary
human review into permission for a bot to change infrastructure.

| Project setup | Actions variable | Normal merging | Human scope override |
| --- | --- | --- | --- |
| `merge.backend = "github-queue"` | `MERGE_BACKEND=queue` | GitHub merge queue | An administrator performs an admin merge. |
| `merge.backend = "bors"` | `MERGE_BACKEND=bors` | Bors | An authorized administrator uses `bors merge`. |

Open `https://github.com/ExampleOrg/ExampleCeti/settings/rules`. Check every rule applying to
the default branch, including organization rulesets and any classic protection rule.

For either setup, require pull requests, code-owner review for human-owned files, and checks
**build**, **bump-guard** and **scope** from their expected GitHub Actions producer. Install the
workflows first and let an ordinary build run before selecting their check names.

With the **GitHub merge queue**, enable **Require merge queue** and set `MERGE_BACKEND=queue`.
The build workflow must handle `merge_group` and build the combined commit. On rulesets that
block merges, add **Repository admin** to the bypass list with **For pull requests only**.
Keep the bypass restricted to administrators. Preserve separate deletion and force-push
protection. If classic branch protection also applies, allow administrator overrides there too.

A PR touching restricted files keeps `scope` red and cannot merge automatically. After
reviewing its changes and build result, an administrator can explicitly merge it through
GitHub's UI or `gh pr merge PR_NUMBER --repo ExampleOrg/ExampleCeti --admin --squash`.
This bypasses the normal branch rules; it does not claim that `scope` passed. Ordinary review
alone is not a scope override. This setup needs no Bors deployment or Bors approval.

With **Bors**, disable the GitHub merge queue and set `MERGE_BACKEND=bors` after configuring the
service. The Bors App needs **Always allow** on rulesets that otherwise prevent it from
updating the base branch with its tested staging commit. Keep deletion and force-push
protection in separate rulesets without adding a Bors bypass. Replace overlapping classic
protection with an equivalent ruleset that permits this App. Only the project Bors App should
bypass rules that block merging; administrators approve restricted paths through `bors merge`.
Configure `services.bors.url` and `services.bors.repository_id` from the Bors repository page. Doctor identifies the Bors App as `REPOSITORY-bors`; if you
registered a different slug, set `services.bors.app_slug` to that actual slug.

An authorized administrator uses `bors merge` to approve restricted paths. Trusted CI verifies
Bors's approval for the exact PR head and merge base, including the member PRs of a staging
batch. Changed heads, revoked approvals and users who no longer have admin access cannot
reuse an old approval. The other scope guards and build checks still apply.

Doctor checks these settings and installed workflow configuration directly. It does not scan
PRs to infer merge policy. If it cannot read a bypass list or protection setting, it identifies
the setting and asks a repository administrator to inspect it.

Existing `github-direct` configurations must choose one of these two setups. For the merge
queue, edit `merge.backend` to `github-queue`, enable the queue and set `MERGE_BACKEND=queue`.
For Bors, configure the service and its branch rules before selecting `bors` and setting
`MERGE_BACKEND=bors`. Review and publish the configuration and matching generated workflows.
A Tools-version bump alone leaves an old `github-direct` configuration unsupported in CI.
Prepare the configuration and workflow changes together, and coordinate the repository variable
and branch rules with the maintainer before installing the new setup. Doctor does not silently
convert a configuration or change repository settings. If your repository cannot use GitHub's
merge queue, configure Bors.

The roadmap repository separately protects authored roadmap text. Progress automation may
merge only its validated generated report updates. Data/CI archive permissions are separate
from authority over content or roadmaps.

## Contributors and workers

Install `gh`, `git`, `uv`, `jq`, an appropriate Lean toolchain, and the chosen agent CLI.
Authenticate under the intended contributor account:

```sh
gh auth login
codex login
# or: claude auth login
tauceti --conf ./tauceti.toml status
tauceti --conf ./tauceti.toml work --only review
```

The worker creates/uses the contributor's fork. It needs permission to push to that fork,
not write access to canonical content. A token restricted to the canonical repository is
insufficient for authoring. Subscription credentials remain local; Bubble's proxy scopes must
use the configured project. Provider quota is shared across projects using that account.

Persistent worker definitions add `conf = "/absolute/path/to/tauceti.toml"` or a canonical
HTTPS URL. Worker IDs are project-local for working data. In one shared manager configuration,
use distinct definition IDs so management commands remain unambiguous.

## Extras

Each optional feature has an explicit choice. `off` means it is intentionally unused, rather
than a partially configured hosted service. Install only the workflows and credentials of
enabled features.

### Claims

Set `repositories.claims` to the separate writable namespace and enable claims admission.
Install the claims App only there, place its credentials in the content admission workflow,
and keep Actions disabled on the claims repository. Run the admission workflow in dry-run
mode first to inspect the intended contributors. Contributors without shared access continue
coordinating within their own fork. No project uses Tau Ceti's namespace by default.

### Archives

Set `repositories.data` and `repositories.ci` and enable their features. Review records are
written to a local outbox before best-effort synchronization to Data. Keep each project's
destination separate. CI collection reads explicitly listed repositories and writes append-only
records to CI; derived databases can be rebuilt from them. A collector App with extra read
quota is optional for small projects. Archive unavailability must not invalidate a live review.

### Documentation and progress

Publish documentation for the actual configured Lean library. Set `publication.docs_url`
to the resulting API documentation root. Enable Progress only after that publication is
working. Reports identify the commit actually documented; a branch name alone is not evidence
that docs have been published. Install the Progress repository's roadmap merge templates at
the same SHA as the generator, with the configured roadmap layout and repository.

For a downstream, select a full commit SHA compatible with `lean-toolchain` in
`[tools.docgen]` (`repo = "leanprover/doc-gen4"`, `ref = "FULL_COMMIT_SHA"`). The generated
`api-docs.yml` builds a temporary nested documentation project from that revision's lockfile
and the library's exact dependency pins. It generates an import-all root without changing
the library root. It publishes a plain API reference at `/docs`, including `SOURCE_SHA`,
then advances `docgen` to that published commit. No Tau Ceti website sources are required.

Create the Pages site with `gh api --method POST repos/OWNER/PROJECT/pages -f build_type=workflow`.
If the site already exists, use `--method PUT` instead. Alternatively open **Settings → Pages →
Build and deployment → Source → GitHub Actions**. Dispatch `api-docs.yml` and inspect both
**Generate API documentation** and **Deploy to GitHub Pages**. A skipped deployment is not
evidence of publication.

For progress, extend the existing automation App installation to the roadmap repository at
`https://github.com/settings/installations`: **Configure → Repository access → Only select
repositories**, add the roadmap alongside content, then **Save**. Upload the existing key as
the roadmap's `SYNC_BOT_APP_PRIVATE_KEY`, and set its `SYNC_BOT_APP_ID` variable to that App's
numeric ID. A second automation App is unnecessary.

### Roadmap authoring (WIP)

[TauCetiRoadmapWorker](https://github.com/TauCetiProject/TauCetiRoadmapWorker) automates
roadmap planning and authoring: humans file targets, agents prepare designs and packages,
independent agents review them, and accepted packages become draft PRs in the roadmap
repository. Humans review and merge those final PRs. Its job board is
[TauCetiRoadmapJobs](https://github.com/TauCetiProject/TauCetiRoadmapJobs).
This is an **optional extra, still under development**, introduced in the
[downstream setup discussion](https://leanprover.zulipchat.com/#narrow/channel/638283-Tau-Ceti-scientific-advisory-board/topic/Tau.20Ceti.20downstreams/near/633525014).
Existing human-authored roadmaps need none of this. It is separate from Progress, which reports
implementation progress rather than authoring targets.

For a downstream, prepare your own job board, for example
`ExampleOrg/ExampleCetiRoadmapJobs`, using the worker's current
[operator and maintainer guide](https://github.com/TauCetiProject/TauCetiRoadmapWorker/blob/main/docs/OPERATORS.md).
Use its
[configuration example](https://github.com/TauCetiProject/TauCetiRoadmapWorker/blob/main/tauceti-roadmap.toml.example)
to create a separate, human-owned `tauceti-roadmap.toml`. In that file:

- Set `repositories.content = "ExampleOrg/ExampleCetiRoadmap"` (the roadmap repository)
  and `repositories.library = "ExampleOrg/ExampleCeti"` (the Lean library).
- Set `repositories.board` to your job board. Choose your own packages repository and claims
  namespace, or use the documented board/fork defaults. Do not inherit Tau Ceti's destinations.
- Match the roadmap paths and default branch to your roadmap repository. Choose authorized
  target filers, operators and independent reviewers, and keep `roadmap.open_as_draft = true`
  and `redteam.ready_after = false` for human review.
- Protect the configuration, prompts and merge order with human CODEOWNERS. Configure board
  maintenance (`board sync`, `board intake`, `board stale`) using the current maintainer guide;
  running an authoring worker alone does not advance the board.

Publish that configuration on the job board's default branch. In the content project's
`tauceti.toml`, add the URL to the existing `[roadmap]` table and enable the feature in the
existing `[features]` table (do not duplicate those tables):

```toml
[roadmap]
# Keep existing active_dir and completed_dir settings.
authoring_conf_url = "https://raw.githubusercontent.com/ExampleOrg/ExampleCetiRoadmapJobs/main/tauceti-roadmap.toml"

[features]
# Keep existing feature choices.
roadmap_authoring = true
```

On each operator host, authenticate `gh` and the chosen agent CLI, then install and check:

```sh
uv tool install git+https://github.com/TauCetiProject/TauCetiRoadmapWorker
tauceti-roadmap --conf https://raw.githubusercontent.com/ExampleOrg/ExampleCetiRoadmapJobs/main/tauceti-roadmap.toml doctor
```

The roadmap worker's own `doctor` checks its host tools, repository access, claims, state,
sources cache, PDF extraction/OCR and sandbox. Follow its current guide for Lean package
caches and source access; downloaded texts stay in the operator's local cache.
`tauceti-tools doctor --check features.roadmap_authoring` prints this handoff and the configured
command. It leaves enabled authoring **pending**, since it does not run the dedicated doctor
or establish runtime health. It installs nothing and starts no workers or maintenance.
`--no-extras` omits the check; `features.roadmap_authoring = false` (the default) disables it.
The configuration and procedures are WIP: consult the linked upstream guides during upgrades
and revise this section as the integration develops.

### Announcements

Set an explicit Zulip site, channel and topic. Create a bot on that site, save its email and
API key as the notification workflow's secrets, and confirm membership in the destination.
Do not reuse Tau Ceti's topic accidentally. Progress prose is machine-written and is not a
human-reviewed roadmap; its publication is separately enabled.

### Runners and audits

The starter selects ordinary GitHub runners. Namespace and self-hosted fleets require the
downstream's account, labels and read credentials. Do not copy Tau Ceti's runner group names.
Security-audit/release workers are administrator features and use the configured source scope,
protected audit branch and explicit alert destination. Never bypass a blocked audit by changing
worker identity.

## Cache

`services.cache.mode = "off"` leaves ordinary local builds available. Hosted or custom cache
requires explicit artifact/revision endpoints and a service name. Anonymous read access and
trusted publication are separate permissions. Obtain a scoped write key from the provider and
store it only in the trusted publication job; ordinary PR builds never publish shared outputs.
Use the configured repository identity in cache mappings. No downstream inherits Tau Ceti's
writer credentials or bucket permissions.

## Bors

Bors is always operated by the downstream. `hosted` supplies its PostgreSQL database as a
separate Neon project managed by the Tau Ceti operator. `custom` supplies PostgreSQL from the
downstream's provider. `off` keeps the selected GitHub merge backend. The database option does not select where
the runtime runs.

Configure the Bors URL, deployment (`docker` or `cloudflare`), project repository, default
branch and expected review App ID. Its App receives signed webhooks and holds the authority
to construct/test/promote candidates. Approval and required checks remain head-bound.

### Ordinary container host

Use the released Bors image pinned by digest. Run one application replica. Store runtime
credentials in `bors.private.env`, mode 0600, outside version control. The generated Compose
template binds HTTP to localhost; Caddy or an existing HTTPS proxy terminates TLS and blocks
public `/internal/*` routes. Set `PUBLIC_HOST`, `PUBLIC_PROTOCOL=https`, `PUBLIC_PORT=443`.

Encode the PEM privately and generate the application secret:

```sh
openssl base64 -A -in bors-app.private-key.pem > bors-app.private-key.base64
openssl rand -base64 64 > bors-secret-key-base
chmod 600 bors-app.private-key.base64 bors-secret-key-base
docker compose up -d
curl --fail https://bors.example.org/health/
```

For Docker, remove the Neon URI query suffix `?sslmode=require&channel_binding=require`
(or its `sslmode=require` variant) before saving `DATABASE_URL`; the Compose template sets
`DATABASE_USE_SSL=true` for Postgrex. The Cloudflare upload helper does this normalization.
Copy the other private file contents into their corresponding runtime variables. Put the numeric
App ID in `GITHUB_INTEGRATION_ID`; put the automation/review App ID in `TAUCETI_REVIEW_APP_ID`.
Set `BORS_PROJECT` and `BORS_BASE_BRANCH` to the configured content repository and branch.
Register the App webhook and callback URLs as above. Sign in to Bors and synchronize the
installation so the repository is registered in its database.

Schedule the existing authenticated `/internal/merge-reconcile` call once per minute through
localhost. The request uses `x-bors-internal-secret` with the private webhook secret. The generated host
script reads that value from a mode-0600 file and supplies it directly as an HTTP header,
never a visible command argument. Install its cron line with `crontab -e`.

### Cloudflare Containers

The downstream creates its own Cloudflare account, enables Containers/Workers billing,
registers the service domain and logs in using `npx wrangler login`. Use the pinned Bors
checkout and its deployment files. Set its account ID, Worker/domain, queue names and R2
bucket names to downstream-owned resources. Configure the project/review identity and upload
runtime secrets using the provided upload helper from private files. Run `npx wrangler deploy`
using a host with Docker or the supported cloud build integration. Existing Cloudflare
scheduling calls the same authenticated maintenance endpoint.

The Cloudflare webhook queue remains part of that deployment. Ordinary Docker uses Bors's
existing webhook handler. No additional PostgreSQL webhook-inbox system is required here.

### Database provision

Hosted enrollment requests identify the content repository, canonical configuration and
maintainer. The operator approves the request and creates one Neon project for that downstream.
The operator retains management/API credentials and records its project ID and resource
settings privately. The downstream receives only its database connection credentials through
a private channel. Never place the connection URI in an issue, public configuration or PR.

For custom Neon, open `https://console.neon.tech`, select the intended organization, choose
**New project**, enter the project name and region, and select the supported PostgreSQL
version. Record compute minimum/maximum, suspension behavior and history retention explicitly.
Choose the **direct** connection endpoint for the current Bors driver, rather than transaction
pooling. Set the driver-supported TLS configuration; do not assume libpq URI parameters are
interpreted by the Elixir driver. A custom ordinary PostgreSQL service is also supported.

Database schema migrations run through the Bors release's existing migration command.
Keep database credentials private and verify health through the Bors runtime, rather than
printing the URI. Reuse an existing project on a repeated provisioning run.

### Enable merging

Keep the selected GitHub merge backend active while registering and inspecting Bors. Confirm App delivery,
repository synchronization and a successful pilot staging build using the required
`build`, `bump-guard` and `scope` checks. Then explicitly select the Bors merge backend in
configuration and the corresponding repository variable/rules. A responding health endpoint
alone is insufficient reason to change merge authority.

## GitHub state

The hosted Tau Ceti mirror publishes read-only queries for `TauCetiProject/TauCeti`
without a client token, allowing ordinary Tau Ceti workers to use it by default.
Service operators may explicitly publish other public repositories using the
`PUBLIC_READ_REPOSITORIES` JSON name list. A name must also exist in
`ALLOWED_REPOSITORIES`; publishing a name does not enroll a repository or expose
administrative, health, changes or activity endpoints. Only publish repositories
whose mirrored data is intended to be public. The default custom deployment
continues to require scoped client credentials. Anonymous queries only read
existing mirrored snapshots; they cannot request hydration or create cursor
objects. Responses are inline and bounded at 16 MiB. Missing coverage or a
larger response reports unavailable so workers can use their normal fallback.
An absent GitHub state section defaults to the public mirror for the canonical
Tau Ceti content repository; an explicit `mode = "off"` still disables it.

`off` uses direct GitHub reads. `hosted` requests enrollment in the operator's shared mirror.
`custom` deploys the same service in the downstream's Cloudflare account.

The operator keeps an explicit allowlist of repositories and their installations, issues
repository-scoped client credentials and retains its App key. Installing its public App does
not grant access to unapproved repository scopes. Enroll only the required repositories.

For custom deployment use `services/github-state/scripts/setup.py` from the selected Tools
checkout. Run `prepare` with the chosen repository, service name, URL and account; register
the generated App form; exchange its one-time code privately; install it on the selected
repository; run `inspect`, `deploy`, then `bootstrap`. The setup script prints the concrete
registration and installation links. Its credential directory is private.

The deployment uses Cloudflare Workers, repository Durable Objects, installation schedulers,
Queues and R2. Preserve resource identities and state across code upgrades. Configure the
client with the service URL and a private token-file path. The service receives webhooks and
holds read-only installation credentials; mutations continue using the worker's identity.

### Hosted repository enrollment and client credentials

The downstream administrator opens
https://github.com/apps/tauceti-github-state/installations/new, selects the downstream
account, chooses **Only select repositories → ExampleCeti**, and clicks **Install**.
The infrastructure operator separately approves the repository. Installing the App alone
does not register the repository with the service or grant a worker access to it.

The client credential is a random bearer token generated by the downstream administrator.
It is not a GitHub personal access token or App installation token. The service hashes each
incoming bearer token with SHA-256 and looks up that hash in its `CLIENT_TOKENS` secret.
The matching entry's `repositories` list is the token's scope. The service also requires
the repository to appear in `ALLOWED_REPOSITORIES`, with its numeric repository ID and
the App installation ID for the downstream account.

On the downstream worker machine, generate a separate token for this repository. Re-running
this command keeps the existing token. It prints only the grant containing its hash:

```sh
python3 - <<'PY'
import hashlib, json, os, secrets
from pathlib import Path

path = Path.home() / ".config/tauceti/projects/exampleorg__exampleceti/github_state/client-token"
path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
path.parent.chmod(0o700)
if not path.exists():
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        output.write(secrets.token_urlsafe(48) + "\n")
path.chmod(0o600)
grant = {"hash": hashlib.sha256(path.read_text().strip().encode()).hexdigest(),
         "repositories": ["ExampleOrg/ExampleCeti"]}
print(json.dumps(grant))
PY
```

The downstream administrator obtains the installation ID from their account's **Installed
GitHub Apps → tauceti-github-state → Configure** page: the number in
`/settings/installations/NUMBER`. Send that installation ID, the printed grant object,
repository name and canonical configuration URL to the infrastructure operator in the
enrollment request. These are not secrets. Keep the raw token on the downstream machine;
the operator does not need it. The operator cannot open another account's installation
settings. Use that account's installation, rather than the operator's original installation.

Alternatively, Tools can generate the same private token and prepare the enrollment
request in local files. From the downstream checkout, replace `INSTALLATION_ID` with
the number above:

```sh
tauceti-tools github-state request --installation INSTALLATION_ID
```

This reuses an existing token, writes `client-grant.json` and `access-request.md` beside
it, and prints a `gh issue create --body-file ...` command. Review the saved request
before submitting it. The request contains only the hash, repository scope, installation
ID and public configuration URL. It does not post an issue or change the service.

After approving the request, the operator obtains the repository ID:

```sh
gh api repos/ExampleOrg/ExampleCeti --jq .id
```

In https://dash.cloudflare.com/, select **Workers & Pages → tauceti-github-state → Settings →
Variables and Secrets**. Edit `ALLOWED_REPOSITORIES`, preserving its existing entries, and
append this object with the two actual decimal IDs as JSON strings:

```json
{"id":"REPOSITORY_ID","name":"ExampleOrg/ExampleCeti","installation":"INSTALLATION_ID"}
```

Deploy the change. In the same panel edit the **Secret** `CLIENT_TOKENS`. The operator
retains the complete JSON array privately at
`~/.config/tauceti-github-state/client-tokens.json`, with file permissions `0600`. Append
the request's grant object to that complete array and deploy the entire updated array.
The new value is an array, even for a single client; each entry has the `hash` and
`repositories` fields shown above. Reuse an identical grant on repeated enrollment.

[Cloudflare hides stored secret values](https://developers.cloudflare.com/workers/configuration/secrets/);
the dashboard cannot recover the existing array. If the service still has exactly the
initial `setup.py` client and has never enrolled others, reconstruct its initial array
from the original private token:

```sh
python3 - <<'PY'
import hashlib, json
from pathlib import Path
path = Path.home() / ".config/tauceti-github-state/client-token"
print(json.dumps([{"hash": hashlib.sha256(path.read_text().strip().encode()).hexdigest(),
                   "repositories": ["TauCetiProject/TauCeti"]}]))
PY
```

Otherwise recover the complete array from the operator's private backup before editing.
Replacing it with just the new grant revokes other clients. Keep the updated array in the
private file for subsequent enrollments.

The equivalent local-file helper, for a service with only its original client, is:

```sh
tauceti-tools github-state initial-grants
```

It reads `~/.config/tauceti-github-state/client-token` and writes a new private
`~/.config/tauceti-github-state/client-tokens.json`, refusing to overwrite an existing file.
Use `--token-file`, `--repository` and `--output` if the original client's token, repository
(default `TauCetiProject/TauCeti`) or retained-array path differs. Append the downstream
grant to the complete array before uploading it; this command does not upload anything.

For later code updates run `npx wrangler deploy` from the service checkout. Its `keep_vars`
setting preserves dashboard variables, and secrets persist. Do not rerun
`scripts/setup.py deploy` after enrolling downstreams: that initial-deployment helper
replaces the allowlist and client grants with its single configured repository.

After both settings are deployed, the operator starts repository initialization using
the existing service admin credential. Adjust its private file path if necessary:

```sh
python3 - <<'PY'
import json, urllib.request
from pathlib import Path

request = urllib.request.Request(
    "https://github-state.taucetiproject.org/admin/bootstrap",
    data=json.dumps({"repository": "ExampleOrg/ExampleCeti"}).encode(),
    headers={
        "Authorization": "Bearer " + (Path.home() / ".config/tauceti-github-state/admin-token").read_text().strip(),
        "Content-Type": "application/json",
        "User-Agent": "TauCetiWorker/1.0",
    },
)
with urllib.request.urlopen(request, timeout=30) as response:
    print("Bootstrap HTTP", response.status)
PY
```

The equivalent operator command is:

```sh
tauceti-tools github-state bootstrap --conf https://raw.githubusercontent.com/ExampleOrg/ExampleCeti/main/tauceti.toml
```

It reads the same private admin token. Its destination defaults to the operator's
`https://github-state.taucetiproject.org` service, independently of the downstream
configuration; redirects are refused. For another service the operator supplies
`--url https://THE_OPERATOR_SERVICE`. Use `--admin-token-file PATH` if that credential
is stored elsewhere.

The expected response is HTTP 200. Initialization continues in the background; authenticated
`GET /v1/health?repository=ExampleOrg%2FExampleCeti` must eventually show that repository,
its bootstrap inventory and writable storage.

Tell the downstream administrator when approval and initialization are complete. Their
client token is already stored privately under
`~/.config/tauceti/projects/exampleorg__exampleceti/github_state/client-token`. Set
`services.github_state.url` to the hosted endpoint and pass `--github-state-token-file`
with that path when starting workers. The administrator may instead set
`TAUCETI_GITHUB_STATE_TOKEN_FILE` for diagnostics. The App private key and service admin
token stay with the operator; neither is a downstream client credential. Raw tokens never
belong in project configuration or infrastructure-request issues.

Bors observations are optional. Configure an approved receipt endpoint for the correct project
and branch, or a Cloudflare service binding. Absence of Bors does not impair GitHub mirroring.

## Verification

Configuration checks and successful workflow runs answer different questions. Doctor can
check that a secret exists, but GitHub hides its value. It separately reports whether an
existing workflow has successfully used the App credentials. You can leave that use unchecked
until the next normal run; there is no need to create a PR merely to satisfy doctor.

Inspect existing runs with:

```sh
gh workflow list --repo ExampleOrg/ExampleCeti
gh run list --repo ExampleOrg/ExampleCeti --limit 10
gh run view RUN_ID --repo ExampleOrg/ExampleCeti --log
gh api repos/ExampleOrg/ExampleCeti/actions/runs/RUN_ID/jobs
```

Substitute an actual run ID from the listing. Inspect the credential-consuming step, not just
the overall green conclusion: a skipped step or authentication fallback leaves that credential
unchecked. Normal content changes trigger pr-build; review and build changes trigger merge automation.
After fixing credentials, rerun the existing workflow. Doctor checks the rerun and its token
step rather than rejecting it because the original run predates the credential update. A
newer skipped run does not hide a successful rerun among the recent workflow records. This
optional credential-use check is separate from checking merge settings. A skipped step or an
inaccessible record is reported as unchecked, rather than a configuration
error. A step that actually failed is reported separately.
Use `gh workflow run WORKFLOW --repo ... --ref main` only where the existing workflow supports
manual dispatch, supplying its declared inputs.

Secret-list APIs show names/metadata, not stored values. A secret can be present and expired,
or unavailable to a particular workflow. Inspect scope and its actual consumer. An API error
does not mean an empty repository or an absent secret.

When waiting for a run, start one monitor and let it complete:

```sh
gh run watch RUN_ID --repo ExampleOrg/ExampleCeti --exit-status --interval 120
```

## Operation and recovery

If Bors explicitly reports the requested staging batch as canceled, a successful
build's finalizer finishes with `batch withdrawn; no verdict posted`. It removes
the pending finalization or success intent without posting a red or green
`build` commit status. This is a completed finalizer job, not a successful build
verdict for the withdrawn batch. Missing, malformed or unavailable Bors responses
do not prove withdrawal; real build and cache publication failures retain their
failure behavior, including a sandbox build that was canceled before completion.

Review upgrades as configuration/workflow PRs. Update common pins and their literal callers
together. Never point unfinished publication records at a new destination during an upgrade.
Archive failures retain local outboxes; retry synchronization after restoring access.

For App rotation generate a new key, install it in the private secret store, confirm a real
consumer succeeds, and revoke the old key. Database rotation is coordinated with the runtime
operator. Hosted database recovery/handover uses the recorded Neon project identity and its
retention/export facilities; do not recreate a project to repair a credential problem.

To remove an optional service, first stop its publishers/consumers and select its disabled
mode. Revoke scoped access and decide separately whether to retain or delete durable records.
Changing configuration never implies deleting a database, archive or deployment.

### Operator database commands

The infrastructure operator keeps a private registry, separate from every public repository.
For example, save `~/.config/tauceti/neon-projects.json` with mode 0600:

```json
{
  "ExampleOrg/ExampleCeti": {
    "approved": true,
    "org_id": "THE_OPERATOR_NEON_ORGANIZATION_ID",
    "region": "aws-us-east-1",
    "pg_version": 18,
    "min_cu": 0.25,
    "max_cu": 0.25,
    "suspend_timeout_seconds": -1,
    "history_retention_seconds": 604800
  }
}
```

In the Neon console select the operator organization, open **Settings → API keys**, create
an organization API key for this administration, and save it privately as
`~/.config/tauceti/neon-api-key`. Copy `org_id` from **Settings → General information**
([Neon organization instructions](https://neon.com/docs/manage/orgs-api)). The following command previews the resource settings; it
creates nothing:

```sh
tauceti-tools neon provision --conf ./tauceti.toml \
  --registry ~/.config/tauceti/neon-projects.json \
  --token-file ~/.config/tauceti/neon-api-key
```

After confirming the project, paid organization and capacity, run the same command with
`--apply --output ~/.config/tauceti/exampleceti-database-url`. The registry records the Neon
project ID. Repeating the operation reuses that identity or discovers the existing named
project; an uncertain create response requires inspection before retrying. The output file
contains the direct URI and is mode 0600. Send only that credential to the downstream privately.
Keep the management key with the operator.

Use `neon inspect` with the same configuration, registry and token-file arguments to print
resource identity without credentials. Record the actual branch ID and database role in the
private registry as `branch_id` and `role`. `neon rotate` previews password rotation; add
`--apply --output NEW_PRIVATE_FILE` only while coordinating the runtime secret change with its
operator. `neon handover` prints the existing project's console link and transfer/export steps.
No handover command deletes a database.

### Concrete Cloudflare Bors installation

Select a full Bors commit SHA in `[tools.bors]` with repository `TauCetiProject/bors-ng`.
Clone that revision in a deployment directory, and install its Node dependencies:

```sh
gh repo clone TauCetiProject/bors-ng bors
git -C bors fetch origin SELECTED_FULL_BORS_SHA
git -C bors checkout --detach SELECTED_FULL_BORS_SHA
cd bors
npm ci
npx wrangler login
```

Copy `wrangler.jsonc` to `wrangler.downstream.jsonc`. Set `account_id` to the account ID shown
on the Cloudflare dashboard, `name` to `exampleceti-bors`, and `routes[0].pattern` to
`bors.example.org`. Keep `custom_domain: true`. Change both producer and consumer queue names
to `exampleceti-bors-webhooks`, the dead-letter queue to `exampleceti-bors-webhooks-dlq`, and
the R2 bucket to `exampleceti-bors-webhooks`. Keep the existing Durable Object binding name,
class and migration tag. Create those resources:

```sh
npx wrangler queues create exampleceti-bors-webhooks-dlq
npx wrangler queues create exampleceti-bors-webhooks
npx wrangler r2 bucket create exampleceti-bors-webhooks
```

Add a top-level `vars` object containing `BORS_PROJECT: "ExampleOrg/ExampleCeti"`,
`BORS_BASE_BRANCH: "main"`, `BORS_STAGE_DISPATCH_PROJECT: "ExampleOrg/ExampleCeti"`,
`PUBLIC_HOST: "bors.example.org"`, and `BORS_SPECULATIVE_TRAINS: "false"`.
Set `TAUCETI_REVIEW_APP_ID` to the automation App's numeric ID and `BORS_REVIEW_LOGIN`
to its exact `APP_SLUG[bot]` login. These are public identifiers, not keys.
Exact-run cancellation and speculative trains remain Tau Ceti-only; downstream deployments
do not need cancellation actor or workflow IDs.
The repository ID used in a Bors observation URL is instead Bors's own database repository ID,
visible in its `/repositories/NUMBER` UI. Set `services.bors.repository_id` to that number;
the scope approval check uses it to read the correct project's approval records.

Save the App's client ID, client secret, PEM and webhook secret in private files. Upload:

```sh
python3 deploy/cloudflare/upload_secrets.py \
  --name exampleceti-bors --config wrangler.downstream.jsonc \
  --database-url-file /PRIVATE/database-url \
  --webhook-secret-file /PRIVATE/github-webhook-secret \
  --github-client-id APP_CLIENT_ID \
  --github-client-secret-file /PRIVATE/github-client-secret \
  --github-app-id NUMERIC_APP_ID \
  --github-private-key-file /PRIVATE/github-private-key.pem \
  --secret-key-base-file /PRIVATE/secret-key-base
npx wrangler deploy --config wrangler.downstream.jsonc
curl --fail https://bors.example.org/health/
```

The upload helper creates the application secret if its private file does not exist. It
normalizes a direct Neon URI for the Postgrex driver's explicit TLS setting. Its values travel
to Wrangler through stdin. The dashboard must have Workers/Containers billing enabled and
this domain's zone in the selected account. Sign in through the service's GitHub login,
synchronize the selected installation, and inspect webhook delivery before changing merge
rules. Public App visibility can be changed on an existing registration under **General →
Where can this GitHub App be installed? → Any account → Save changes**.

### Concrete state-service installation

From a checkout of the selected Tools revision:

```sh
cd services/github-state
npm ci
npx wrangler login
python3 scripts/setup.py prepare --repository ExampleOrg/ExampleCeti \
  --name exampleceti-state --url https://github-state.example.org \
  --account-id DOWNSTREAM_CLOUDFLARE_ACCOUNT_ID \
  --credentials-dir ~/.config/exampleceti-state
```

Open the printed local HTML form in a browser signed in as ExampleOrg's App manager. Click
**Register dedicated read-only App**, confirm the displayed name, permissions and public
installation setting, then **Create GitHub App**. Complete the one-time manifest code exchange
using the script's printed instructions; the code and response are credentials and stay
private. Install the App with **Only select repositories → ExampleCeti → Install**. Record
the installation number from the resulting `/settings/installations/NUMBER` URL.

Run `inspect`, `deploy` and `bootstrap` with the same repository/name/URL/account/credentials
arguments and `--installation NUMBER`. The helper creates or reuses the named queues and R2
bucket, uploads the private secrets and configures the selected repository. It prints the
expected result of each operation. Save the generated client token in the operator's private
credential directory, then give the worker `--github-state-url https://github-state.example.org`
and `--github-state-token-file /PRIVATE/client-token`. Keep the admin token and App private key
on the service operator's machine. Hosted enrollment uses the existing operator's public App
installation URL and repository-scoped token instead of deploying a second service.

### Roadmap Lean package

The roadmap is a separate Lake package. It can depend on the content package if its target
statements use that project's definitions, or directly on Mathlib for a new empty project.
Use a matching toolchain and commit its manifest. For the active/completed directory names
shown above, its `lakefile.toml` contains:

```toml
name = "ExampleCetiRoadmap"
defaultTargets = ["ExampleCetiRoadmap", "Completed"]

[[require]]
name = "ExampleCeti"
git = "https://github.com/ExampleOrg/ExampleCeti"
rev = "main"

[[lean_lib]]
name = "ExampleCetiRoadmap"
globs = ["ExampleCetiRoadmap.*"]

[[lean_lib]]
name = "Completed"
globs = ["Completed.+"]
```

Use the content package's actual Lake name in `require`. Keep empty root modules
`ExampleCetiRoadmap.lean` and `Completed.lean`, and put human-written target statements in
`ExampleCetiRoadmap/AREA/Suggested.lean`. These statements may use `sorry`; the content
repository's no-axiom/no-sorry audit does not apply to specifications. Each area has a
`README.md` and its arXiv topic in `metadata.toml`. The supplied roadmap CI builds this
package on pull requests, main pushes and merge groups; its job name is `build`. Require
that check and human review in the roadmap's branch rules. The optional progress gate
validates the generated report's exact diff before using its scoped merge App.

### R2 cache setup

A concrete custom cache can use one R2 bucket, `exampleceti-cache`, with `artifacts/` and
`revisions/` prefixes. In the selected Cloudflare account, open R2 object storage, create that
bucket, and attach `cache.example.org` in its Settings under Custom Domains. The domain must
belong to that account's Cloudflare zone. Wait for its status to become active. This supplies
anonymous reads through the domain. [Cloudflare's public-bucket instructions](https://developers.cloudflare.com/r2/buckets/public-buckets/)
describe that operation.

For publication, open R2's Account Details, select Manage beside API Tokens, and create an
account token named `ExampleCeti cache writer`. Grant Object Read & Write, limited to
`exampleceti-cache`. Save the resulting Access Key ID and Secret Access Key privately when
shown; the secret cannot be retrieved later. This bucket-scoped S3 credential is separate
from a general Cloudflare administration token. [R2 authentication instructions](https://developers.cloudflare.com/r2/api/tokens/)
cover the provider controls and endpoint jurisdictions.

Set these public configuration values, replacing ACCOUNT_ID with the selected account:

```toml
[services.cache]
mode = "custom"
name = "exampleceti-public"
domain = "cache.example.org"
artifact_url = "https://cache.example.org/artifacts"
revision_url = "https://cache.example.org/revisions"
artifact_write_url = "https://ACCOUNT_ID.r2.cloudflarestorage.com/exampleceti-cache/artifacts"
revision_write_url = "https://ACCOUNT_ID.r2.cloudflarestorage.com/exampleceti-cache/revisions"
```

For a jurisdictional bucket use its matching S3 hostname from the provider's confirmation.
In the content repository's Actions variables enter:

| Variable | Value from the configuration |
|---|---|
| `LAKE_CACHE_ARTIFACT_ENDPOINT_PUBLIC` | `artifact_url` |
| `LAKE_CACHE_REVISION_ENDPOINT_PUBLIC` | `revision_url` |
| `LAKE_CACHE_ARTIFACT_ENDPOINT` | `artifact_write_url` |
| `LAKE_CACHE_REVISION_ENDPOINT` | `revision_write_url` |

Save `ACCESS_KEY_ID:SECRET_ACCESS_KEY` as the sole line of the private file
`~/.config/tauceti/projects/exampleorg__exampleceti/cache/LAKE_CACHE_KEY`, mode 0600.
Upload it without placing the value on a command line:

```sh
gh secret set LAKE_CACHE_KEY --repo ExampleOrg/ExampleCeti \
  < ~/.config/tauceti/projects/exampleorg__exampleceti/cache/LAKE_CACHE_KEY
```

A post-merge `CI` run stages outputs and its existing `publish-lake-cache` job uploads them.
Inspect that job's upload and public confirmation steps; a skipped optional publisher is not
proof that the key works. Keep publisher credentials out of untrusted build execution. For
hosted cache, request a grant instead of creating a bucket; the operator supplies scoped write
credentials and all four endpoint values privately. Installation of an App alone grants none
of this access.

The review spend and round ledger lives on the `reviews` branch of the content repository,
or the configured Data repository when one is declared. This state persists even with optional
review archiving off; the automation App needs contents write permission on that repository.
