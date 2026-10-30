"""One declarative project configuration, shared by CLI, workers and CI.

Import-time bootstrap is intentional: legacy engines import repository constants.
An entrypoint calls bootstrap before importing those engines, and its subprocesses
inherit the saved configuration path rather than fetching mutable online content.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

CANONICAL = "TauCetiProject/TauCeti"
DOCS = "https://github.com/TauCetiProject/TauCetiTools/blob/main/docs/project-setup.md"
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
REF_RE = re.compile(r"[0-9a-f]{40}\Z")
_active: Project | None = None


class ConfigError(ValueError):
    pass


def _table(raw: dict, key: str) -> dict:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table")
    return value


def _path(value: str, where: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or any(c in value for c in "\n\r\0"):
        raise ConfigError(f"{where} must be a relative repository path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value.startswith("-"):
        raise ConfigError(f"{where} must stay within the repository")
    return value.rstrip("/")


def repository(value: str, where: str) -> str:
    if not isinstance(value, str) or not REPO_RE.fullmatch(value) or value.endswith(".git"):
        raise ConfigError(f"{where} must be owner/repository, without a URL or .git suffix")
    return value


@dataclass(frozen=True)
class Project:
    raw: dict[str, Any]
    source: str = ""

    @property
    def repos(self) -> dict:
        return self.raw["repositories"]

    @property
    def content(self) -> str:
        return self.repos["content"]

    @property
    def key(self) -> str:
        # GitHub names are case-insensitive. This also works before repo creation.
        return self.content.lower().replace("/", "__")

    @property
    def legacy(self) -> bool:
        return self.content.lower() == CANONICAL.lower()

    @property
    def branch(self) -> str:
        return self.raw.get("project", {}).get("branch", "main")

    @property
    def roots(self) -> list[str]:
        return self.raw.get("lean", {}).get("roots", [self.content.split("/")[1]])

    @property
    def roadmap_dir(self) -> str:
        return self.raw.get("roadmap", {}).get("active_dir", self.repos["roadmap"].split("/")[1])

    @property
    def completed_dir(self) -> str:
        return self.raw.get("roadmap", {}).get("completed_dir", "Completed")

    def tool(self, name: str) -> dict:
        return self.raw.get("tools", {}).get(name, {})

    def service(self, name: str) -> dict:
        return self.raw.get("services", {}).get(name, {"mode": "off"})

    def feature(self, name: str) -> bool:
        return bool(self.raw.get("features", {}).get(name, False))

    @property
    def state_root(self) -> Path:
        try:
            import pwd

            home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        except ImportError:
            home = Path.home()
        base = Path(os.environ.get("XDG_STATE_HOME", str(home / ".local/state")))
        return base / "tauceti/projects" / self.key

    def environment(self) -> dict[str, str]:
        """Complete project bindings; absent extras never inherit Tau Ceti destinations."""
        review = self.tool("review")
        progress = self.tool("progress")
        cache = self.service("cache")
        state = self.raw.get("services", {}).get("github_state", {})
        state_default = "https://github-state.taucetiproject.org" if self.content == "TauCetiProject/TauCeti" else ""
        return {
            **{
                f"TAUCETI_{role.upper()}_NAME": value.split("/")[1]
                for role in ("content", "roadmap", "review", "data", "ci", "claims")
                for value in [self.repos.get(role, "/")]
            },
            **{
                f"TAUCETI_{role.upper()}_OWNER": value.split("/")[0]
                for role in ("content", "roadmap", "review", "data", "ci", "claims")
                for value in [self.repos.get(role, "/")]
            },
            "TAUCETI_REPO": self.content,
            "TAUCETI_ROADMAP_REPO": self.repos["roadmap"],
            "TAUCETI_REVIEW_REPO": review.get("repo", "TauCetiProject/TauCetiReview"),
            "TAUCETI_RUBRICS_REPO": self.repos.get("review", review.get("repo", "TauCetiProject/TauCetiReview")),
            "TAUCETI_RUBRICS": json.dumps(self.raw.get("review", {}).get("rubrics", [])),
            "TAUCETI_REVIEW_REF": review.get("ref", ""),
            "TAUCETI_RUBRICS_REF": self.raw.get("review", {}).get("rubrics_ref", review.get("ref", "")),
            "TAUCETI_DATA_REPO": self.repos.get("data", "") if self.feature("archives") else "",
            "TAUCETI_LEDGER_REPO": self.repos.get("data", self.content),
            "TAUCETI_LEDGER_OWNER": self.repos.get("data", self.content).split("/")[0],
            "TAUCETI_LEDGER_NAME": self.repos.get("data", self.content).split("/")[1],
            "TAUCETI_CI_REPO": self.repos.get("ci", "") if self.feature("ci_archive") else "",
            "TAUCETI_CLAIMS_REPO": self.repos.get("claims", "") if self.feature("claims") else "",
            "TAUCETI_BASE_BRANCH": self.branch,
            "TAUCETI_LEAN_ROOT": self.roots[0],
            "TAUCETI_LEAN_ROOTS": json.dumps(self.roots),
            "TAUCETI_ROADMAP_DIR": self.roadmap_dir,
            "TAUCETI_COMPLETED_DIR": self.completed_dir,
            "TAUCETI_PROJECT_KEY": self.key,
            "TAUCETI_PROJECT_STATE": str(self.state_root),
            "TAUCETI_PROGRESS_REPO": progress.get("repo", "TauCetiProject/TauCetiProgress"),
            "TAUCETI_PROGRESS_REF": progress.get("ref", ""),
            "TAUCETI_DOCS_URL": self.raw.get("publication", {}).get("docs_url", ""),
            "TAUCETI_DOCS_REF": self.raw.get("publication", {}).get("docs_ref", "origin/docgen"),
            "TAUCETI_ZULIP_SITE": self.raw.get("publication", {}).get("zulip_site", ""),
            "TAUCETI_ZULIP_CHANNEL": self.raw.get("publication", {}).get("zulip_channel", ""),
            "TAUCETI_ZULIP_TOPIC": self.raw.get("publication", {}).get("zulip_topic", ""),
            "TAUCETI_GITHUB_STATE_URL": state.get("url", state_default) if state.get("mode") != "off" else "",
            "TAUCETI_CACHE_DOMAIN": cache.get("domain", "") if cache.get("mode") != "off" else "",
            "TAUCETI_CACHE_SERVICE": cache.get("name", self.key) if cache.get("mode") != "off" else "",
            "TAUCETI_CACHE_ARTIFACT_URL": cache.get("artifact_url", "") if cache.get("mode") != "off" else "",
            "TAUCETI_CACHE_REVISION_URL": cache.get("revision_url", "") if cache.get("mode") != "off" else "",
            "TAUCETI_BORS_URL": self.service("bors").get("url", "")
            if self.service("bors").get("mode") != "off"
            else "",
            "TAUCETI_BORS_REPOSITORY_ID": str(self.service("bors").get("repository_id", "")),
            "TAUCETI_UPSTREAMS": json.dumps(self.raw.get("upstreams", [])),
        }


def parse(data: bytes, source: str = "") -> Project:
    from .onboarding import configuration_problem

    diagnostic_source = (os.environ.get("TAUCETI_CONF_SOURCE", source)
                         if source == os.environ.get("TAUCETI_CONF_SNAPSHOT") else source)
    try:
        raw = tomllib.loads(data.decode("utf-8"))
    except (ValueError, UnicodeError) as error:
        raise ConfigError(configuration_problem(diagnostic_source, f"Invalid TOML: {error}")) from None
    try:
        if not raw:
            raise ConfigError("The file contains no project settings.")
        return _validate(raw, source)
    except ConfigError as error:
        raise ConfigError(configuration_problem(diagnostic_source, str(error), fresh=not data.strip())) from None


def _validate(raw: dict, source: str) -> Project:
    allowed = {
        "schema_version",
        "project",
        "repositories",
        "lean",
        "roadmap",
        "tools",
        "review",
        "merge",
        "features",
        "services",
        "upstreams",
        "publication",
        "apps",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"Unknown configuration sections: {', '.join(sorted(unknown))}")
    if raw.get("schema_version") != 1 or isinstance(raw.get("schema_version"), bool):
        raise ConfigError("Set schema_version = 1 at the top of the file, before any [table] header.")
    repos = _table(raw, "repositories")
    for key in ("content", "roadmap"):
        if key not in repos:
            raise ConfigError(f"repositories.{key} is required")
    for key, value in repos.items():
        repository(value, f"repositories.{key}")
    for key in allowed - {"schema_version", "upstreams", "repositories"}:
        _table(raw, key)
    branch = _table(raw, "project").get("branch", "main")
    if not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) or ".." in branch:
        raise ConfigError("project.branch must be a Git branch name")
    roots = _table(raw, "lean").get("roots", [repos["content"].split("/")[1]])
    if (
        not isinstance(roots, list)
        or not roots
        or any(
            not isinstance(x, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*", x)
            for x in roots
        )
    ):
        raise ConfigError("lean.roots must be a nonempty array of Lean module names")
    for key, value in _table(raw, "roadmap").items():
        if key.endswith("_dir"):
            _path(value, f"roadmap.{key}")
    authoring_conf = _table(raw, "roadmap").get("authoring_conf_url")
    if authoring_conf is not None:
        if not isinstance(authoring_conf, str):
            raise ConfigError("roadmap.authoring_conf_url must be an HTTPS URL without credentials")
        try:
            url = urllib.parse.urlsplit(authoring_conf)
            valid = (url.scheme == "https" and url.hostname and not url.username and not url.password
                     and not any(c.isspace() for c in authoring_conf))
        except ValueError:
            valid = False
        if not valid:
            raise ConfigError("roadmap.authoring_conf_url must be an HTTPS URL without credentials")
    for name, value in _table(raw, "tools").items():
        if not isinstance(value, dict):
            raise ConfigError(f"tools.{name} must have repo and ref fields")
        repository(value.get("repo", ""), f"tools.{name}.repo")
        if not isinstance(value.get("ref"), str) or not REF_RE.fullmatch(value["ref"]):
            raise ConfigError(f"tools.{name}.ref must be a full immutable 40-character commit SHA. Run gh api repos/{value['repo']}/commits/HEAD --jq .sha and put the returned SHA in ref under [tools.{name}].")
    review = _table(raw, "review")
    if "rubrics" in review and (
        not isinstance(review["rubrics"], list)
        or not review["rubrics"]
        or any(
            not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
            for name in review["rubrics"]
        )
    ):
        raise ConfigError("review.rubrics must be a nonempty list of rubric names")
    if "rubrics_ref" in review and not REF_RE.fullmatch(str(review["rubrics_ref"])):
        raise ConfigError("review.rubrics_ref must be an immutable full commit SHA")
    for name, value in _table(raw, "features").items():
        if not isinstance(value, bool):
            raise ConfigError(f"features.{name} must be true or false")
    if raw.get("features", {}).get("progress") and not raw.get("tools", {}).get("progress"):
        raise ConfigError("features.progress = true requires [tools.progress] repo and a full immutable ref")
    for name, value in _table(raw, "services").items():
        if not isinstance(value, dict) or value.get("mode") not in ("off", "hosted", "custom"):
            raise ConfigError(f"services.{name}.mode must be off, hosted or custom")
        for key in ("url", "artifact_url", "revision_url", "artifact_write_url", "revision_write_url"):
            if value.get(key):
                url = urllib.parse.urlsplit(value[key])
                if url.scheme != "https" or not url.hostname or url.username or url.password:
                    raise ConfigError(f"services.{name}.{key} must be an HTTPS URL without credentials")
        if name == "bors" and value.get("deployment", "docker") not in ("docker", "cloudflare"):
            raise ConfigError("services.bors.deployment must be docker or cloudflare")
    for upstream in raw.get("upstreams", []):
        if not isinstance(upstream, dict):
            raise ConfigError("upstreams must be an array of tables")
        repository(upstream.get("repo", ""), "upstreams.repo")
        if not isinstance(upstream.get("package"), str) or not re.fullmatch(r"[A-Za-z0-9_-]+", upstream["package"]):
            raise ConfigError("each upstream requires a Lake package name")
    backend = _table(raw, "merge").get("backend", "github-queue")
    if backend == "github-direct":
        raise ConfigError(
            'github-direct is no longer a supported merge setup. Choose merge.backend = "github-queue" '
            "and enable GitHub's merge queue with MERGE_BACKEND=queue, or configure Bors and choose "
            'merge.backend = "bors" with MERGE_BACKEND=bors. Review the branch rules before switching; '
            'doctor does not change your configuration or GitHub settings.'
        )
    if backend not in ("github-queue", "bors"):
        raise ConfigError("merge.backend must be github-queue or bors")
    if backend == "bors" and _table(raw, "services").get("bors", {}).get("mode", "off") == "off":
        raise ConfigError("Bors must be configured before selecting merge.backend = bors")
    return Project(raw, source)


def read_source(source: str) -> bytes:
    url = urllib.parse.urlsplit(source)
    if url.scheme:
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ConfigError("Online configuration must use HTTPS without embedded credentials")
        if url.hostname == "github.com" and "/blob/" in url.path:
            source = "https://raw.githubusercontent.com" + url.path.replace("/blob/", "/", 1)
        from .system import ensure_ssl_cert_file

        ensure_ssl_cert_file()
        try:
            with urllib.request.urlopen(source, timeout=30) as response:
                if urllib.parse.urlsplit(response.url).scheme != "https":
                    raise ConfigError("Configuration redirected outside HTTPS")
                data = response.read(1024 * 1024 + 1)
        except OSError as error:
            raise ConfigError(
                f"Cannot fetch {source}: {error}. Check the URL and retry; no fallback project was selected."
            ) from error
        if len(data) > 1024 * 1024:
            raise ConfigError("Configuration exceeds 1 MiB")
        return data
    try:
        return Path(source).expanduser().read_bytes()
    except FileNotFoundError:
        from .onboarding import configuration_problem

        raise ConfigError(configuration_problem(source, "The file does not exist.", fresh=True)) from None
    except OSError as error:
        raise ConfigError(f"Cannot read {source}: {error}") from error


def load(source: str) -> Project:
    return parse(read_source(source), source)


def save_snapshot(source: str) -> Path:
    data = read_source(source)
    project = parse(data, source)
    directory = project.state_root / "config"
    directory.mkdir(parents=True, exist_ok=True)
    # Existing workers keep their own file; refresh never overwrites it mid-round.
    fd, name = tempfile.mkstemp(prefix="project-", suffix=".toml", dir=directory)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
    return Path(name)


def conf_argument(argv: list[str]) -> tuple[str | None, list[str]]:
    remaining: list[str] = []
    selected = None
    i = 0
    while i < len(argv):
        item = argv[i]
        if item == "--conf":
            i += 1
            if i == len(argv):
                raise ConfigError("--conf requires a local path or HTTPS URL")
            if selected is not None:
                raise ConfigError("Specify --conf only once")
            selected = argv[i]
        elif item.startswith("--conf="):
            if selected is not None:
                raise ConfigError("Specify --conf only once")
            selected = item.split("=", 1)[1]
        else:
            remaining.append(item)
        i += 1
    return selected, remaining


def checkout_root() -> Path:
    """Use this Git checkout's root, or the current directory outside Git."""
    try:
        git = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    except OSError:
        return Path.cwd()
    return Path(git.stdout.rstrip("\n")) if git.returncode == 0 else Path.cwd()


def configuration_source(explicit: str | None = None) -> str | None:
    """Explicit settings win; otherwise use the current directory or Git root."""
    selected = explicit or os.environ.get("TAUCETI_CONF")
    if selected:
        return selected
    local = Path.cwd() / "tauceti.toml"
    if local.is_file():
        return str(local)
    root = checkout_root() / "tauceti.toml"
    return str(root) if root.is_file() else None


def bootstrap(argv: list[str] | None = None) -> Project | None:
    global _active
    args = sys.argv[1:] if argv is None else argv
    explicit, _ = conf_argument(args)
    source = configuration_source(explicit)
    if not source:
        return _active
    inherited = os.environ.get("TAUCETI_CONF_SNAPSHOT")
    if inherited and os.environ.get("TAUCETI_CONF_SOURCE") == source:
        path = Path(inherited)
    else:
        path = save_snapshot(source)
    _active = load(str(path))
    os.environ.update(_active.environment())
    os.environ["TAUCETI_CONF"] = source
    os.environ["TAUCETI_CONF_SOURCE"] = source
    os.environ["TAUCETI_CONF_SNAPSHOT"] = str(path)
    return _active


def active() -> Project | None:
    return _active


def setting(section: str, key: str, default: Any = None) -> Any:
    project = active()
    return project.raw.get(section, {}).get(key, default) if project else default
