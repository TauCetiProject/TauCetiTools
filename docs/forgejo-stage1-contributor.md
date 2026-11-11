# Stage 1 contributor commands

Stage 1 keeps familiar `gh` commands through `tauceti-gh`, with native GitHub CLI
delegation for CI. The migration operator supplies the approved immutable Tools
revision, its installed environment, trusted project configuration, source and
execution repository identities, and the supported Forgejo origin. Supporting
tools and CI may remain on GitHub.

Use your own Forgejo account/token for source operations. Store the token in a
private file (`chmod 600`); configure Git authentication separately. A GitHub
token does not authenticate to Forgejo. The configuration and token path come
from trusted operator settings outside the checkout, not from a contributor PR.

## Opt in without changing the global CLI

The example assumes `tools_environment` is the absolute installed Tools environment
path and `trusted_configuration` is the approved configuration file. Set
`source_origin` to its exact Forgejo origin, `source_token_file` to your private
token file, and `source_user_id` to your observed native numeric account ID.
`source_repository` must be `HOST/OWNER/REPO` on Forgejo (include `:port` in HOST
when required);
`execution_repository` must be `github.com/OWNER/REPO` for the configured CI repo.
Set `pr_number` to an existing native PR number and `run_id` to a GitHub run ID
for the read examples below.

Run this in a subshell. Capture the native executable **before** adding the shim
to `PATH`, and supply `TAUCETI_REAL_GH` so delegated GitHub commands cannot recurse.
Keep the existing native GitHub authentication; source credentials stay separate.

```sh
(
  set -eu
  real_gh=$(command -v gh) || { echo 'Native gh is missing' >&2; exit 1; }
  case "$real_gh" in /*) ;; *) echo 'Use an absolute native gh executable' >&2; exit 1 ;; esac
  test -x "$tools_environment/bin/tauceti-gh" || { echo 'Installed tauceti-gh is missing' >&2; exit 1; }
  private_bin=$(mktemp -d)
  trap 'rm -f "$private_bin/gh"; rmdir "$private_bin"' EXIT
  ln -s "$tools_environment/bin/tauceti-gh" "$private_bin/gh"
  export TAUCETI_REAL_GH="$real_gh"
  export TAUCETI_CONF="$trusted_configuration"
  export TAUCETI_FORGEJO_ORIGIN="$source_origin"
  export TAUCETI_FORGEJO_TOKEN_FILE="$source_token_file"
  export TAUCETI_FORGEJO_USER_ID="$source_user_id"
  export PATH="$private_bin:$PATH"
  unset GH_HOST GH_REPO

  gh pr list -R "$source_repository" --json number,title,headRefOid,url
  gh pr view "$pr_number" -R "$source_repository" --json number,headRefOid,url
  gh run view "$run_id" -R "$execution_repository" \
    --json databaseId,status,conclusion,headSha,url
)
```

The global `gh`, native login, and shell configuration are unchanged after the
subshell exits. For private certificate authorities, supply `SSL_CERT_FILE` in
the trusted environment; the CLI preserves it. When `SSL_CERT_FILE` is unset,
the native Forgejo path selects an available system CA bundle, preferring
`NIX_SSL_CERT_FILE`, and keeps TLS verification enabled.

## Publish and inspect a contribution

Push your topic branch to your Forgejo fork using Git. Set `contributor` to your
native account name, `topic_branch` to the pushed branch, `title` to the PR title,
and `body_file` to its description file. The complete block below creates its own
opt-in environment and opens a PR with an explicit fork head. Its output includes
the native PR URL; use that number in the read example above.

```sh
(
  set -eu
  real_gh=$(command -v gh) || { echo 'Native gh is missing' >&2; exit 1; }
  case "$real_gh" in /*) ;; *) echo 'Use an absolute native gh executable' >&2; exit 1 ;; esac
  test -x "$tools_environment/bin/tauceti-gh" || { echo 'Installed tauceti-gh is missing' >&2; exit 1; }
  private_bin=$(mktemp -d)
  trap 'rm -f "$private_bin/gh"; rmdir "$private_bin"' EXIT
  ln -s "$tools_environment/bin/tauceti-gh" "$private_bin/gh"
  export TAUCETI_REAL_GH="$real_gh"
  export TAUCETI_CONF="$trusted_configuration"
  export TAUCETI_FORGEJO_ORIGIN="$source_origin"
  export TAUCETI_FORGEJO_TOKEN_FILE="$source_token_file"
  export TAUCETI_FORGEJO_USER_ID="$source_user_id"
  export PATH="$private_bin:$PATH"
  unset GH_HOST GH_REPO

  gh pr create -R "$source_repository" --head "$contributor:$topic_branch" --base main \
    --title "$title" --body-file "$body_file"
)
```

For an ambiguous write, inspect the native repository before retrying; a lost
response does not establish that creation failed. Unsupported commands and flags
fail explicitly. In particular, `gh pr merge` is not the Stage 1 merge path:
human acceptance, candidate dispatch, verified results, and exact-candidate
promotion use the Worker Stage 1 operator commands.

The routed read commands include `gh pr diff` (also `--name-only`),
`gh pr view --comments`, and `gh pr checks` (including `--json` and `--jq`).
Diff reads reject a comparison that moves during the read; filename listing
uses the exact Git comparison. Comments include both conversation and review
bodies. Checks report observed native statuses and keep missing verified
execution evidence pending: exit 8 means pending, and exit 1 means a failed
check. A native status named `build` does not establish execution authority.
Additional diff formats, checks watching/filtering, and options outside the
parsed command contract remain explicitly unsupported.

GitHub run `headSha` identifies the execution workflow context, not necessarily
the Forgejo candidate. The operator verifies candidate, request, run, and attempt
identities through the retained CI contract. A green run viewed with `gh` alone
does not authorize merge. Never route a native Forgejo PR number to GitHub.

See [the preparation reference](forgejo-preparation.md) for the supported command
surface and credential/routing rules. Comprehensive unmodified upstream `gh`
compatibility and autonomous agent integration remain separate work.
