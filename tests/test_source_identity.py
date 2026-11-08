"""Native actor identity survives login changes but never crosses origins."""

import unittest

from tauceti_tools.forge import Forge, Incomplete, PrincipalRef
from tauceti_tools.source_identity import SourceActor


class Actors(unittest.TestCase):
    def setUp(self):
        self.forge = Forge("forgejo", "https://source.example")

    def test_login_renames_preserve_identity_and_other_origins_do_not(self):
        actor = SourceActor.from_native(self.forge, {"id": 7, "login": "before"})
        renamed = SourceActor.from_native(self.forge, {"id": 7, "login": "after"})
        self.assertEqual(actor, renamed)
        self.assertEqual(hash(actor), hash(renamed))
        other = SourceActor.from_native(Forge("forgejo", "https://other.example"), {"id": 7, "login": "before"})
        self.assertNotEqual(actor, other)

    def test_deleted_synthetic_and_noncanonical_ids_cannot_be_publishers(self):
        for value in [-2, -1, 0, True, "7", None]:
            with self.subTest(value=value), self.assertRaises(Incomplete):
                SourceActor.from_native(self.forge, {"id": value, "login": "display"})
        with self.assertRaises(ValueError):
            SourceActor(PrincipalRef(self.forge, "007"), "display")

    def test_projected_identity_requires_the_same_origin_and_native_id(self):
        actor = SourceActor.from_projection(self.forge, {"id": self.forge.key + "/users/7", "login": "display"})
        self.assertEqual(actor, SourceActor.from_native(self.forge, {"id": 7, "login": "renamed"}))
        for value in ["7", "forgejo:https://other.example/users/7", self.forge.key + "/users/007"]:
            with self.subTest(value=value), self.assertRaises(Incomplete):
                SourceActor.from_projection(self.forge, {"id": value, "login": "display"})
