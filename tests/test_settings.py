import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from arcaea_uploader.planner import PlanError
from arcaea_uploader.settings import (
    default_settings, effective_settings, load_settings, save_settings,
)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_saved_settings_keep_unknown_fields_and_resolve_paths_on_import(self):
        source = self.root / "source" / "config.json"
        source.parent.mkdir()
        source.write_text(json.dumps({"local": {"assets_root": "资源包", "extra": "kept"},
                                      "ssh": {"server_project_root": "/srv/Arcaea-server"},
                                      "extension": {"custom": True}}), encoding="utf-8")
        settings = load_settings(source)
        destination = self.root / "other" / "config.json"
        save_settings(destination, settings)
        reloaded = load_settings(destination)
        self.assertEqual(reloaded["local"]["assets_root"], str(source.parent / "资源包"))
        self.assertEqual(reloaded["ssh"]["server_project_root"], "/srv/Arcaea-server")
        self.assertEqual(reloaded["local"]["extra"], "kept")
        self.assertTrue(reloaded["extension"]["custom"])

    def test_invalid_port_does_not_overwrite_existing_config(self):
        path = self.root / "config.json"
        settings = default_settings()
        save_settings(path, settings)
        before = path.read_bytes()
        for port in ("abc", 0, 65536, True, 22.5):
            with self.subTest(port=port):
                settings["ssh"]["port"] = port
                with self.assertRaises(PlanError):
                    save_settings(path, settings)
                self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".config-*")), [])

    def test_placeholder_is_derived_without_mutating_saved_values(self):
        settings = default_settings()
        settings["local"]["server_project_root"] = str(self.root / "server")
        resolved = effective_settings(settings, self.root)
        self.assertEqual(resolved["local"]["placeholder_aff"],
                         str(self.root / "server" / "latest version" / "database" / "songs" / "aletheia" / "0.aff"))
        self.assertEqual(settings["local"]["placeholder_aff"], "")

    def test_legacy_tool_paths_are_retired_without_losing_other_settings(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({"local": {"bundler_entry": "old/path", "media_script": "old/script.py",
                                  "assets_root": "active", "extra": "preserved"}}), encoding="utf-8")
        values = load_settings(path)
        save_settings(path, values)
        reloaded = load_settings(path)
        self.assertNotIn("bundler_entry", reloaded["local"])
        self.assertNotIn("media_script", reloaded["local"])
        self.assertEqual(reloaded["local"]["assets_root"], str(self.root / "active"))
        self.assertEqual(reloaded["local"]["extra"], "preserved")

    def test_ffmpeg_command_name_is_not_rewritten_as_relative_path(self):
        settings = default_settings()
        settings["local"]["ffmpeg_path"] = "ffmpeg"
        self.assertEqual(effective_settings(settings, self.root)["local"]["ffmpeg_path"], "ffmpeg")

    def test_incomplete_profile_can_be_saved_and_loaded(self):
        path = self.root / "config.json"
        save_settings(path, default_settings())
        values = load_settings(path)
        self.assertEqual(values["local"]["input_root"], "")
        self.assertEqual(values["ssh"]["port"], 22)

    def test_bundle_mode_defaults_for_older_profiles_and_round_trips_manual_choice(self):
        path = self.root / "config.json"
        path.write_text('{"local":{}}', encoding="utf-8")
        config = load_settings(path)
        self.assertEqual(config["bundle"], {"follow_app_version": True, "app_version": ""})
        config["bundle"] = {"follow_app_version": False, "app_version": "7.0.257"}
        save_settings(path, config)
        self.assertEqual(load_settings(path)["bundle"], config["bundle"])

    def test_bundle_follow_flag_requires_a_json_boolean(self):
        path = self.root / "config.json"
        path.write_text('{"bundle":{"follow_app_version":"false"}}', encoding="utf-8")
        with self.assertRaises(PlanError):
            load_settings(path)

    def test_malformed_configuration_is_rejected(self):
        path = self.root / "config.json"
        for contents in ("{broken", "[]", '{"local": null}', '{"api":{"token_env":42}}'):
            with self.subTest(contents=contents):
                path.write_text(contents, encoding="utf-8")
                with self.assertRaises(PlanError):
                    load_settings(path)


if __name__ == "__main__":
    unittest.main()
