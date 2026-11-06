# Disposable project

`content/` and `roadmap/` are two separate starter repository trees, sharing a real pinned
Mathlib manifest and toolchain. They contain no credentials or enabled optional integrations.
The configuration selects immutable commits from the published Tools, Review and Progress
repositories.

Copy these trees out of Tools. In `content/tauceti.toml`, replace `ExampleOrg`, `ExampleCeti`
and `ExampleMaintainer` with your actual repository/root/owner names. Rename the Lean roots,
Lake package names and roadmap directories to match, or keep ExampleCeti for a throwaway run.
Create the two repositories using the commands in the [manual](../../docs/project-setup.md#repositories),
commit each tree on its repository's default branch and publish it. The placeholder roadmap
does not authorize agents to invent a mathematical programme.

From the content checkout, install the Tools commit selected in the configuration and run:

```sh
tauceti-tools doctor --conf ./tauceti.toml
tauceti-tools doctor --conf ./tauceti.toml --fix workflows
tauceti-tools doctor --conf ./tauceti.toml --fix workflows --apply
tauceti-tools doctor --conf ./tauceti.toml --fix roadmap.workflows --apply
```

Preview/inspect the generated workflow PRs, then merge them normally. Register the project-owned
App, install it only on the selected repositories, upload the named credentials, and configure
branch rules using the printed commands and UI instructions. An ordinary content PR supplies
the first build result. Doctor checks App-token use separately when a normal workflow uses it;
you do not need an extra PR merely to verify credentials. Nothing in this example enrolls the project in hosted
infrastructure or modifies Epsilon Eridani. The tracked `Completed/.gitkeep` keeps that empty
directory in a fresh clone.

The starter selects GitHub's merge queue: set `MERGE_BACKEND=queue`, require the build,
bump-guard and scope checks, and allow repository administrators to perform admin merges
while keeping automation Apps out of the bypass list. To test Bors instead, configure its
service and repository ID, set `merge.backend="bors"` and `MERGE_BACKEND=bors`, disable the
GitHub queue and give the Bors App the authority described in the manual. Restricted paths
need an explicit admin merge with the queue, or an authorized `bors merge` with Bors.

During setup, App-dependent jobs skip with a message while credentials are missing; normal
builds still run. Supply credentials before treating those skipped jobs as configured automation.

For a local configuration/isolation check, pass this file to the Worker `--help` or `doctor`
commands. `doctor` reports inaccessible example repositories as unknown and explains how to
create or grant access to the actual destinations; it never treats a 404 as proof of absence.
