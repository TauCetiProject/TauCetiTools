"""Project context for native content with explicitly configured shared GitHub tools."""

from __future__ import annotations

import copy
import os

from .forge import GITHUB
from .project import QUALIFIED_CONTEXT_VARIABLES, ConfigError, Project, bootstrap_qualified


def shared_settings(project):
    """Legacy shared-tool settings; the original Project retains content identity."""
    if project is None:
        return None
    references = [project.repository_ref(role) for role in project.raw["repositories"] if role != "content"]
    references.extend(project.tool_pin(name)[0] for name in project.raw.get("tools", {}))
    if any(reference.forge != GITHUB for reference in references):
        raise ConfigError("Shared roadmap, tool and archive repositories must currently use GitHub bindings")
    if project.repository_ref("content").forge == GITHUB:
        return project
    raw = copy.deepcopy(project.raw)
    raw["repositories"]["content"] = project.repository_ref("content").name
    return Project(raw, project.source)


def environment(project):
    settings = shared_settings(project)
    reference = project.repository_ref("content")
    if reference.forge == GITHUB:
        return settings.environment()
    return {
        **settings.environment(),
        **dict.fromkeys(QUALIFIED_CONTEXT_VARIABLES, ""),
        "TAUCETI_REPO": reference.name if reference.forge == GITHUB else reference.key,
        "TAUCETI_PROJECT_STATE": str(project.state_root),
        "TAUCETI_PROJECT_KEY": project.key,
    }


def bootstrap(argv=None):
    """Reuse the trusted snapshot and select shared tools without rehoming content."""
    project = bootstrap_qualified(argv)
    if project is not None:
        os.environ.update(environment(project))
    return project
