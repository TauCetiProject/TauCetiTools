"""Exercise the retained reader against real Git entries and untrusted API data."""

import base64
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


class RetainedReader(unittest.TestCase):
    def test_git_entries_parents_and_blob_metadata_are_verified(self):
        config = policy()
        original = request(config)
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory) / "remote.git"
            executable = shutil.which("git")
            env = dict(
                PATH=os.defpath,
                GIT_CONFIG_NOSYSTEM="1",
                GIT_CONFIG_GLOBAL="/dev/null",
                GIT_AUTHOR_NAME="Fixture",
                GIT_AUTHOR_EMAIL="fixture@example.invalid",
                GIT_COMMITTER_NAME="Fixture",
                GIT_COMMITTER_EMAIL="fixture@example.invalid",
            )
            subprocess.run([executable, "init", "--bare", "--quiet", str(remote)], env=env, check=True)

            def git(*args, data=None):
                return (
                    subprocess.run(
                        [executable, f"--git-dir={remote}", *args], env=env, input=data, capture_output=True, check=True
                    )
                    .stdout.decode()
                    .strip()
                )

            empty_tree = git("mktree", data=b"")
            parent = git("commit-tree", empty_tree, data=b"Accepted source head\n")
            members = (cc.Member(17, parent),)
            raw = cc.member_manifest_bytes(original.epoch, original.request_id, members)
            blob_id = git("hash-object", "-w", "--stdin", data=raw)
            original_git = cw.ci_git.Git

            class LocalGit(original_git):
                def run(self, *args, check=True):
                    args = tuple(remote.as_uri() if arg == config.source.repository.clone_url else arg for arg in args)
                    return super().run(*args, check=check)

            class Client:
                forge = config.credential_origin

                def __init__(self, blob):
                    self.blob = blob

                def request(self, method, path):
                    if path == f"repos/{config.source.repository.name}":
                        return {"id": config.source.native_id, "full_name": config.source.repository.name}
                    expected = f"repos/{config.source.repository.name}/git/blobs/{blob_id}"
                    if method != "GET" or path != expected:
                        raise AssertionError("unexpected native read")
                    return self.blob

            ordinary = f"100644 blob {blob_id}\t{cc.MEMBER_MANIFEST_FILE}\n"
            variants = [
                ordinary,
                ordinary + f"100644 blob {blob_id}\tother\n",
                ordinary.replace("100644", "100755"),
                ordinary.replace("100644", "120000"),
                ordinary.replace(cc.MEMBER_MANIFEST_FILE, "wrong.json"),
                f"040000 tree {empty_tree}\t{cc.MEMBER_MANIFEST_FILE}\n",
            ]
            good_blob = {
                "sha": blob_id,
                "size": len(raw),
                "encoding": "base64",
                "content": base64.b64encode(raw).decode(),
            }
            with patch.object(cw.ci_git, "Git", LocalGit):
                for index, entry in enumerate(variants):
                    tree = git("mktree", data=entry.encode())
                    commit = git("commit-tree", tree, "-p", parent, data=b"Retain membership\n")
                    descriptor = cc.MemberManifest(commit, hashlib.sha256(raw).hexdigest(), 1, len(raw))
                    req = replace(original, members=(), member_manifest=descriptor)
                    git("update-ref", req.manifest_ref, commit)
                    with self.subTest(entry=entry):
                        if index == 0:
                            bound, actual = cw.retained_members(
                                req, config, context(req, config), token="native-token", client=Client(good_blob)
                            )
                            self.assertEqual(bound.accepted_members, members)
                            self.assertEqual(actual, raw)
                        else:
                            reason = "exactly one data file" if index == 1 else "ordinary data file"
                            with self.assertRaisesRegex(ValueError, reason):
                                cw.retained_members(req, config, context(req, config), client=Client(good_blob))
                tree = git("mktree", data=ordinary.encode())
                commit = git("commit-tree", tree, "-p", parent, data=b"Valid retention\n")
                descriptor = cc.MemberManifest(commit, hashlib.sha256(raw).hexdigest(), 1, len(raw))
                req = replace(original, members=(), member_manifest=descriptor)
                git("update-ref", req.manifest_ref, commit)
                for key, invalid in (
                    ("sha", "f" * 40),
                    ("size", len(raw) + 1),
                    ("size", True),
                    ("encoding", "plain"),
                    ("content", "!invalid!"),
                    ("content", base64.b64encode(b"x" * len(raw)).decode()),
                ):
                    reason = "invalid retained membership blob"
                    if key == "content":
                        reason = "base64" if invalid == "!invalid!" else "differs from its Git blob"
                    with self.subTest(key=key, invalid=str(invalid)[:80]), self.assertRaisesRegex(ValueError, reason):
                        cw.retained_members(
                            req, config, context(req, config), client=Client(dict(good_blob, **{key: invalid}))
                        )
                with self.assertRaises(ValueError):
                    cw.retained_members(
                        replace(req, member_manifest=replace(descriptor, sha256="f" * 64)),
                        config,
                        context(req, config),
                        client=Client(good_blob),
                    )
                no_parent = git("commit-tree", tree, data=b"Missing retained parent\n")
                git("update-ref", req.manifest_ref, no_parent)
                req = replace(req, member_manifest=replace(descriptor, commit=no_parent))
                with self.assertRaisesRegex(ValueError, "parents"):
                    cw.retained_members(req, config, context(req, config), client=Client(good_blob))


if __name__ == "__main__":
    unittest.main()
