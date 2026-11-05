import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from arcaea_uploader.bundler import build_bundle, build_configured_bundle, validate_bundle


class BundlerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.assets = self.root / "完整 assets"
        (self.assets / "songs" / "dl_example").mkdir(parents=True)
        for name in ("songlist", "packlist", "unlocks"):
            (self.assets / "songs" / name).write_text("{}", encoding="utf-8")
        self.audio = self.assets / "songs" / "dl_example" / "preview.ogg"
        self.audio.write_bytes(b"original preview")

    def tearDown(self):
        self.temp.cleanup()

    def test_initial_and_incremental_build_use_internal_core_and_preserve_history(self):
        first = build_bundle(self.assets, self.root / "build",
                             app_version="6.13.10", bundle_version="6.13.10.0")
        self.assertEqual(first.bundle.name, "6.13.10.0.cb")
        self.assertEqual(first.metadata.name, "6.13.10.0.json")
        self.assertEqual(len(validate_bundle(first.bundle, first.metadata)["added"]), 4)
        self.audio.write_bytes(b"changed preview")
        second = build_bundle(self.assets, self.root / "build")
        data = validate_bundle(second.bundle, second.metadata)
        self.assertEqual(data["previousVersionNumber"], "6.13.10.0")
        self.assertEqual(second.version, "6.13.10.1")
        self.assertEqual(second.bundle.name, data["versionNumber"] + ".cb")
        self.assertEqual(second.metadata.name, data["versionNumber"] + ".json")
        self.assertEqual([e["path"] for e in data["added"]], ["songs/dl_example/preview.ogg"])
        self.assertEqual(data["removed"], ["songs/dl_example/preview.ogg"])
        self.assertEqual(len(json.loads((self.assets / "metadata.oldjson").read_text())), 2)
        # Handles are closed, so Windows allows immediate moves.
        second.bundle.rename(second.bundle.with_suffix(".moved"))

    def test_existing_metadata_is_not_overwritten_or_paired_with_new_empty_cb(self):
        destination = self.root / "release"
        destination.mkdir()
        existing = destination / "1.0.0.json"
        existing.write_bytes(b"keep me")
        with self.assertRaises(FileExistsError):
            build_bundle(self.assets, destination, app_version="1.0", bundle_version="1.0.0")
        self.assertEqual(existing.read_bytes(), b"keep me")
        self.assertFalse((destination / "1.0.0.cb").exists())
        self.assertEqual(list(destination.glob(".bundle-*")), [])
        self.assertFalse((self.assets / "metadata.oldjson").exists())

    def test_output_inside_assets_is_rejected_before_build(self):
        with self.assertRaises(ValueError):
            build_bundle(self.assets, self.assets / "output", app_version="1.0", bundle_version="1.0.0")
        self.assertFalse((self.assets / "output").exists())

    def test_first_build_requires_versions(self):
        with self.assertRaises(ValueError):
            build_bundle(self.assets, self.root / "release")
        self.assertFalse((self.root / "release").exists())

    def test_empty_history_still_requires_initial_versions(self):
        history = self.assets / "metadata.oldjson"
        history.write_text("[]", encoding="utf-8")
        with self.assertRaises(ValueError):
            build_bundle(self.assets, self.root / "release")
        self.assertEqual(history.read_text(encoding="utf-8"), "[]")
        self.assertFalse((self.root / "release").exists())

    def test_base_application_transition_uses_zero_suffix_without_changing_json_version(self):
        result = build_bundle(self.assets, self.root / "build", app_version="7.0.256", bundle_version="7.0.256")
        self.assertEqual(result.bundle.name, "7.0.256.0.cb")
        self.assertEqual(result.metadata.name, "7.0.256.0.json")
        self.assertEqual(validate_bundle(result.bundle, result.metadata)["versionNumber"], "7.0.256")
        next_result = build_bundle(self.assets, self.root / "build")
        self.assertEqual(next_result.bundle.name, "7.0.256.1.cb")
        self.assertEqual(next_result.version, "7.0.256.1")

    def test_configured_transition_preserves_chain_and_then_resumes_automatic_updates(self):
        output = self.root / "build"
        old = build_bundle(self.assets, output, app_version="7.0.256", bundle_version="7.0.256.4")
        transition = build_configured_bundle(self.assets, output, follow_app_version=False, app_version="7.0.257")
        data = validate_bundle(transition.bundle, transition.metadata)
        self.assertEqual(transition.bundle.name, "7.0.257.0.cb")
        self.assertEqual(transition.metadata.name, "7.0.257.0.json")
        self.assertEqual(data["applicationVersionNumber"], "7.0.257")
        self.assertEqual(data["versionNumber"], "7.0.257")
        self.assertEqual(data["previousVersionNumber"], old.version)
        self.assertEqual(data["added"], [])  # Keep the existing resource baseline.
        following = build_configured_bundle(self.assets, output, app_version="unused manual draft")
        data = validate_bundle(following.bundle, following.metadata)
        self.assertEqual(following.bundle.name, "7.0.257.1.cb")
        self.assertEqual(data["previousVersionNumber"], "7.0.257")
        self.assertEqual(following.app_version, "7.0.257")

    def test_first_configured_build_needs_only_the_manual_application_version(self):
        result = build_configured_bundle(self.assets, self.root / "build", follow_app_version=False, app_version="7.0.257")
        self.assertEqual(result.bundle.name, "7.0.257.0.cb")
        self.assertIsNone(validate_bundle(result.bundle, result.metadata)["previousVersionNumber"])

    def test_transition_filename_collision_does_not_advance_history(self):
        output = self.root / "build"
        build_bundle(self.assets, output, app_version="7.0.256", bundle_version="7.0.256.4")
        history = self.assets / "metadata.oldjson"
        before = history.read_bytes()
        target = output / "7.0.257.0.json"
        target.write_bytes(b"keep existing transition")
        with self.assertRaises(FileExistsError):
            build_configured_bundle(self.assets, output, follow_app_version=False, app_version="7.0.257")
        self.assertEqual(history.read_bytes(), before)
        self.assertEqual(target.read_bytes(), b"keep existing transition")
        self.assertFalse(target.with_suffix(".cb").exists())

    def test_auto_filename_uses_highest_history_version_rather_than_last_record(self):
        history = self.assets / "metadata.oldjson"
        history.write_text(json.dumps([{"applicationVersionNumber": "7.0.256", "versionNumber": version}
            for version in ("7.0.256.9", "7.0.256.10", "7.0.256.1")]), encoding="utf-8")
        result = build_bundle(self.assets, self.root / "build")
        self.assertEqual(result.bundle.name, "7.0.256.11.cb")
        self.assertEqual(validate_bundle(result.bundle, result.metadata)["previousVersionNumber"], "7.0.256.10")

    def test_cb_collision_does_not_advance_history(self):
        first = build_bundle(self.assets, self.root / "build", app_version="1.0", bundle_version="1.0")
        history = self.assets / "metadata.oldjson"
        before = history.read_bytes()
        target = first.bundle.parent / "1.0.1.cb"
        target.write_bytes(b"existing release")
        with self.assertRaises(FileExistsError):
            build_bundle(self.assets, first.bundle.parent)
        self.assertEqual(history.read_bytes(), before)
        self.assertEqual(target.read_bytes(), b"existing release")
        self.assertFalse(target.with_suffix(".json").exists())

    def test_generated_data_corruption_is_detected(self):
        result = build_bundle(self.assets, self.root / "release", app_version="1.0", bundle_version="1.0.0")
        raw = bytearray(result.bundle.read_bytes())
        raw[-1] ^= 1
        result.bundle.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            validate_bundle(result.bundle, result.metadata)
