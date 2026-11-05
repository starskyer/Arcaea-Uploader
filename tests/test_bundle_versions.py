import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from arcaea_uploader.bundle_versions import read_history, resolve_bundle_versions


class BundleVersionTests(unittest.TestCase):
    def setUp(self):
        self.history = [{"versionNumber": version, "applicationVersionNumber": "7.0.256"}
                        for version in ("7.0.256.9", "7.0.256.10", "7.0.256.1")]

    def test_follow_uses_highest_history_version_and_ignores_manual_draft(self):
        plan = resolve_bundle_versions(self.history, app_version="unused draft")
        self.assertEqual(plan.app_version, "7.0.256")
        self.assertEqual(plan.bundle_version, "7.0.256.11")
        self.assertEqual(plan.previous_version, "7.0.256.10")
        self.assertEqual(plan.filename_prefix, "7.0.256.11")
        self.assertFalse(plan.transition)

    def test_custom_version_plans_a_transition_with_a_base_json_version(self):
        plan = resolve_bundle_versions(self.history, follow_app_version=False, app_version="7.0.257")
        self.assertEqual(plan.app_version, "7.0.257")
        self.assertEqual(plan.bundle_version, "7.0.257")
        self.assertEqual(plan.previous_version, "7.0.256.10")
        self.assertEqual(plan.filename_prefix, "7.0.257.0")
        self.assertTrue(plan.transition)

    def test_no_history_requires_an_explicit_application_version(self):
        with self.assertRaisesRegex(ValueError, "首次打包"):
            resolve_bundle_versions([])
        plan = resolve_bundle_versions([], follow_app_version=False, app_version="7.0.257")
        self.assertIsNone(plan.previous_version)
        self.assertEqual(plan.filename_prefix, "7.0.257.0")

    def test_follow_after_base_transition_increments_to_one(self):
        plan = resolve_bundle_versions([{"versionNumber": "7.0.257", "applicationVersionNumber": "7.0.257"}])
        self.assertEqual(plan.bundle_version, "7.0.257.1")
        self.assertEqual(plan.filename_prefix, "7.0.257.1")

    def test_invalid_or_unchanged_custom_application_version_is_rejected(self):
        for value in ("", "7..0", "7.0.a", "../7.0", True, "7.0.256"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_bundle_versions(self.history, follow_app_version=False, app_version=value)
        with self.assertRaises(ValueError):
            resolve_bundle_versions(self.history, follow_app_version="false")

    def test_read_history_is_read_only_and_checks_bad_records(self):
        with TemporaryDirectory() as temp:
            assets = Path(temp)
            self.assertEqual(read_history(assets), [])
            self.assertEqual(list(assets.iterdir()), [])
            path = assets / "metadata.oldjson"
            raw = json.dumps(self.history).encode()
            path.write_bytes(raw)
            self.assertEqual(read_history(assets), self.history)
            self.assertEqual(path.read_bytes(), raw)
            path.write_text('[{"versionNumber":"7.0.1"}]', encoding="utf-8")
            with self.assertRaises(ValueError):
                read_history(assets)
