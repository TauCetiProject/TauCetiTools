"""The review context keeps qualified content identity and existing shared-tool bindings."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tauceti_tools import project, review_project
from tauceti_tools.review_source import source_for
from tauceti_tools.templates import render


def configuration(kind):
    content = '"Owner/Project"' if kind == "github" else '{forge="native",name="Owner/Project"}'
    return (
        'schema_version=2\n[forges.native]\nkind="forgejo"\nurl="https://git.example.org"\n'
        f'[repositories]\ncontent={content}\nroadmap="Owner/Roadmap"\n'
        'review="Owner/Rubrics"\nclaims="Owner/Claims"\ndata="Owner/Data"\n'
        '[lean]\nroots=["Project"]\n[tools.review]\nrepo="Owner/Review"\nref="' + "a" * 40 + '"\n'
    )


class ReviewProject(unittest.TestCase):
    def test_snapshot_bootstrap_and_rendering_for_both_sources(self):
        for kind in ("github", "forgejo"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                snapshot = Path(directory) / "project.toml"
                snapshot.write_text(configuration(kind))
                source = "https://invalid.test/trusted-project.toml"
                with (
                    patch.dict(
                        os.environ,
                        {"TAUCETI_CONF": source, "TAUCETI_CONF_SOURCE": source, "TAUCETI_CONF_SNAPSHOT": str(snapshot)},
                        clear=True,
                    ),
                    patch.object(project, "_active", None),
                    patch.object(project, "save_snapshot", side_effect=AssertionError("refetched trusted snapshot")),
                ):
                    configured = review_project.bootstrap([])
                    ref = configured.repository_ref("content")
                    self.assertEqual(os.environ["TAUCETI_REPO"], ref.name if kind == "github" else ref.key)
                    self.assertEqual(os.environ["TAUCETI_REVIEW_REF"], "a" * 40)
                    self.assertEqual(os.environ["TAUCETI_ROADMAP_REPO"], "Owner/Roadmap")
                    self.assertEqual(os.environ["TAUCETI_RUBRICS_REPO"], "Owner/Rubrics")
                    self.assertIs(project.active(), configured)
                    rendered = render(
                        "https://github.com/TauCetiProject/TauCeti/pull/7\n"
                        "TauCetiProject/TauCetiRoadmap TauCeti/ https://github.com/TauCetiProject/TauCetiReview",
                        configured,
                        qualified=True,
                    )
                    self.assertIn(ref.url + "/pull/7", rendered)
                    self.assertIn("Owner/Roadmap Project/", rendered)
                    self.assertIn("https://github.com/Owner/Review", rendered)
                    if kind == "forgejo":
                        self.assertNotIn("https://github.com/Owner/Project", rendered)
                        with self.assertRaises(project.ConfigError):
                            configured.environment()

    def test_native_config_cannot_silently_select_github_or_another_binding(self):
        configured = project.parse(configuration("forgejo").encode())
        with patch.object(project, "_active", configured), patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "REPOSITORY_ID"):
                source_for("Owner/Project")
            with self.assertRaisesRegex(ValueError, "configured content"):
                source_for("Other/Project")
            with tempfile.TemporaryDirectory() as directory:
                token = Path(directory) / "token"
                token.write_text("secret")
                token.chmod(0o600)
                api = Mock()
                api.repository.return_value = dict(id=7, full_name="Owner/Project")
                api.current_user.return_value = dict(id=3)
                with (
                    patch.dict(os.environ, TAUCETI_FORGEJO_REPOSITORY_ID="7", TAUCETI_FORGEJO_TOKEN_FILE=str(token)),
                    patch("tauceti_tools.review_source.Forgejo", return_value=api),
                ):
                    self.assertEqual(source_for("Owner/Project").repository, configured.repository_ref("content"))
                    with patch.dict(os.environ, TAUCETI_FORGEJO_ORIGIN="https://other.example.org"):
                        with self.assertRaisesRegex(ValueError, "credential origin"):
                            source_for("Owner/Project")
                    wrong = dict(
                        forge=dict(kind="forgejo", url="https://other.example.org"), name="Owner/Project", id=7
                    )
                    with patch.dict(os.environ, TAUCETI_SOURCE_BINDING=json.dumps(wrong)):
                        with self.assertRaisesRegex(ValueError, "configured content"):
                            source_for("Owner/Project")

    def test_unsupported_shared_tool_host_is_explicit(self):
        raw = project.parse(configuration("forgejo").encode()).raw
        raw["repositories"]["roadmap"] = {"forge": "native", "name": "Owner/Roadmap"}
        with self.assertRaisesRegex(project.ConfigError, "Shared roadmap"):
            review_project.environment(project.Project(raw, ""))

    def test_legacy_github_configuration_can_still_opt_into_a_native_source(self):
        configured = project.parse(configuration("github").encode())
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("secret")
            token.chmod(0o600)
            binding = dict(forge=dict(kind="forgejo", url="https://git.example.org"), name="Owner/Project", id=7)
            api = Mock()
            api.repository.return_value = dict(id=7, full_name="Owner/Project")
            api.current_user.return_value = dict(id=3)
            with (
                patch.object(project, "_active", configured),
                patch.dict(
                    os.environ,
                    {
                        "TAUCETI_SOURCE_BINDING": json.dumps(binding),
                        "TAUCETI_FORGEJO_ORIGIN": "https://git.example.org",
                        "TAUCETI_FORGEJO_TOKEN_FILE": str(token),
                    },
                    clear=True,
                ),
                patch("tauceti_tools.review_source.Forgejo", return_value=api),
            ):
                self.assertEqual(source_for("Owner/Project").repository.forge.url, "https://git.example.org")


if __name__ == "__main__":
    unittest.main()
