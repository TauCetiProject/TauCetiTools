#!/usr/bin/env python3
"""Native Lake publication and the small credential-boundary copy."""

import contextlib
import hashlib
import io
import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

import lake_cache_publish as publish

INTEGRATION = "--integration" in sys.argv
if INTEGRATION:
    sys.argv.remove("--integration")
TOOLCHAIN = os.environ.get("ELAN_TOOLCHAIN") or "leanprover/lean4:v4.36.0-rc2"
SHA = "a" * 40
NAME = "0123456789abcdef.ltar"
REPO = "TauCetiProject/TauCeti"
UPLOAD_ENDPOINT = "https://upload.example/artifacts"
PUBLISHER = Path(os.environ.get("TAUCETI_PUBLISHER_WORKFLOW")
                 or Path(__file__).resolve().parents[3] / ".github/workflows/publish-lake-cache.yml")


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.raw = ('"2026-03-17"\n' + json.dumps(["1" * 16, NAME]) + "\n").encode()
        (self.source / "outputs.jsonl").write_bytes(self.raw)
        (self.source / NAME).write_bytes(b"archive")

    def copy(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return publish.stage(self.source, self.root / "copied")

    def test_full_native_stage_has_no_additional_manifest_or_network(self):
        result = self.copy()
        self.assertEqual(set(p.name for p in (self.root / "copied").iterdir()), {"outputs.jsonl", NAME})
        self.assertEqual((self.root / "copied" / NAME).read_bytes(), b"archive")
        self.assertEqual(result["upload_bytes"], result["full_bytes"])
        self.assertEqual(result["inherited"], 0)
        (self.source / NAME).write_bytes(b"changed later")
        self.assertEqual((self.root / "copied" / NAME).read_bytes(), b"archive")

    def test_missing_artifact_never_leaves_a_partial_copy(self):
        (self.source / NAME).unlink()
        with self.assertRaises(ValueError):
            self.copy()
        self.assertFalse((self.root / "copied").exists())

    def test_symlink_and_fifo_rejected_without_reading_them(self):
        for kind in ("symlink", "fifo"):
            with self.subTest(kind=kind):
                target = self.source / NAME
                target.unlink()
                if kind == "symlink":
                    target.symlink_to(self.source / "outputs.jsonl")
                else:
                    os.mkfifo(target)
                with self.assertRaises((ValueError, OSError)):
                    self.copy()
                self.assertFalse((self.root / "copied").exists())

    def test_unsafe_or_partial_maps_and_extra_files_are_rejected(self):
        for data in (b"", b'"2026-03-17"\n', b'"2026-03-17"\n["1111111111111111","../secret"]\n'):
            with self.subTest(data=data):
                (self.source / "outputs.jsonl").write_bytes(data)
                with self.assertRaises((ValueError, UnicodeError)):
                    self.copy()
        (self.source / "outputs.jsonl").write_bytes(self.raw)
        (self.source / "publication.json").write_text("{}")
        with self.assertRaises(ValueError):
            self.copy()

    def test_historical_input_aliases_can_share_one_archive(self):
        (self.source / "outputs.jsonl").write_bytes(self.raw + (json.dumps(["2" * 16, NAME]) + "\n").encode())
        self.assertEqual(self.copy()["objects"], 1)

    def test_split_parts_merge_back_to_the_staged_copy(self):
        names = [f"{index:016x}.ltar" for index in range(7)]
        raw = '"2026-03-17"\n' + "".join(json.dumps([f"{index + 1:016x}", name]) + "\n" for index, name in enumerate(names))
        (self.source / NAME).unlink()
        (self.source / "outputs.jsonl").write_text(raw)
        for name in names:
            (self.source / name).write_bytes(name.encode())
        self.copy()
        sizes = publish.split(self.root / "copied", self.root / "parts", 3)
        self.assertEqual(sizes, [4, 2, 2])
        self.assertEqual(os.listdir(self.root / "copied"), [])
        self.assertTrue((self.root / "parts/part-0/outputs.jsonl").is_file())
        merged = self.root / "merged"
        merged.mkdir()
        for part in (self.root / "parts").iterdir():
            for path in part.iterdir():
                os.rename(path, merged / path.name)
        with contextlib.redirect_stdout(io.StringIO()):
            result = publish.stage(merged, self.root / "again")
        self.assertEqual(result["objects"], len(names))

    def plan(self, answers=None):
        """Stage, check and select with HEAD ``answers`` (None: no answer file)."""
        names = ["0123456789abcdef.ltar", "1123456789abcdef.ltar", "2123456789abcdef.ltar"]
        (self.source / NAME).unlink()
        raw = '"2026-03-17"\n' + "".join(json.dumps([f"{i + 1:016x}", n]) + "\n" for i, n in enumerate(names))
        (self.source / "outputs.jsonl").write_text(raw)
        for name in names:
            (self.source / name).write_bytes(name.encode() * 3)
        plan, upload = self.root / "plan", self.root / "upload"
        with contextlib.redirect_stdout(io.StringIO()):
            stats = publish.stage(self.source, plan)
            (plan / "metrics.json").write_text(json.dumps(stats))
            self.assertEqual(publish.check(plan, upload, UPLOAD_ENDPOINT, REPO), 3)
            if answers is not None:
                (upload / "heads.out").write_text(answers(names))
            stats = publish.select(plan, upload, "https://upload.example/revisions", REPO, TOOLCHAIN, SHA)
        config = (upload / "artifacts.curl").read_text()
        uploaded = [n for n in names if f"/{REPO}/{n[:-5]}.art" in config]
        return names, stats, uploaded, upload

    @staticmethod
    def url(name):
        return f"{UPLOAD_ENDPOINT}/{REPO}/{name[:-5]}.art"

    @staticmethod
    def etag(name):
        return '"' + hashlib.md5(name.encode() * 3).hexdigest() + '"'

    def test_check_names_lake_urls_and_select_without_answers_uploads_everything(self):
        names, stats, uploaded, upload = self.plan()
        heads = (upload / "heads.curl").read_text()
        self.assertEqual([line for line in heads.splitlines() if line.startswith("url")],
                         [f"url = {json.dumps(self.url(n))}" for n in names])
        self.assertEqual(uploaded, names)
        self.assertEqual((stats["inherited"], stats["uploaded_objects"]), (0, 3))
        self.assertEqual(stats["upload_bytes"], stats["full_bytes"])
        self.assertEqual((upload / "map-url").read_text().strip(),
                         f"https://upload.example/revisions/{REPO}/tc/{TOOLCHAIN.replace('/', '--').replace(':', '---')}/{SHA}.jsonl")
        self.assertIn(f'upload-file = {json.dumps(str(self.root / "plan" / names[0]))}',
                      (upload / "artifacts.curl").read_text())

    def test_only_a_200_with_the_copy_md5_is_skipped(self):
        def answers(names):
            return (f"200\t{self.etag(names[0])}\t{self.url(names[0])}\n"   # identical: skipped
                    f"200\t\"{'0' * 32}\"\t{self.url(names[1])}\n"       # different bytes
                    f"404\t\t{self.url(names[2])}\n"                       # missing
                    f"200\t{self.etag(names[2])}\thttps://other.example/{names[2][:-5]}.art\n"
                    f"200\t{self.etag(names[1])}\t{self.url(names[1])}x\n"  # truncated or altered line
                    "garbage\n")
        names, stats, uploaded, _ = self.plan(answers)
        self.assertEqual(uploaded, names[1:])
        self.assertEqual((stats["inherited"], stats["uploaded_objects"]), (1, 2))
        self.assertLess(stats["upload_bytes"], stats["full_bytes"])

    def test_an_unreadable_answer_file_uploads_everything(self):
        names, stats, uploaded, _ = self.plan(lambda names: "")
        upload = self.root / "upload"
        (upload / "heads.out").unlink()
        (upload / "heads.out").mkdir()  # opening it raises IsADirectoryError
        self.assertEqual(publish.present(upload), set())

    def test_every_identical_object_skipped_leaves_only_the_map(self):
        names, stats, uploaded, upload = self.plan(
            lambda names: "".join(f"200\t{self.etag(n)}\t{self.url(n)}\n" for n in names))
        self.assertEqual(uploaded, [])
        self.assertEqual((upload / "artifacts.curl").read_text(), "")
        self.assertEqual((stats["inherited"], stats["uploaded_objects"], stats["upload_bytes"]), (3, 0, 0))

    def test_select_rejects_a_request_list_for_another_map_or_an_encoded_toolchain(self):
        self.copy()
        plan, upload = self.root / "copied", self.root / "upload"
        (plan / "metrics.json").write_text("{}")
        publish.check(plan, upload, UPLOAD_ENDPOINT, REPO)
        with self.assertRaises(ValueError):
            publish.select(plan, upload, "https://upload.example/revisions", REPO, "leanprover/lean4:v4+x", SHA)
        (upload / "expected.json").write_text(json.dumps({self.url("f" * 16 + ".ltar"): ["f" * 16 + ".ltar", "0" * 32]}))
        with self.assertRaises(ValueError):
            publish.select(plan, upload, "https://upload.example/revisions", REPO, TOOLCHAIN, SHA)
        self.assertFalse((upload / "map-url").exists())
        with self.assertRaises(ValueError):
            publish.artifact_url(UPLOAD_ENDPOINT, "../x", NAME)

    def test_split_rejects_a_copy_without_its_map_or_a_bad_count(self):
        self.copy()
        with self.assertRaises(ValueError):
            publish.split(self.root / "copied", self.root / "parts", 0)
        (self.root / "copied" / "outputs.jsonl").unlink()
        with self.assertRaises(ValueError):
            publish.split(self.root / "copied", self.root / "parts", 2)


@unittest.skipUnless(INTEGRATION, "pass --integration with stock Lake installed")
class StockLakePublication(unittest.TestCase):
    """Run the publisher workflow's own steps against a local TLS bucket, then fetch with stock Lake."""

    def test_publisher_steps_upload_missing_artifacts_and_stock_lake_restores(self):
        self.assertTrue(PUBLISHER.is_file(), f"set TAUCETI_PUBLISHER_WORKFLOW; {PUBLISHER} is missing")
        steps = {step.get("name"): step for step in yaml.safe_load(PUBLISHER.read_text())["jobs"]["publish"]["steps"]}
        sequence = [
            steps["Copy native staging data into the credential boundary"],
            steps["Check which artifacts the bucket already holds"],
            steps["Select the artifacts the bucket lacks"],
            steps["Publish the staged Lake cache"],
        ]
        self.assertTrue(sequence[1]["continue-on-error"])
        with tempfile.TemporaryDirectory(prefix="native-publication-") as temporary:
            root = Path(temporary)
            objects, requests, mode = {}, [], {"put": True, "head": True}

            class Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def log_message(self, *args):
                    pass

                def reply(self, code, data=b"", headers=()):
                    self.send_response(code)
                    for header in headers:
                        self.send_header(*header)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(data)

                def do_HEAD(self):
                    requests.append(("HEAD", self.path))
                    if not mode["head"]:
                        self.reply(500)
                    elif self.path in objects:
                        data = objects[self.path]
                        self.send_response(200)
                        self.send_header("ETag", '"' + hashlib.md5(data).hexdigest() + '"')
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                    else:
                        self.reply(404)

                def do_GET(self):
                    requests.append(("GET", self.path))
                    if self.path in objects:
                        self.reply(200, objects[self.path])
                    else:
                        self.reply(404)

                def do_PUT(self):
                    data = self.rfile.read(int(self.headers["Content-Length"]))
                    requests.append(("PUT", self.path))
                    if mode["put"] is not True and "/artifacts/" in self.path:
                        if mode["put"] == "redirect":
                            self.reply(307, headers=[("Location", "https://localhost:1/elsewhere")])
                        else:
                            self.reply(403)
                        return
                    objects[self.path] = data
                    self.reply(200)

            cert, key = root / "cert.pem", root / "key.pem"
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                 "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
                 "-addext", "subjectAltName=DNS:localhost"],
                check=True, capture_output=True,
            )
            class Server(ThreadingHTTPServer):
                def handle_error(self, request, client_address):
                    pass  # curl --fail-early resets the other transfers' connections

            server = Server(("localhost", 0), Handler)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            endpoint = f"https://localhost:{server.server_port}"
            env = {
                **os.environ,
                "CURL_CA_BUNDLE": str(cert),
                "ELAN_TOOLCHAIN": TOOLCHAIN,
                "LAKE_ARTIFACT_CACHE": "true",
                "LAKE_RESTORE_ARTIFACTS": "true",
            }
            for name in ("LAKE_NO_CACHE", "LAKE_CONFIG", "LAKE_CACHE_KEY", "LAKE_CACHE_KEY_RAW"):
                env.pop(name, None)

            def project(name):
                p = root / name
                p.mkdir()
                (p / "lean-toolchain").write_text(TOOLCHAIN + "\n")
                (p / "lakefile.toml").write_text(
                    'name = "TauCeti"\nplatformIndependent = true\n'
                    'defaultTargets = ["TauCeti"]\n[[lean_lib]]\nname = "TauCeti"\n'
                )
                (p / "TauCeti.lean").write_text("module\npublic import TauCeti.Basic\n")
                (p / "TauCeti").mkdir()
                (p / "TauCeti/Basic.lean").write_text("module\npublic def value : Nat := 7\n")
                return p

            def lake(p, *args, ok=True, extra=None):
                result = subprocess.run(
                    ["lake", *args], cwd=p,
                    env={**env, "LAKE_CACHE_DIR": str(p / ".lake/cache"), **(extra or {})},
                    capture_output=True, text=True, timeout=120,
                )
                self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
                return result.stdout + result.stderr

            producer = project("producer")
            lake(producer, "update")
            lake(producer, "build", "--iofail", "--rehash", "-o", ".lake/outputs.jsonl")
            lake(producer, "cache", "stage", ".lake/outputs.jsonl", str(root / "native-stage"))
            archives = len(publish.mapping((root / "native-stage/outputs.jsonl").read_bytes()))
            self.assertGreaterEqual(archives, 2)
            workspace = root / "workspace"
            (workspace / "publisher-tools/tauceti_tools/assets").mkdir(parents=True)
            (workspace / "publisher-tools/tauceti_tools/assets/scripts").symlink_to(Path(publish.__file__).resolve().parent)
            runs = [0]

            def publish_revision(revision, ok=True):
                """Run the four steps as the workflow does, each with only its own environment."""
                runs[0] += 1
                temp = root / f"runner-temp-{runs[0]}"
                temp.mkdir()
                values = {
                    "STAGING": str(root / "native-stage"),
                    "S3_ARTIFACT_ENDPOINT": endpoint + "/artifacts",
                    "S3_REVISION_ENDPOINT": endpoint + "/revisions",
                    "PUBLIC_ARTIFACT_ENDPOINT": endpoint + "/artifacts",
                    "PUBLIC_REVISION_ENDPOINT": endpoint + "/revisions",
                    "LAKE_CACHE_KEY_RAW": "test-id:test-secret\n",
                    "REVISION": revision,
                    "TOOLCHAIN": TOOLCHAIN,
                    "PUBLICATION_REQUIRED": "true",
                }
                del requests[:]
                for step in sequence:
                    step_env = {name: values[name] for name in step.get("env", {})}
                    result = subprocess.run(
                        ["bash", "-c", step["run"]], cwd=workspace, capture_output=True, text=True, timeout=120,
                        env={**env, **step_env, "RUNNER_TEMP": str(temp), "GITHUB_REPOSITORY": REPO,
                             "GITHUB_OUTPUT": str(temp / "output")},
                    )
                    shown = [line for line in (result.stdout + result.stderr).splitlines()
                             if not line.startswith("::add-mask::")]
                    self.assertNotIn("test-secret", "\n".join(shown))
                    if result.returncode and step is sequence[-1] and not ok:
                        return
                    self.assertEqual(result.returncode, 0, step["name"] + result.stdout + result.stderr)
                self.assertTrue(ok, "publication unexpectedly succeeded")
                self.assertEqual(requests[-1][0], "PUT")
                self.assertIn(f"/revisions/{REPO}/tc/", requests[-1][1])
                return json.loads((temp / "lake-publication-plan/metrics.json").read_text())

            def count(method, kind):
                return sum(1 for m, path in requests if m == method and f"/{kind}/" in path)

            # A failed artifact upload never publishes the map; a retry does.
            for refusal in ("forbidden", "redirect"):
                mode["put"] = refusal
                publish_revision(SHA, ok=False)
                self.assertFalse(any("/revisions/" in path for path in objects), refusal)
            mode["put"] = True
            stats = publish_revision(SHA)
            self.assertEqual((count("HEAD", "artifacts"), count("PUT", "artifacts")), (archives, archives))
            self.assertEqual((stats["inherited"], stats["uploaded_objects"]), (0, archives))

            consumer = project("consumer")
            lake(consumer, "update")
            config = root / "lake.toml"
            config.write_text(
                '[[cache.service]]\nname = "test"\nkind = "s3"\nartifactEndpoint = "' + endpoint + '/artifacts"\n'
                'revisionEndpoint = "' + endpoint + '/revisions"\n'
            )

            def fetch(revision, name, ok=True):
                p = project(name)
                lake(p, "update")
                lake(p, "cache", "get", "--service=test", f"--repo={REPO}", "--rev=" + revision,
                     ok=ok, extra={"LAKE_CONFIG": str(config)})
                if ok:
                    restored = lake(p, "build", "--iofail", "-o", ".lake/restored.jsonl")
                    self.assertIn("Unpacked TauCeti", restored)
                    self.assertNotIn("Built TauCeti", restored)
                    self.assertEqual(publish.mapping((producer / ".lake/outputs.jsonl").read_bytes()),
                                     publish.mapping((p / ".lake/restored.jsonl").read_bytes()))
                return p

            fetch(SHA, "first-consumer")

            # Identical objects already in the bucket are not uploaded again.
            stats = publish_revision("b" * 40)
            self.assertEqual((count("HEAD", "artifacts"), count("PUT", "artifacts"), count("PUT", "revisions")),
                             (archives, 0, 1))
            self.assertEqual((stats["inherited"], stats["uploaded_objects"], stats["upload_bytes"]), (archives, 0, 0))
            fetch("b" * 40, "second-consumer")

            # A deleted object and one holding other bytes are uploaded again.
            stored = sorted(path for path in objects if "/artifacts/" in path)
            good = objects[stored[1]]
            del objects[stored[0]]
            objects[stored[1]] = b"corrupt artifact"
            publish_revision("c" * 40)
            self.assertEqual(sorted(path for m, path in requests if m == "PUT" and "/artifacts/" in path), stored[:2])
            self.assertEqual(objects[stored[1]], good)
            fetch("c" * 40, "third-consumer")

            # When the existence check fails, every artifact is uploaded.
            mode["head"] = False
            stats = publish_revision("d" * 40)
            mode["head"] = True
            self.assertEqual(count("PUT", "artifacts"), archives)
            self.assertEqual(stats["inherited"], 0)

            changed = fetch("d" * 40, "changed-consumer")
            (changed / "TauCeti/Basic.lean").write_text("module\npublic def value : Nat := 8\n")
            self.assertIn("Built TauCeti", lake(changed, "build", "--iofail", "-o", ".lake/changed.jsonl"))

            objects[stored[0]] = b"corrupt artifact"
            fetch("d" * 40, "damaged-consumer", ok=False)


if __name__ == "__main__":
    unittest.main()
