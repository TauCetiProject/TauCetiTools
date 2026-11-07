import base64
import hashlib
import json
import unittest
import urllib.parse
from unittest.mock import Mock

from tauceti_tools.forge import Forge, ForgeError, Incomplete, RepositoryRef, Unsupported
from tauceti_tools.forge_api import _commit_tree, _tree, execute, plan
from tauceti_tools.forge_command import parse_command

EMPTY_TREE = hashlib.sha1(b"tree 0\0").hexdigest()
RAW_COMMIT = f"tree {EMPTY_TREE}\nauthor Operator <operator@example.invalid> 0 +0000\ncommitter Operator <operator@example.invalid> 0 +0000\n\nProof commit\n".encode()
COMMIT_SHA = hashlib.sha1(f"commit {len(RAW_COMMIT)}\0".encode() + RAW_COMMIT).hexdigest()


def git_object(raw, kind, revision=None):
    return {
        "sha": revision or hashlib.sha1(f"{kind} {len(raw)}\0".encode() + raw).hexdigest(),
        "size": len(raw),
        "encoding": "base64",
        "content": base64.b64encode(raw).decode(),
    }


def native_commit():
    identity = {"name": "Operator", "email": "operator@example.invalid", "date": "2026-11-07T00:00:00Z"}
    return {
        "sha": COMMIT_SHA,
        "url": "https://git.example.org/api/v1/repos/one/project/git/commits/" + COMMIT_SHA,
        "parents": [],
        "commit": {"message": "Proof commit", "tree": {"sha": COMMIT_SHA}, "author": identity, "committer": identity},
    }


class ApiContracts(unittest.TestCase):
    def setUp(self):
        self.forge = Forge("forgejo", "https://git.example.org")
        self.repo = RepositoryRef(self.forge, "one/project")
        self.api = Mock(forge=self.forge, max_pages=3, max_bytes=16 * 1024 * 1024)
        self.value = {"id": 2, "login": "operator"}

        def response(method, path, *, response_headers=None, response_meta=None):
            if response_headers is not None:
                response_headers.update({"content-type": "application/json", "etag": '"local-version"'})
            if response_meta is not None:
                response_meta.update(status=200, reason="OK", version=11)
            return self.value

        self.api.request.side_effect = response
        self.api.pages.side_effect = lambda path: iter([self.api.collection(path)])

    def request(self, *args):
        return plan(parse_command(["api", *args]), context=self.repo)

    def test_unsupported_inputs_fail_before_io(self):
        for args in [
            ["graphql", "-f", "query=query{viewer{login}}"],
            ["user", "-X", "POST"],
            ["user", "--input", "/private/body"],
            ["user", "-H", "Authorization: token other"],
            ["user", "-H", "Host: attacker.example"],
            ["user", "-F", "body=@-"],
            ["https://attacker.example/user"],
            ["repos/one/project/../secrets"],
            ["repos/one/project/contents/%2e%2e/secrets"],
            ["user?access_token=secret"],
            ["repos/one/project/labels", "--include"],
            ["user", "--paginate"],
            ["user", "--slurp"],
            ["repos/one/project/contents/A", "-H", "Accept: application/vnd.github.raw", "--jq", ".x"],
            ["repos/one/project/labels?per_page=10", "-X", "GET", "-F", "per_page=11"],
        ]:
            with self.subTest(args=args), self.assertRaises(Unsupported):
                self.request(*args)
        self.api.request.assert_not_called()

    def test_string_flag_values_do_not_select_a_host(self):
        cmd = parse_command(["api", "repos/one/project/contents/A", "-X", "GET", "-f", "ref=--hostname=github.com"])
        request = plan(cmd)
        self.assertEqual(request.parameters, (("ref", "--hostname=github.com"),))
        self.assertNotIn("hostname", cmd.options)

    def test_user_node_id_is_qualified_and_header_status_is_real(self):
        result, diagnostic, status = execute(self.request("user", "--include"), self.api)
        self.assertEqual((diagnostic, status), ("", 0))
        head, body = result.split("\n\n", 1)
        self.assertTrue(head.startswith("HTTP/1.1 200 OK\n"))
        self.assertIn('etag: "local-version"', head)
        self.assertEqual(json.loads(body)["node_id"], self.forge.key + "/users/2")

    def test_repository_identity_cannot_be_substituted(self):
        self.value = {"id": 5, "full_name": "attacker/other"}
        with self.assertRaises(Incomplete):
            execute(self.request("repos/one/project"), self.api)

    def test_repo_and_owner_ids_have_distinct_namespaces(self):
        self.value = {"id": 5, "full_name": "one/project", "owner": {"id": 5, "login": "one"}}
        result, _, _ = execute(self.request("repos/one/project"), self.api)
        row = json.loads(result)
        self.assertNotEqual(row["node_id"], row["owner"]["node_id"])
        self.assertNotIn("type", row["owner"])

    def test_owner_permission_maps_to_admin_only_for_the_requested_principal(self):
        self.value = {"permission": "owner", "user": {"id": 2, "login": "operator"}}
        result, _, _ = execute(self.request("repos/one/project/collaborators/operator/permission"), self.api)
        self.assertEqual(json.loads(result)["permission"], "admin")
        self.value["user"]["login"] = "other"
        with self.assertRaises(Incomplete):
            execute(self.request("repos/one/project/collaborators/operator/permission"), self.api)

    def test_paginated_output_keeps_page_boundaries_and_starting_page(self):
        self.api.collection.return_value = [{"id": n, "name": str(n)} for n in range(1, 6)]
        result, _, _ = execute(
            self.request("repos/one/project/labels?per_page=2&page=2", "--paginate", "--slurp"), self.api
        )
        self.assertEqual([[row["id"] for row in page] for page in json.loads(result)], [[3, 4], [5]])
        self.api.collection.assert_called_once_with("repos/one/project/labels")

    def test_api_page_bound_does_not_claim_a_complete_prefix(self):
        self.api.collection.return_value = [{"id": n} for n in range(7)]
        with self.assertRaises(Incomplete):
            execute(self.request("repos/one/project/labels?per_page=2", "--paginate"), self.api)

    def test_ref_array_is_bound_to_the_exact_requested_ref(self):
        self.value = [{"ref": "refs/heads/main-old", "object": {"sha": "a" * 40}}]
        with self.assertRaises(ForgeError) as missing:
            execute(self.request("repos/one/project/git/refs/heads/main"), self.api)
        self.assertEqual(missing.exception.status, 404)
        self.value.append({"ref": "refs/heads/main", "object": {"sha": "b" * 40}})
        result, _, _ = execute(self.request("repos/one/project/git/refs/heads/main"), self.api)
        self.assertEqual(json.loads(result)["object"]["sha"], "b" * 40)

    def test_unrelated_matching_refs_or_incomplete_git_objects_are_errors(self):
        self.value = [{"ref": "refs/heads/other", "object": {"sha": "a" * 40}}]
        with self.assertRaises(Incomplete):
            execute(self.request("repos/one/project/git/matching-refs/heads/claims"), self.api)
        self.value = {"sha": "a" * 40, "truncated": True, "tree": []}
        with self.assertRaises(Incomplete):
            execute(self.request("repos/one/project/git/trees/main?recursive=1"), self.api)

    def test_tree_pages_are_joined_only_for_one_immutable_tree(self):
        def page(method, path, **kwargs):
            if "/git/commits/" in path:
                return native_commit()
            n = int(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["page"][0])
            return {
                "sha": "a" * 40,
                "total_count": 3,
                "page": n,
                "truncated": True,
                "tree": [{"path": str(n), "sha": "b" * 40, "type": "blob", "mode": "100644"}],
            }

        self.api.request.side_effect = page
        result = _tree(self.api, "repos/one/project/git/trees/" + "a" * 40, [], {}, {})
        self.assertEqual([entry["path"] for entry in result["tree"]], ["1", "2", "3"])
        self.assertFalse(result["truncated"])

        def changed(method, path, **kwargs):
            value = page(method, path, **kwargs)
            if value.get("page") == 2:
                value["sha"] = "c" * 40
            return value

        self.api.request.side_effect = changed
        with self.assertRaises(Incomplete):
            _tree(self.api, "repos/one/project/git/trees/" + "a" * 40, [], {}, {})

    def test_missing_tree_page_or_aggregate_overflow_is_not_complete(self):
        self.api.request.return_value = {"sha": "a" * 40, "total_count": 1, "page": 1, "tree": []}
        self.api.request.side_effect = None
        with self.assertRaises(Incomplete):
            execute(self.request("repos/one/project/git/trees/main"), self.api)
        self.api.max_bytes = 2
        self.api.pages.side_effect = lambda path: iter([[{"id": 1}]])
        with self.assertRaises(Incomplete):
            execute(self.request("repos/one/project/labels", "--paginate"), self.api)

    def test_single_page_stops_before_scanning_the_collection_history(self):
        def batches(path):
            yield [{"id": 1, "name": "first"}, {"id": 2, "name": "second"}]
            raise AssertionError("a prefix read scanned past its requested page")

        self.api.pages.side_effect = batches
        result, _, _ = execute(self.request("repos/one/project/labels?per_page=2"), self.api)
        self.assertEqual([row["id"] for row in json.loads(result)], [1, 2])

    def test_commit_endpoints_have_their_distinct_github_shapes(self):
        def reply(method, path, **kwargs):
            if "/git/commits/" in path:
                return native_commit()
            if "/git/blobs/" in path:
                return git_object(RAW_COMMIT, "commit")
            selector = path.split("/git/trees/", 1)[1].split("?")[0]
            return {"sha": selector, "page": 1, "total_count": 0, "tree": []}

        self.api.request.side_effect = reply
        result, _, _ = execute(self.request("repos/one/project/git/commits/" + COMMIT_SHA), self.api)
        row = json.loads(result)
        self.assertEqual(row["message"], "Proof commit")
        self.assertEqual(row["tree"]["sha"], EMPTY_TREE)
        self.assertNotEqual(row["sha"], row["tree"]["sha"])
        result, _, _ = execute(self.request("repos/one/project/commits/main"), self.api)
        self.assertEqual(json.loads(result)["commit"]["message"], "Proof commit")
        self.assertEqual(json.loads(result)["commit"]["tree"]["sha"], EMPTY_TREE)
        with self.assertRaises(ForgeError):
            execute(self.request("repos/one/project/git/commits/" + "b" * 40), self.api)

    def test_commit_namespace_names_do_not_change_the_route(self):
        paths = []

        def reply(method, path, **kwargs):
            paths.append(path)
            return git_object(RAW_COMMIT, "commit") if "/git/blobs/" in path else native_commit()

        self.api.request.side_effect = reply
        result, _, _ = execute(self.request("repos/git/commits/commits/main"), self.api)
        self.assertEqual(json.loads(result)["sha"], COMMIT_SHA)
        self.assertEqual(paths[0], "repos/git/commits/git/commits/main")
        self.assertEqual(paths[1], "repos/git/commits/git/blobs/" + COMMIT_SHA)

    def test_raw_object_type_or_hash_mismatch_is_not_a_tree_identity(self):
        for kind in ["blob", "commit"]:

            def reply(method, path, kind=kind, **kwargs):
                if "/git/commits/" in path:
                    return native_commit()
                return git_object(b"unexpected", kind, COMMIT_SHA)

            self.api.request.side_effect = reply
            with self.subTest(kind=kind), self.assertRaises(Incomplete):
                execute(self.request("repos/one/project/git/commits/" + COMMIT_SHA), self.api)

    def test_ref_namespace_names_do_not_change_the_route(self):
        self.value = [{"ref": "refs/heads/main", "object": {"sha": "a" * 40}}]
        for name, suffix in [
            ("git/ref", "git/ref/heads/main"),
            ("git/refs", "git/refs/heads/main"),
            ("git/matching-refs", "git/matching-refs/heads/"),
        ]:
            with self.subTest(name=name):
                execute(self.request("repos/" + name + "/" + suffix), self.api)
                self.assertEqual(
                    self.api.request.call_args.args[:2],
                    ("GET", "repos/" + name + "/git/refs/heads/" + ("" if "matching-refs" in suffix else "main")),
                )

    def test_known_commit_raw_unsupported_status_is_not_reported_as_absent(self):
        def reply(method, path, **kwargs):
            if "/git/commits/" in path:
                return native_commit()
            raise ForgeError("unsupported raw object type", status=404)

        self.api.request.side_effect = reply
        with self.assertRaises(Incomplete) as incomplete:
            execute(self.request("repos/one/project/git/commits/" + COMMIT_SHA), self.api)
        self.assertEqual(incomplete.exception.status, 0)

    def test_an_actual_commit_object_cannot_be_accepted_as_a_tree(self):
        def reply(method, path, **kwargs):
            if "/git/commits/" in path:
                raise ForgeError("not a commit", status=404)
            return git_object(RAW_COMMIT, "commit")

        self.api.request.side_effect = reply
        with self.assertRaises(Incomplete):
            execute(self.request("repos/one/project/git/trees/" + COMMIT_SHA), self.api)

    def test_commit_header_parsing_rejects_wrong_order_or_hash_width(self):
        for raw, algorithm in [
            (b"parent " + b"b" * 40 + b"\ntree " + EMPTY_TREE.encode() + b"\n\nMessage\n", "sha1"),
            (RAW_COMMIT, "sha256"),
        ]:
            digest = hashlib.new(algorithm, f"commit {len(raw)}\0".encode() + raw).hexdigest()
            self.api.request.side_effect = None
            self.api.request.return_value = {
                "sha": digest,
                "size": len(raw),
                "encoding": "base64",
                "content": base64.b64encode(raw).decode(),
            }
            with self.subTest(algorithm=algorithm), self.assertRaises(Incomplete):
                _commit_tree(self.api, "repos/one/project", digest)

    def test_commit_tree_preserves_a_noncanonical_tree_object_id(self):
        # This tree writes a directory with a leading zero in its mode. The
        # commit header identifies these exact bytes, not a re-serialization.
        legacy_tree = b"040000 nested\0" + bytes.fromhex(EMPTY_TREE)
        legacy_sha = hashlib.sha1(f"tree {len(legacy_tree)}\0".encode() + legacy_tree).hexdigest()
        raw = RAW_COMMIT.replace(EMPTY_TREE.encode(), legacy_sha.encode(), 1)
        digest = hashlib.sha1(f"commit {len(raw)}\0".encode() + raw).hexdigest()
        self.api.request.side_effect = None
        self.api.request.return_value = git_object(raw, "commit")
        self.assertEqual(_commit_tree(self.api, "repos/one/project", digest)["sha"], legacy_sha)

    def test_missing_ref_namespace_requires_confirmed_code_visibility(self):
        self.api.request.side_effect = ForgeError("not found", status=404)
        self.api.repository.return_value = {"permissions": {"pull": True}}
        result, _, _ = execute(self.request("repos/one/project/git/matching-refs/heads/claims/"), self.api)
        self.assertEqual(json.loads(result), [])
        self.api.repository.return_value = {"permissions": {"pull": False}}
        with self.assertRaises(Incomplete):
            execute(self.request("repos/one/project/git/matching-refs/heads/claims/"), self.api)
        self.api.repository.side_effect = ForgeError("not found", status=404)
        with self.assertRaises(ForgeError):
            execute(self.request("repos/one/project/git/matching-refs/heads/claims/"), self.api)

    def test_json_large_blob_omission_is_not_an_empty_file(self):
        self.value = {"sha": "a" * 40, "size": 20_000_000, "encoding": "base64", "content": ""}
        for endpoint in ["git/blobs/" + "a" * 40, "contents/large.txt"]:
            self.value["type"] = "file"
            with self.subTest(endpoint=endpoint), self.assertRaises(Incomplete):
                execute(self.request("repos/one/project/" + endpoint), self.api)

    def test_recursive_presence_resolves_a_tree_before_traversal(self):
        paths = []

        def reply(method, path, **kwargs):
            paths.append(path)
            if "/git/commits/" in path:
                return native_commit()
            if "/git/blobs/" in path:
                return git_object(RAW_COMMIT, "commit")
            return {"sha": path.split("/git/trees/", 1)[1].split("?")[0], "page": 1, "total_count": 0, "tree": []}

        self.api.request.side_effect = reply
        result, _, _ = execute(self.request("repos/one/project/git/trees/main?recursive=false"), self.api)
        self.assertEqual(json.loads(result)["sha"], EMPTY_TREE)
        self.assertIn("/git/trees/" + EMPTY_TREE + "?recursive=1", paths[-1])

    def test_real_tree_object_fallback_verifies_the_tree_hash(self):
        def reply(method, path, **kwargs):
            if "/git/commits/" in path:
                raise ForgeError("not a commit", status=404)
            if "/git/blobs/" in path:
                return git_object(b"", "tree", revision=path.rsplit("/", 1)[1])
            return {"sha": path.split("/git/trees/", 1)[1].split("?")[0], "page": 1, "total_count": 0, "tree": []}

        self.api.request.side_effect = reply
        result, _, _ = execute(self.request("repos/one/project/git/trees/" + EMPTY_TREE), self.api)
        self.assertEqual(json.loads(result)["sha"], EMPTY_TREE)
        with self.assertRaises(Incomplete):
            execute(self.request("repos/one/project/git/trees/" + "a" * 40), self.api)

    def test_sha256_blob_content_is_checked_in_json_mode(self):
        content = "λ\n".encode()
        self.value = {
            "encoding": "base64",
            "content": base64.b64encode(content).decode(),
            "size": len(content),
            "sha": hashlib.sha256(f"blob {len(content)}\0".encode() + content).hexdigest(),
        }
        result, _, _ = execute(self.request("repos/one/project/git/blobs/" + self.value["sha"]), self.api)
        self.assertEqual(json.loads(result)["size"], len(content))
        self.value["content"] = base64.b64encode("μ\n".encode()).decode()
        with self.assertRaises(Incomplete):
            execute(self.request("repos/one/project/git/blobs/" + self.value["sha"]), self.api)

    def test_raw_text_preserves_unicode_and_newlines(self):
        text = "example : True := trivial\n-- λ\n"
        self.value = {
            "encoding": "base64",
            "content": base64.b64encode(text.encode()).decode(),
            "size": len(text.encode()),
            "sha": hashlib.sha1(f"blob {len(text.encode())}\0".encode() + text.encode()).hexdigest(),
        }
        result, _, _ = execute(
            self.request("repos/one/project/contents/A.lean", "-H", "Accept: application/vnd.github.raw"), self.api
        )
        self.assertEqual(result, text)
        self.value["content"] = "not base64"
        with self.assertRaises(Incomplete):
            execute(
                self.request("repos/one/project/contents/A.lean", "-H", "Accept: application/vnd.github.raw"), self.api
            )


if __name__ == "__main__":
    unittest.main()
