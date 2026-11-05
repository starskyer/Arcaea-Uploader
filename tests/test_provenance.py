from hashlib import sha256
from importlib.resources import files
import json
import unittest


class ProvenanceTests(unittest.TestCase):
    def test_vendored_source_and_license_match_recorded_upstream(self):
        root = files("arcaea_uploader._vendor.arcaea_bundler")
        provenance = json.loads(root.joinpath("UPSTREAM.json").read_text(encoding="utf-8"))
        for name, expected in provenance["files"].items():
            self.assertEqual(sha256(root.joinpath(name).read_bytes()).hexdigest(), expected)
        self.assertIn("Copyright (c) 2024 Lost-MSth", root.joinpath("LICENSE").read_text(encoding="utf-8"))

    def test_original_media_snapshot_is_preserved(self):
        root = files("arcaea_uploader._legacy")
        origin = json.loads(root.joinpath("ORIGIN.json").read_text(encoding="utf-8"))
        self.assertEqual(sha256(root.joinpath("resize_and_preview.py").read_bytes()).hexdigest(), origin["sha256"])
