"""Manifested workflow execution uses real local Git without a native server."""

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tauceti_tools import ci_candidate as cc
from tauceti_tools import ci_candidate_workflow as cw
from tests.test_ci_candidate import context, policy, request


class ManifestExecution(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.remote = self.root / "remote.git"
        self.executable = shutil.which("git")
        self.env = dict(
            PATH=os.defpath,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL="/dev/null",
            GIT_AUTHOR_NAME="Fixture",
            GIT_AUTHOR_EMAIL="fixture@example.invalid",
            GIT_COMMITTER_NAME="Fixture",
            GIT_COMMITTER_EMAIL="fixture@example.invalid",
        )
        subprocess.run([self.executable, "init", "--bare", "--quiet", str(self.remote)], env=self.env, check=True)
        self.git("config", "uploadpack.allowFilter", "true")
        self.tree = self.git("mktree", data=b"")
        base = self.git("commit-tree", self.tree, data=b"base\n")
        head = self.git("commit-tree", self.tree, "-p", base, data=b"candidate\n")
        member = self.git("commit-tree", self.tree, "-p", base, data=b"accepted PR head\n")
        self.config = policy()
        original = request(self.config)
        members = (cc.Member(17, member),)
        self.raw = cc.member_manifest_bytes(original.epoch, original.request_id, members)
        blob = self.git("hash-object", "-w", "--stdin", data=self.raw)
        self.manifest_tree = self.git("mktree", data=f"100644 blob {blob}\t{cc.MEMBER_MANIFEST_FILE}\n".encode())
        commit = self.git("commit-tree", self.manifest_tree, "-p", member, data=b"Retain members\n")
        self.req = replace(
            original,
            head=head,
            base=base,
            tree=self.tree,
            members=members,
            member_manifest=cc.MemberManifest(commit, hashlib.sha256(self.raw).hexdigest(), 1, len(self.raw)),
        )
        self.git("update-ref", self.req.retained_ref, head)
        self.git("update-ref", self.req.manifest_ref, commit)
        self.ctx = context(self.req, self.config)
        fixture = self
        original_git = cw.ci_git.Git

        class LocalGit(original_git):
            def run(self, *args, check=True):
                args = tuple(
                    fixture.remote.as_uri() if arg == fixture.config.source.repository.clone_url else arg
                    for arg in args
                )
                return super().run(*args, check=check)

        class Client:
            forge = fixture.config.credential_origin

            def request(self, method, path):
                if path == f"repos/{fixture.config.source.repository.name}":
                    return {"id": fixture.config.source.native_id, "full_name": fixture.config.source.repository.name}
                if path.endswith("/pulls/17"):
                    return {"number": 17, "head": {"sha": member, "ref": "feature"}, "labels": []}
                raise AssertionError("unexpected native read")

        self.client = Client()
        original_transfer = cw.ci_checkout.SourceGit.transfer

        def local_transfer(instance, url, references):
            fixture.assertEqual(url, fixture.config.source.repository.clone_url)
            return original_transfer(instance, fixture.remote.as_uri(), references)

        for patcher in (
            patch.object(cw.ci_git, "Git", LocalGit),
            patch.object(cw.ci_checkout.SourceGit, "transfer", local_transfer),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def git(self, *args, data=None):
        return (
            subprocess.run(
                [self.executable, f"--git-dir={self.remote}", *args],
                env=self.env,
                input=data,
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .strip()
        )

    def test_resolve_fetch_and_policy_keep_exact_members_and_write_a_commit_graph(self):
        result = cw.resolve(self.req, self.config, self.ctx, self.root / "resolve.git", client=self.client)
        self.assertEqual(result["request_digest"], self.req.digest)
        self.assertEqual(result["head"], self.req.head)
        job = self.root / "job"
        job.mkdir()
        fetched = cw.fetch(self.req, self.config, self.ctx, job, client=self.client)
        self.assertTrue((Path(fetched["store"]) / "objects/info/commit-graph").is_file())
        self.assertEqual(
            cw.member_size_policy(self.req, self.config, self.ctx, job), {"SOURCE_SIZE_POLICY_COMPLETE": "1"}
        )

    def test_resolve_and_fetch_reject_a_matching_commit_with_the_wrong_parents(self):
        bad = self.git("commit-tree", self.manifest_tree, data=b"Missing accepted parents\n")
        req = replace(self.req, member_manifest=replace(self.req.member_manifest, commit=bad))
        self.git("update-ref", req.manifest_ref, bad)
        with self.assertRaisesRegex(ValueError, "parents"):
            cw.resolve(req, self.config, self.ctx, self.root / "bad-resolve.git", client=self.client)
        job = self.root / "bad-job"
        job.mkdir()
        with self.assertRaisesRegex(ValueError, "parents"):
            cw.fetch(req, self.config, self.ctx, job, client=self.client)

    def test_resolve_checks_accepted_objects_even_when_a_promisor_fetch_reports_success(self):
        # Promisor transfers may relax connectivity checks. Simulate a promised
        # object remaining absent while all advertised refs and headers exist.
        real_run = cw.ci_git.Git.run
        member_head = self.req.accepted_heads[0]

        def missing_object(instance, *args, check=True):
            if args == ("cat-file", "-t", member_head):
                raise subprocess.CalledProcessError(128, ["git", "cat-file"], stderr=b"promised object missing")
            return real_run(instance, *args, check=check)

        with patch.object(cw.ci_git.Git, "run", missing_object), self.assertRaises(subprocess.CalledProcessError):
            cw.resolve(self.req, self.config, self.ctx, self.root / "missing.git", client=self.client)

    def test_cli_requires_verified_files_and_creates_manifest_output_exclusively(self):
        incoming = cc.CandidateRequest.parse(self.req.record(), policy=self.config)
        common = [
            "--policy",
            "trusted-policy",
            "--configuration",
            "trusted-project",
            "--output",
            str(self.root / "result"),
        ]
        with (
            patch.object(cw, "load_request", return_value=(incoming, self.config)),
            patch.object(cw, "context_from_environment", return_value=self.ctx),
        ):
            for command in ("fetch", "member-policy"):
                with self.subTest(command=command), self.assertRaises(SystemExit) as error:
                    cw.main([command, *common, "--workspace", str(self.root)])
                self.assertEqual(error.exception.code, 2)
            with patch.object(cw, "retained_members") as reader, self.assertRaises(SystemExit) as error:
                cw.main(["resolve", *common, "--store", str(self.root / "unused")])
            self.assertEqual(error.exception.code, 2)
            reader.assert_not_called()
            saved = self.root / "members.json"
            saved.write_bytes(b"preserve me")
            with (
                patch.object(cw, "retained_members", return_value=(self.req, self.raw)),
                patch.object(cw, "resolve") as resolver,
                self.assertRaises(FileExistsError),
            ):
                cw.main(["resolve", *common, "--store", str(self.root / "unused"), "--members-output", str(saved)])
            self.assertEqual(saved.read_bytes(), b"preserve me")
            resolver.assert_not_called()
            saved.write_bytes(self.raw + b"\n")
            for command, operation in (("fetch", "fetch"), ("member-policy", "member_size_policy")):
                with (
                    self.subTest(command=command),
                    patch.object(cw, operation) as execute,
                    self.assertRaisesRegex(ValueError, "differs"),
                ):
                    cw.main([command, *common, "--workspace", str(self.root), "--members-file", str(saved)])
                execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
