"""Credential sources and GitHub form instructions, shared by their consumers."""

import shlex
from pathlib import Path

from .deployment import app_manifest


def app_setup(project, role, *, organization=False):
    manifest = app_manifest(project, role)
    owner = project.content.split("/")[0]
    settings = (f"https://github.com/organizations/{owner}/settings/apps" if organization
                else "https://github.com/settings/apps")
    permissions = ", ".join(
        {"statuses": "Commit statuses", "actions_variables": "Variables"}.get(name, name.replace("_", " ").title())
        + ": " + ("Read and write" if access == "write" else "Read-only")
        for name, access in manifest["default_permissions"].items()
    )
    repos = [project.content]
    if role == "claims":
        repos = [project.repos.get("claims", project.content + "-claims")]
    elif role == "automation":
        repos += [project.repos[key] for key, enabled in (("roadmap", project.feature("progress")),
                  ("data", project.feature("archives"))) if enabled and key in project.repos]
    prefix = {"automation": "APP", "claims": "CLAIMS_APP", "collector": "COLLECTOR_APP"}[role]
    owners = ", ".join(sorted({repo.split('/')[0] for repo in repos}))
    directory = Path.home() / ".config/tauceti/projects" / project.key / role
    id_file, key_file = directory / (prefix + "_ID"), directory / (prefix + "_PRIVATE_KEY")
    steps = [
        f"Open {settings}. Reuse {manifest['name']} if it already exists; a missing repository secret does not require a second App. Otherwise click New GitHub App.",
        f"For a new App enter GitHub App name: {manifest['name']} (choose an available name if taken); Homepage URL: {manifest['url']}; Webhook → Active: unchecked; Where can this GitHub App be installed?: Any account. Repository permissions: {permissions}. Click Create GitHub App.",
        f"On that App's General page copy the numeric App ID (not Client ID). Under Install App choose each required account ({owners}) → Only select repositories. Select that account's repositories from {', '.join(repos)}, then Install. For an existing installation use Configure → Repository access, add these repositories, and Save.",
        f"Prepare the private credential directory:\n$ mkdir -p {shlex.quote(str(directory))}\n$ chmod 700 {shlex.quote(str(directory))}\nSave the numeric App ID as the only line of {id_file}. Reuse your existing PEM at {key_file}, or on the same App's General page click Generate a private key and move the downloaded PEM there. Then:\n$ chmod 600 {shlex.quote(str(id_file))} {shlex.quote(str(key_file))}",
    ]
    if role == "claims" and "claims" not in project.repos:
        steps.insert(0, f"Set repositories.claims = \"{repos[0]}\" in {project.source}. Confirm whether it exists with gh repo view {repos[0]}; if absent create it with gh repo create {repos[0]} --public --add-readme. Install this App only on that claims repository.")
    return [settings], steps


def credential_setup(project, name, repository, path, *, organization=False):
    if "APP_" in name:
        urls, steps = app_setup(project, "claims" if name.startswith("CLAIMS_") else "automation",
                                organization=organization)
    elif name == "LAKE_CACHE_KEY":
        urls = ["https://dash.cloudflare.com/"]
        steps = [
            "If cache mode is hosted, request the project's scoped writer from its operator using the Build cache instructions below. Do not create a GitHub App for this credential.",
            "For your own R2 cache, open Cloudflare → R2 object storage → Manage API Tokens → Create Account API token. Name it for this project's cache writer; choose Object Read & Write → Apply to specific buckets only → your project's cache bucket → Create. Save the Access Key ID and Secret Access Key from the confirmation page.",
            f"Save ACCESS_KEY_ID:SECRET_ACCESS_KEY as the only line of {path}, replacing both parts with those values. The endpoint configuration and full operator procedure are in the Build cache task.",
        ]
    elif name.startswith("ZULIP_"):
        publication = project.raw.get("publication", {})
        site = publication.get("zulip_site", "")
        urls = [site + "/#settings/your-bots"] if site else ["https://zulip.com/help/add-a-bot-or-integration"]
        steps = [
            f"In {project.source}, set publication.zulip_site to the HTTPS URL of your Zulip organization, zulip_channel to the destination channel's name, and zulip_topic to the intended topic. Use your own destination; these values do not come from Tau Ceti.",
            "In that Zulip organization open the gear menu → Personal settings → Bots → Add a new bot. Choose Generic bot, give it a project-specific name and username, then Add. Reuse the project's existing bot if it has one.",
            "In Bots use the bot's download icon to save its zuliprc file. It contains email= and key= values. Add the bot to the destination channel, including private channels, through the channel's subscriber controls.",
            f"Save the {'email=' if name == 'ZULIP_EMAIL' else 'key='} value from that zuliprc file, without the field name, as the only line of {path}.",
        ]
    else:  # The optional fleet uses the downstream's own picker and credentials.
        urls = [f"https://github.com/{repository}/blob/{project.branch}/.github/workflows/pick-runner.yml"]
        steps = [
            "The supplied picker uses GitHub-hosted runners and does not consume FLEET_READ_TOKEN. If you do not operate a runner fleet, set features.fleet = false and rerun doctor.",
            f"If you operate a fleet, install your own pick-runner.yml and obtain the read-only credential required by that picker's FLEET_READ_TOKEN consumer. Save it in {path}. No Tau Ceti fleet credential or fleet access is included.",
        ]
    if "APP_" not in name:
        steps.insert(0, f"Prepare the private credential directory before saving values:\n$ mkdir -p {shlex.quote(str(path.parent))}\n$ chmod 700 {shlex.quote(str(path.parent))}")
        steps += [f"Restrict the saved credential file:\n$ chmod 600 {shlex.quote(str(path))}"]
    urls.append(f"https://github.com/{repository}/settings/secrets/actions")
    steps += [
        f"Upload the private file using the gh secret set command below. Alternatively open Settings → Secrets and variables → Actions → Secrets → New repository secret. Name: {name}; Secret: the contents of {path}; click Add secret.",
        "For organization secrets, the repository must be included in Repository access. An environment secret with the same name overrides a repository secret; inspect that workflow's selected environment before replacing credentials.",
    ]
    return urls, steps
