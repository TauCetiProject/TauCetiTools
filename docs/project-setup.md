# Tau Ceti project architecture and manual setup

This document describes a project using the shared Tau Ceti workflow. Commands below use
`ExampleOrg/ExampleCeti`. Substitute the organization, project name and human owner once
when preparing an installation. Each project has its own repositories, configuration,
credentials and merge authority. An organization can contain several projects.

## Architecture

The content repository holds Lean source and reproducible dependency pins. Human-owned
configuration defines the source paths agents may edit, the dependencies, review policy and
required checks. The roadmap repository supplies the mathematical targets; it is maintained
by humans, even where a human uses an assistant to draft text.

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
```

The [complete example](../examples/epsilon-eridani.toml) shows the full configuration.
Its main fields are `repositories.content`, `repositories.roadmap`, `project.branch`,
`lean.roots` and the pinned `[tools.tools]`, `[tools.review]`, `[tools.progress]` sections.
Use actual repository names and immutable full commit SHAs. Do not place passwords,
tokens, PEM keys or database URLs in the public configuration.

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
Commit both `lean-toolchain` and `lake-manifest.json`. Set the Lean library root to the
configuration's `lean.roots`. The supplied build harness currently renders one top-level
Lean library root. Projects with several roots keep their existing custom build workflow. A project following Tau Ceti's empty-root policy uses a glob
that builds every source module; its root module does not need individual imports.

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
and TauCeti; Mathlib can be a transitive dependency. Those packages must agree on their
Lean toolchain and shared dependency pins. Resolve incompatible upstream versions before
opening an automated dependency update. Never independently update transitive Mathlib
to a version rejected by another direct dependency.

Keep forward-only dependency changes separate from source authoring. Protected lakefile
changes remain human-owned. Configure the build policy rather than removing guards to
make an update pass.

## Roadmaps

In the roadmap repository create an active directory, for example `ExampleCetiRoadmap/`,
with one subdirectory per area. Each area has `README.md`; `Suggested.lean` can give
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

Commit the project configuration, CODEOWNERS and workflow changes on a branch and open a
PR against the upstream default branch. Do not merge setup through an admin bypass.

The trusted build workflow selects immutable PR and base commits, establishes source scope,
attests Lake configuration, prepares integration and runs candidate Lean offline under the
existing sandbox. Shared drivers come from the workflow-pinned Tools version. Candidate
scripts, workflow edits and cached audit binaries cannot replace those trusted drivers.

The PR-status workflow maintains the labels workers use to select work. It is core
automation even when Zulip is off; the same reconciler skips notifications in that mode.

Project lint baselines and exceptions remain in project `scripts/`. The setup action overlays
shared implementations but preserves the selected project's policy data.

## Actions

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

Use Actions variables for non-secret values, for example `MERGE_BACKEND` (`queue`, `direct` or `bors`),
`LAKE_CACHE_ARTIFACT_ENDPOINT_PUBLIC`, `LAKE_CACHE_REVISION_ENDPOINT_PUBLIC` and deliberately
enabled fleet/runner settings. Set `CI_REVIEW_ENABLED` only if paid CI review is intended;
operator-run subscription review does not require provider API keys in the repository.

## Branch rules

Open `https://github.com/ExampleOrg/ExampleCeti/settings/rules/new?target=branch`:

1. Name: **Tau Ceti content**. Enforcement status: **Active**. Target the configured default
   branch.
2. Require a pull request and code-owner review for human-owned changes.
3. Require checks **build**, **bump-guard**, **scope**, with the expected check producer.
4. With `merge.backend = "github-queue"`, enable **Require merge queue**. The installed workflow must run
   on `merge_group` and validate the actual combined commit.
5. Configure any automation bypass only for pull requests and only for the intended App.
   Trusted merge policy must still reject unauthorized human-owned changes. Do not give
   content authors unconditional bypass or use admin merge to get past missing reviews.

For `merge.backend = "github-direct"`, set the Actions variable `MERGE_BACKEND` to `direct`.
Require the same build, scope and bump-guard checks, and leave **Require branches to be up to
date before merging** unchecked (`required_status_checks.strict = false`). Do not require a
merge queue. Trusted automation applies the same current-head scoreboard and path policy,
then squash-merges through GitHub's API with the exact reviewed head SHA. Advancing the base
branch does not invalidate a review of the same diff or require a branch update. GitHub still
rejects merge conflicts. Keep the direct-merge App out of the ruleset bypass list so
GitHub also enforces the required checks. The hourly sweep retries
eligible PRs after missed events. This mode works for public personal-account repositories.

GitHub merge queue availability depends on repository visibility and account type. The standard
starter uses a public organization-owned repository. If a queue is unavailable, explicitly select direct GitHub merging or configure Bors.

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
downstream's provider. `off` keeps the GitHub queue. The database option does not select where
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

Keep GitHub queue active while registering and inspecting Bors. Confirm App delivery,
repository synchronization and a successful pilot staging build using the required
`build`, `bump-guard` and `scope` checks. Then explicitly select the Bors merge backend in
configuration and the corresponding repository variable/rules. A responding health endpoint
alone is insufficient reason to change merge authority.

## GitHub state

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

Bors observations are optional. Configure an approved receipt endpoint for the correct project
and branch, or a Cloudflare service binding. Absence of Bors does not impair GitHub mirroring.

## Verification

Use real workflow runs as evidence:

```sh
gh workflow list --repo ExampleOrg/ExampleCeti
gh run list --repo ExampleOrg/ExampleCeti --limit 10
gh run view RUN_ID --repo ExampleOrg/ExampleCeti --log
gh api repos/ExampleOrg/ExampleCeti/actions/runs/RUN_ID/jobs
```

Substitute an actual run ID from the listing. Inspect the credential-consuming step, not just
the overall green conclusion: a skipped step or authentication fallback leaves that credential
unverified. Trigger a content PR for pr-build; review and build changes trigger merge automation.
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
`~/.config/tauceti/neon-api-key`. The following command previews the resource settings; it
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
visible in its `/repositories/NUMBER` UI; store it as `services.bors.repository_id` only if
collecting those optional receipts.

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
