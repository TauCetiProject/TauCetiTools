"""Complete Git histories, isolated checkouts, retries and offline native builds."""

import base64
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import ci_checkout as checkout
from prepare_pr_integration import prepare

INTEGRATION = "--integration" in sys.argv
if INTEGRATION:
    sys.argv.remove("--integration")


class Checkouts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ci-source-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "upstream"
        self.repo.mkdir()
        self.git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "lakefile.toml").write_text(
            'name = "fixture"\ndefaultTargets = ["Fixture"]\n[[lean_lib]]\nname = "Fixture"\n'
        )
        (self.repo / "lake-manifest.json").write_text('{"version":"1.3.0","packages":[]}\n')
        (self.repo / "lean-toolchain").write_text("leanprover/lean4:v4.36.0-rc2\n")
        (self.repo / "Fixture.lean").write_text("module\npublic def answer : Nat := 42\n")
        # Such a tracked name must never be used for the helper's own HOME.
        (self.repo / ".git-home").write_text("candidate file to preserve\n")
        self.base = self.commit(self.repo, "base")
        self.git(self.repo, "checkout", "-qb", "feature")
        (self.repo / "Fixture.lean").write_text("module\npublic def answer : Nat := 43\n")
        self.head = self.commit(self.repo, "head")
        self.git(self.repo, "checkout", "-q", "main")
        (self.repo / "base-only").write_text("base advanced\n")
        self.tip = self.commit(self.repo, "base advance")
        self.workspace = self.root / "job"
        self.workspace.mkdir()
        self.real_url = checkout.github_url

    def git(self, path, *args):
        return subprocess.check_output(
            ["git", "--no-replace-objects", "-C", str(path), *map(str, args)], stderr=subprocess.PIPE, text=True
        ).strip()

    def commit(self, path, message):
        self.git(path, "add", "--all")
        self.git(path, "-c", "user.name=CI", "-c", "user.email=ci@example.invalid", "commit", "-qm", message)
        return self.git(path, "rev-parse", "HEAD")

    def local_url(self, repository):
        self.real_url(repository)
        return (self.repo if repository == "Owner/Repo" else self.root / "fork").as_uri()

    def fetch(self, **overrides):
        args = dict(
            repository="Owner/Repo",
            head_repository="Owner/Repo",
            head=self.head,
            base=self.tip,
            mergebase=self.base,
            workspace=self.workspace,
        )
        args.update(overrides)
        with patch.object(checkout, "github_url", side_effect=self.local_url):
            return checkout.fetch_sources(**args)

    def materialize(self, role, commit):
        # Origin configuration remains credential-free and names GitHub, not the
        # local fixture transport used by fetch_sources.
        return checkout.materialize(self.workspace, role, commit)

    def test_complete_histories_exact_checkouts_and_stable_integration(self):
        token = "credential-not-for-config"
        result = self.fetch(token=token)
        store = Path(result["store"])
        self.assertFalse((store / "shallow").exists())
        self.assertEqual(self.git(store, "for-each-ref", "--format=%(refname)"), "refs/ci/base\nrefs/ci/head")
        for role, sha in (("head", self.head), ("base", self.tip), ("mergebase", self.base)):
            self.materialize(role, sha)
            path = self.workspace / checkout.ROLES[role]
            self.assertEqual(self.git(path, "rev-parse", "HEAD"), sha)
            self.assertEqual((path / ".git-home").read_text(), "candidate file to preserve\n")
            self.assertNotIn(token, (path / ".git/config").read_text())
        identity = prepare(self.workspace / "pr", self.workspace / "base", self.head, self.tip, "pr-integration")
        self.assertEqual(self.git(self.workspace / "pr", "rev-parse", "HEAD^{tree}"), identity["tested_tree"])
        self.assertEqual(self.git(self.workspace / "pr", "merge-base", self.head, self.tip), self.base)
        self.assertEqual(
            (self.workspace / "pr-raw-head/.git/objects/info/alternates").read_text(), str(store / "objects") + "\n"
        )
        self.assertNotIn(token, (store / "config").read_text() + (store / "identity.json").read_text())
        self.assertFalse(list(self.workspace.glob(".ci-checkout-*")))

    def test_fork_only_head_and_captured_base(self):
        fork = self.root / "fork"
        self.git(self.root, "clone", "-q", self.repo, fork)
        self.git(fork, "checkout", "-q", "feature")
        (fork / "fork-only").write_text("fork work\n")
        head = self.commit(fork, "fork head")
        self.fetch(head_repository="Fork/Repo", head=head)
        # A later moving upstream tip must not change either materialized input.
        (self.repo / "later").write_text("later main\n")
        self.commit(self.repo, "later main")
        self.materialize("head", head)
        self.materialize("base", self.tip)
        self.assertEqual(self.git(self.workspace / "pr", "merge-base", head, self.tip), self.base)
        self.assertEqual(
            self.git(self.workspace / "pr", "remote", "get-url", "origin"), "https://github.com/Fork/Repo.git"
        )

    def test_fetch_cannot_detach_maintenance_before_connectivity_validation(self):
        trace = self.root / "source-trace.jsonl"
        initialize = checkout.SourceGit.__init__

        def aggressive_maintenance(git, *args, **kwargs):
            initialize(git, *args, **kwargs)
            git.env["GIT_TRACE2_EVENT"] = str(trace)
            git.command += ["-c", "gc.autoPackLimit=1", "-c", "gc.autoDetach=true", "-c", "fetch.unpackLimit=1"]

        fork = self.root / "fork"
        self.git(self.root, "clone", "-q", self.repo, fork)
        self.git(fork, "checkout", "-q", "feature")
        (fork / "fork-only").write_text("candidate objects absent upstream\n")
        head = self.commit(fork, "fork head")
        with patch.object(checkout.SourceGit, "__init__", aggressive_maintenance):
            self.fetch(head_repository="Fork/Repo", head=head)
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        children = [event.get("argv", []) for event in events if event.get("event") == "child_start"]
        self.assertFalse([argv for argv in children if "maintenance" in argv or "gc" in argv])
        commands = [event.get("argv", []) for event in events if event.get("event") == "start"]
        self.assertEqual(sum("fetch" in argv for argv in commands), 2)
        self.assertTrue(any("fsck" in argv and "--connectivity-only" in argv for argv in commands))
        self.materialize("head", head)
        self.materialize("base", self.tip)
        self.assertEqual(self.git(self.workspace / "pr", "merge-base", head, self.tip), self.base)

    def test_bad_identities_existing_paths_and_symlinks_are_not_overwritten(self):
        for values in (dict(head="main"), dict(repository="../bad"), dict(head="0" * 40)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.fetch(**values)
        store = self.workspace / checkout.STORE
        store.mkdir()
        (store / "keep").write_text("owned by another operation")
        with self.assertRaises(FileExistsError):
            self.fetch()
        self.assertTrue((store / "keep").exists())
        shutil.rmtree(store)
        store.symlink_to(self.root / "absent")
        with self.assertRaises(FileExistsError):
            self.fetch()
        self.assertTrue(store.is_symlink())

    def test_changed_merge_base_refuses_source_and_cleans_only_owned_store(self):
        with self.assertRaisesRegex(ValueError, "merge base"):
            self.fetch(mergebase=self.tip)
        self.assertFalse((self.workspace / checkout.STORE).exists())
        self.assertTrue(self.repo.is_dir())

    def test_checkout_must_match_role_and_target_must_be_new(self):
        self.fetch()
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.materialize("head", self.tip)
        target = self.workspace / "pr"
        target.symlink_to(self.root / "absent")
        with self.assertRaisesRegex(ValueError, "must be new"):
            self.materialize("head", self.head)
        self.assertTrue(target.is_symlink())

    def test_hostile_git_environment_and_checkout_filters_do_not_execute(self):
        self.git(self.repo, "checkout", "-q", "feature")
        (self.repo / ".gitattributes").write_text("filter-target filter=payload\n")
        (self.repo / "filter-target").write_text("ordinary contents\n")
        head = self.commit(self.repo, "filter attributes")
        marker = self.root / "executed"
        config = self.root / "malicious-config"
        config.write_text(f'[filter "payload"]\n smudge = touch {marker}\n required = true\n')
        with patch.dict(
            os.environ,
            {
                "GIT_CONFIG_GLOBAL": str(config),
                "GIT_DIR": "/bad",
                "GIT_OBJECT_DIRECTORY": "/bad",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.hooksPath",
                "GIT_CONFIG_VALUE_0": "/bad",
            },
        ):
            self.fetch(head=head)
            self.materialize("head", head)
        self.assertFalse(marker.exists())
        self.assertEqual((self.workspace / "pr/filter-target").read_text(), "ordinary contents\n")

    def test_transfer_retries_keep_the_same_requested_objects_and_timeout_scope(self):
        store = self.workspace / checkout.STORE
        store.mkdir()
        with tempfile.TemporaryDirectory() as home:
            git = checkout.SourceGit(store, home, fetch_timeout=600)
            errors = [subprocess.CalledProcessError(128, "git"), subprocess.TimeoutExpired("git", 600), None]
            refs = [(self.head, "refs/ci/head")]
            with (
                patch.object(git, "transfer", side_effect=errors) as transfer,
                patch.object(checkout.time, "sleep") as sleep,
            ):
                git.fetch_sources("https://github.com/Owner/Repo.git", refs)
            self.assertEqual(transfer.call_count, 3)
            self.assertTrue(
                all(call.args == ("https://github.com/Owner/Repo.git", refs) for call in transfer.call_args_list)
            )
            self.assertEqual(sleep.call_count, 2)
            self.assertTrue(all(10 <= call.args[0] <= 20 for call in sleep.call_args_list))
            self.assertEqual(git.fetch_timeout, 600)

    def test_transfer_diagnostics_redact_credentials_and_escape_remote_text(self):
        token = "credential-not-for-logs"
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        with tempfile.TemporaryDirectory() as home:
            git = checkout.SourceGit(self.workspace / "store", home, token=token)
            error = subprocess.CalledProcessError(
                128, "git", stderr=f"fatal: {token} {encoded}\n::warning::remote".encode()
            )
            output = io.StringIO()
            with patch.object(git, "transfer", side_effect=error), contextlib.redirect_stderr(output):
                with self.assertRaises(subprocess.CalledProcessError):
                    git.fetch_sources("https://github.com/Owner/Repo.git", [], attempts=1)
            text = output.getvalue()
            self.assertNotIn(token, text)
            self.assertNotIn(encoded, text)
            self.assertIn("fatal:", text)
            self.assertNotIn("\n::warning::", text)

    def test_cleanup_failure_preserves_the_original_source_error(self):
        remove = shutil.rmtree

        def fail_own_store(path, *args, **kwargs):
            if Path(path) == self.workspace / checkout.STORE:
                raise PermissionError("cleanup failed")
            return remove(path, *args, **kwargs)

        with patch.object(checkout.shutil, "rmtree", side_effect=fail_own_store):
            with self.assertRaisesRegex(ValueError, "resolved merge base"):
                self.fetch(mergebase=self.head)

    def test_cli_exports_objects_only_after_success(self):
        environment = self.root / "environment"
        environment.write_text("existing=kept\n")
        args = [
            "ci_checkout.py",
            "fetch",
            "--repository",
            "Owner/Repo",
            "--head-repository",
            "Owner/Repo",
            "--head",
            self.head,
            "--base",
            self.tip,
            "--mergebase",
            self.base,
            "--workspace",
            str(self.workspace),
            "--github-env",
            str(environment),
        ]
        for failed in (True, False):
            with (
                self.subTest(failed=failed),
                patch.object(sys, "argv", args),
                patch.object(checkout.signal, "signal"),
                patch.object(
                    checkout,
                    "fetch_sources",
                    side_effect=ValueError("failed") if failed else None,
                    return_value={"store": str(self.workspace / checkout.STORE)},
                ),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(checkout.main(), 1 if failed else 0)
                self.assertEqual("CI_SOURCE_OBJECTS=" in environment.read_text(), not failed)
                self.assertIn("existing=kept", environment.read_text())

    def fake_git(self):
        real_git = shutil.which("git")
        directory = self.root / "bin"
        directory.mkdir()
        state = self.root / "child.json"
        executable = directory / "git"
        # Child intentionally ignores TERM and holds the transfer pipes open.
        executable.write_text(
            f"#!{sys.executable}\nimport json,os,subprocess,sys,time\n"
            f'if "fetch" not in sys.argv:os.execv({real_git!r},[{real_git!r},*sys.argv[1:]])\n'
            'child=subprocess.Popen([sys.executable,"-c","import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)"])\n'
            f'open({str(state)!r},"w").write(json.dumps({{"parent":os.getpid(),"child":child.pid}}))\n'
            "child.wait()\n"
        )
        executable.chmod(0o755)
        return directory, state

    def assert_dead(self, pid):
        path = Path(f"/proc/{pid}/stat")
        self.assertTrue(not path.exists() or path.read_text().split()[2] == "Z", f"orphan transfer {pid}")

    @unittest.skipUnless(sys.platform == "linux", "Linux process-group fixture")
    def test_timeout_kills_transfer_descendants_before_returning(self):
        directory, state = self.fake_git()
        with patch.dict(os.environ, PATH=str(directory) + os.pathsep + os.environ["PATH"]):
            git = checkout.SourceGit(self.workspace / "store", self.root, fetch_timeout=0.3)
            with self.assertRaises(subprocess.TimeoutExpired):
                git.transfer("https://github.com/Owner/Repo.git", [(self.head, "refs/ci/head")])
        value = json.loads(state.read_text())
        self.assert_dead(value["parent"])
        self.assert_dead(value["child"])

    @unittest.skipUnless(sys.platform == "linux", "Linux cancellation fixture")
    def test_cli_cancellation_forwards_to_detached_transfer_and_cleans_store(self):
        directory, state = self.fake_git()
        args = [
            sys.executable,
            str(Path(checkout.__file__)),
            "fetch",
            "--repository",
            "Owner/Repo",
            "--head-repository",
            "Owner/Repo",
            "--head",
            self.head,
            "--base",
            self.tip,
            "--mergebase",
            self.base,
            "--workspace",
            str(self.workspace),
        ]
        process = subprocess.Popen(
            ["bash", "-c", 'exec "$@"', "ci-source", *args],
            env={**os.environ, "PATH": str(directory) + os.pathsep + os.environ["PATH"]},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 5
            while not state.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(state.exists())
            time.sleep(0.1)
            process.terminate()
            time.sleep(0.1)
            process.terminate()  # escalation must not interrupt child cleanup
            process.communicate(timeout=10)
            self.assertEqual(process.returncode, 143)
            self.assertFalse((self.workspace / checkout.STORE).exists())
            for pid in json.loads(state.read_text()).values():
                self.assert_dead(pid)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    @unittest.skipUnless(INTEGRATION, "pass --integration with stock Lean and bwrap installed")
    def test_actual_materialization_builds_offline_in_both_modes(self):
        if not shutil.which("bwrap") or not shutil.which("lake"):
            self.skipTest("stock Lean/Lake and bwrap required")
        for mode in ("pr-integration", "frozen-candidate"):
            with self.subTest(mode=mode):
                workspace = self.root / mode
                workspace.mkdir()
                self.workspace = workspace
                base = self.tip if mode == "pr-integration" else self.base
                self.fetch(base=base)
                self.materialize("head", self.head)
                self.materialize("base", base)
                record = prepare(workspace / "pr", workspace / "base", self.head, base, mode)
                project = workspace / "pr"
                (project / ".lake").mkdir()
                # Resolve elan outside the offline sandbox. The production
                # launcher likewise mounts the installed toolchain directly;
                # a shim would try to create HOME/.elan in a read-only HOME.
                if os.environ.get("LEAN_SYSROOT"):
                    lake = Path(os.environ["LEAN_SYSROOT"]) / "bin/lake"
                elif shutil.which("elan"):
                    lake = Path(subprocess.check_output(["elan", "which", "lake"], cwd=project, text=True).strip())
                else:
                    lake = Path(shutil.which("lake")).resolve()
                command = [
                    "bwrap",
                    "--die-with-parent",
                    "--unshare-user",
                    "--unshare-pid",
                    "--unshare-net",
                    "--ro-bind",
                    "/",
                    "/",
                    "--dev",
                    "/dev",
                    "--proc",
                    "/proc",
                    "--tmpfs",
                    str(workspace),
                    "--ro-bind",
                    str(project),
                    str(project),
                    "--bind",
                    str(project / ".lake"),
                    str(project / ".lake"),
                ]
                if mode == "pr-integration":
                    for p in (workspace / "pr-raw-head/.git/objects", workspace / "base/.git/objects"):
                        command += ["--ro-bind", str(p), str(p)]
                objects = workspace / checkout.STORE / "objects"
                query = [shutil.which("git"), "-C", str(project), "merge-base", self.head, base]
                env = {
                    "PATH": str(lake.parent) + os.pathsep + os.environ["PATH"],
                    "HOME": "/nonexistent",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_NO_LAZY_FETCH": "1",
                    "LAKE_ARTIFACT_CACHE": "false",
                    "LEAN_NUM_THREADS": "2",
                }
                missing = subprocess.run(command + ["--", *query], env=env, capture_output=True)
                self.assertNotEqual(missing.returncode, 0)
                mounted = command + ["--ro-bind", str(objects), str(objects)]
                result = subprocess.run(mounted + ["--", *query], env=env, text=True, capture_output=True, check=True)
                self.assertEqual(result.stdout.strip(), self.base)
                result = subprocess.run(
                    mounted + ["--chdir", str(project), "--", str(lake), "build", "--iofail"],
                    env=env,
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue((project / ".lake/build/lib/lean/Fixture.olean").is_file())
                self.assertEqual(self.git(project, "rev-parse", "HEAD"), record["tested_sha"])


if __name__ == "__main__":
    unittest.main()
