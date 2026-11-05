import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from arcaea_uploader.backgrounds import complete_inverse, learn_pairs, merge_pairs, normalize_pairs, pair_lookup
from arcaea_uploader.settings import load_settings, save_settings


class UndirectedBackgroundTests(unittest.TestCase):
    def test_one_entry_resolves_both_directions_without_suffix_inference(self):
        pairs = {"zettai": "zettai_light"}
        for bg, expected in (("zettai", "zettai_light"), ("zettai_light", "zettai")):
            with self.subTest(bg=bg):
                row, messages = {"bg": bg}, []
                complete_inverse(row, pairs, lambda *entry: messages.append(entry))
                self.assertEqual(row["bg_inverse"], expected)
                self.assertIn("对应表", messages[0][2])

    def test_reciprocal_duplicates_are_saved_once(self):
        pairs = normalize_pairs({"day": "night", "night": "day"})
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pair_lookup(pairs), {"day": "night", "night": "day"})
        self.assertEqual(normalize_pairs({"unchanged": "unchanged"}), {"unchanged": "unchanged"})

    def test_confirmed_override_replaces_learned_partner_at_either_endpoint(self):
        result = merge_pairs({"old": "shared", "other": "pair"}, {"shared": "confirmed"})
        lookup = pair_lookup(result)
        self.assertEqual(lookup["confirmed"], "shared")
        self.assertNotIn("old", lookup)
        self.assertEqual(lookup["pair"], "other")

    def test_editor_validation_rejects_one_to_many_including_reverse_collisions(self):
        for pairs in ({"a": "b", "b": "c"}, {"a": "shared", "b": "shared"}):
            with self.subTest(pairs=pairs), self.assertRaisesRegex(ValueError, "一一配对"):
                normalize_pairs(pairs)

    def test_master_conflict_does_not_silently_choose_a_partial_pair(self):
        pairs, conflicts = learn_pairs({"songs": [{"bg": "a_light", "bg_inverse": "b"},
            {"bg": "c", "bg_inverse": "b"}]})
        self.assertEqual(pairs, {})
        for bg in ("a_light", "b", "c"):
            row, messages = {"bg": bg}, []
            complete_inverse(row, pairs, lambda *entry: messages.append(entry), conflicts)
            self.assertNotIn("bg_inverse", row)
            self.assertEqual(messages[0][0], "warning")

    def test_existing_explicit_inverse_is_preserved(self):
        row = {"bg": "day", "bg_inverse": "explicit"}
        complete_inverse(row, {"day": "night"}, lambda *_: None)
        self.assertEqual(row["bg_inverse"], "explicit")

    def test_legacy_dictionary_migration_retains_conflicts_and_other_settings(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            original = json.dumps({"import": {"background_pairs": {"day": "night", "night": "day",
                "a": "shared", "b": "shared"}}, "local": {"extra": "retained"}}).encode()
            path.write_bytes(original)
            config = load_settings(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(config["import"]["background_pairs"], {"day": "night"})
            self.assertEqual(set(config["import"]["background_pair_conflicts"]), {"a", "b", "shared"})
            self.assertEqual(config["local"]["extra"], "retained")
            save_settings(path, config)
            self.assertEqual(load_settings(path), config)
