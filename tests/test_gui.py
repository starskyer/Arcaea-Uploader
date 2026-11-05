import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from PIL import Image

from arcaea_uploader.gui import UploaderApp
from arcaea_uploader.settings import default_settings, load_settings, save_settings


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.workspace = Path(self.temp.name).resolve()
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.temp.cleanup()
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.withdraw()
        self.config = self.workspace / "config.json"
        self.settings = default_settings()
        self.settings["import"]["version"] = "7.0"
        local = self.settings["local"]
        local["input_root"] = str(self.workspace / "incoming")
        local["assets_root"] = str(self.workspace / "active")
        local["server_project_root"] = str(self.workspace / "server")
        local["release_root"] = str(self.workspace / "releases")
        save_settings(self.config, self.settings)
        self.app = UploaderApp(self.root, self.config)
        self.root.update_idletasks()

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def make_package(self):
        assets = self.workspace / "active" / "songs"
        assets.mkdir(parents=True)
        (assets / "songlist").write_text('{"songs":[]}', encoding="utf-8")
        download = self.workspace / "server" / "latest version" / "database" / "songs" / "aletheia"
        download.mkdir(parents=True)
        (download / "0.aff").write_text("placeholder", encoding="utf-8")
        song = self.workspace / "incoming" / "sample"
        song.mkdir(parents=True)
        (song / "2.aff").write_text("chart", encoding="utf-8")
        (song / "base.ogg").write_bytes(b"full audio")
        for name in ("base.jpg", "base_256.jpg"):
            Image.new("RGB", (384, 384)).save(song / name)
        (song / "songlist.json").write_text(json.dumps({"id": "sample", "audioPreview": 0,
            "audioPreviewEnd": 1000, "difficulties": [{"ratingClass": 2, "rating": 9}]}), encoding="utf-8")

    def wait_for_check(self):
        deadline = time.monotonic() + 5
        while self.app.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertFalse(self.app.busy, "Background preflight did not finish")

    def test_shared_input_and_settings_survive_restart(self):
        self.app.input_var.set(str(self.workspace / "新的曲目"))
        self.app.import_version_var.set("2026.11")
        self.app.variables[("ssh", "host")].set("192.0.2.42")
        self.app.variables[("ssh", "port")].set("2222")
        self.assertTrue(self.app.save())
        persisted = load_settings(self.config)
        self.assertEqual(persisted["local"]["input_root"], self.app.input_var.get())
        self.assertEqual(persisted["ssh"]["port"], 2222)
        self.assertEqual(persisted["import"]["version"], "2026.11")
        self.app.close()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = UploaderApp(self.root, self.config)
        self.root.update_idletasks()
        self.assertEqual(self.app.variables[("ssh", "host")].get(), "192.0.2.42")
        self.assertEqual(self.app.input_var.get(), persisted["local"]["input_root"])
        self.assertEqual(self.app.variables[("import", "version")].get(), "2026.11")

    def test_blank_import_version_reports_error_before_writing(self):
        self.make_package()
        self.app.import_version_var.set("")
        with patch("arcaea_uploader.gui.messagebox.showerror") as popup:
            self.app.start_preprocess()
        self.assertFalse(self.app.busy)
        self.assertIsNone(self.app.result)
        self.assertFalse((self.workspace / "releases").exists())
        self.assertIn("ERROR 1", self.app.log_summary.get())
        self.assertTrue(self.app.details.tag_ranges("error"))
        popup.assert_not_called()

    def test_follow_application_version_locks_entries_and_manual_toggle_preserves_draft(self):
        self.make_package()
        history = self.workspace / "active/metadata.oldjson"
        history.write_text(json.dumps([{"applicationVersionNumber": "7.0.256", "versionNumber": "7.0.256.4"}]), encoding="utf-8")
        self.app._refresh_bundle_preview()
        self.assertTrue(self.app.follow_app_version_var.get())
        for entry in self.app.app_version_entries:
            self.assertEqual(str(entry["state"]), "disabled")
            self.assertEqual(entry.get(), "7.0.256")
        self.assertIn("7.0.256.5.cb", self.app.bundle_preview.get())
        self.app.follow_app_checkbox.invoke()
        self.app.manual_app_version_var.set("7.0.257")
        for entry in self.app.app_version_entries:
            self.assertEqual(str(entry["state"]), "normal")
            self.assertEqual(entry.get(), "7.0.257")
        self.assertIn("承接包：7.0.257.0.cb", self.app.bundle_preview.get())
        self.assertTrue(self.app.save())
        config = load_settings(self.config)
        self.assertEqual(config["bundle"], {"follow_app_version": False, "app_version": "7.0.257"})
        self.app.follow_app_checkbox.invoke()
        self.assertEqual(self.app.app_version_entries[0].get(), "7.0.256")
        self.assertEqual(self.app.manual_app_version_var.get(), "7.0.257")
        self.app._fill(config)
        self.assertFalse(self.app.follow_app_version_var.get())
        self.assertEqual(self.app.app_version_entries[0].get(), "7.0.257")

    def test_invalid_manual_application_version_is_reported_without_starting_a_job(self):
        self.make_package()
        self.app.follow_app_version_var.set(False)
        self.app.manual_app_version_var.set("7.0.invalid")
        self.app.start_preprocess()
        self.assertFalse(self.app.busy)
        self.assertIsNone(self.app.result)
        self.assertFalse((self.workspace / "releases").exists())
        self.assertIn("应用版本", self.app.details.get("1.0", "end"))
        self.assertTrue(self.app.details.tag_ranges("error"))

    def test_missing_or_bad_history_does_not_leave_a_stale_detected_application_version(self):
        self.make_package()
        path = self.workspace / "active/metadata.oldjson"
        path.write_text(json.dumps([{"applicationVersionNumber": "7.0.256", "versionNumber": "7.0.256.4"}]), encoding="utf-8")
        self.app._refresh_bundle_preview()
        self.assertEqual(self.app.auto_app_version_var.get(), "7.0.256")
        path.write_text('{broken', encoding="utf-8")
        self.app._refresh_bundle_preview()
        self.assertEqual(self.app.auto_app_version_var.get(), "")
        path.unlink()
        self.app._refresh_bundle_preview()
        self.assertIn("首次打包", self.app.bundle_preview.get())
        self.app.follow_app_version_var.set(False)
        self.app.manual_app_version_var.set("7.0.257")
        self.assertIn("7.0.257.0.cb", self.app.bundle_preview.get())

    def test_preprocessing_log_renders_all_levels_and_counts(self):
        from test_preprocess import song_metadata
        self.make_package()
        (self.workspace / "incoming/sample/songlist.json").write_text(
            json.dumps(song_metadata("sample")) + ",", encoding="utf-8")
        bad = self.workspace / "incoming/bad"
        bad.mkdir()
        (bad / "2.aff").write_bytes(b"chart")
        self.app.start_preprocess()
        self.wait_for_check()
        self.assertFalse(self.app.result["valid"])
        self.assertEqual(self.app.results.index(self.app.results.select()), 2)
        for level in ("info", "warning", "error"):
            self.assertTrue(self.app.details.tag_ranges(level), level)
            count = self.app.result["diagnostic_counts"][level]
            self.assertIn(f"{level.upper()} {count}", self.app.log_summary.get())

    def test_background_editor_seeds_current_unsaved_assets_path_as_one_pair(self):
        self.make_package()
        (self.workspace / "active/songs/songlist").write_text(json.dumps({"songs": [
            {"bg": "special", "bg_inverse": "other"}]}), encoding="utf-8")
        self.app.edit_background_pairs()
        window = next(w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel))
        frame = window.winfo_children()[0]
        editor = next(w for w in frame.winfo_children() if isinstance(w, tk.Text))
        self.assertEqual(json.loads(editor.get("1.0", "end")), {"special": "other"})
        actions = next(w for w in frame.winfo_children() if w.winfo_class() == "TFrame")
        apply = next(w for w in actions.winfo_children() if w.cget("text") == "应用对应表")
        apply.invoke()
        self.assertTrue(self.app.save())
        policy = load_settings(self.config)["import"]
        self.assertEqual(policy["background_pairs"], {"special": "other"})
        self.assertTrue(policy["background_pairs_initialized"])

    def test_background_preflight_renders_and_export_is_invalidated_after_edit(self):
        self.make_package()
        before = {str(p): p.read_bytes() for p in self.workspace.rglob("*") if p.is_file()}
        self.app.check_button.invoke()
        self.wait_for_check()
        self.assertTrue(self.app.result["valid"], self.app.result["errors"])
        self.assertEqual(len(self.app.song_tree.get_children()), 1)
        self.assertGreater(len(self.app.file_tree.get_children()), 0)
        self.assertEqual(str(self.app.export_button["state"]), "normal")
        self.assertTrue(all(str(button["state"]) == "disabled" for button in self.app.stage_buttons))
        report = self.workspace / "report.json"
        with patch("arcaea_uploader.gui.filedialog.asksaveasfilename", return_value=str(report)):
            self.app.export_report()
        self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["songs"][0]["id"], "sample")
        for name, contents in before.items():
            self.assertEqual(Path(name).read_bytes(), contents)
        self.app.input_var.set(str(self.workspace / "different"))
        self.assertEqual(str(self.app.export_button["state"]), "disabled")
        self.assertIn("重新检查", self.app.summary.get())

    def test_worker_failure_leaves_window_usable_for_retry(self):
        self.app.start_check()
        self.wait_for_check()
        self.assertIsNone(self.app.result)
        self.assertEqual(str(self.app.check_button["state"]), "normal")
        self.make_package()
        self.app.start_check()
        self.wait_for_check()
        self.assertTrue(self.app.result["valid"])

    def test_invalid_port_save_preserves_last_configuration(self):
        before = self.config.read_bytes()
        self.app.variables[("ssh", "port")].set("invalid")
        with patch("arcaea_uploader.gui.messagebox.showerror") as error:
            self.assertFalse(self.app.save())
            error.assert_called_once()
        self.assertEqual(self.config.read_bytes(), before)

    def test_gui_preprocess_uses_work_copy_for_check_and_keeps_raw_directory(self):
        from test_preprocess import song_metadata
        self.make_package()
        folder = self.workspace / "incoming" / "sample"
        (folder / "songlist.json").write_text(json.dumps(song_metadata("sample")), encoding="utf-8")
        raw_input = self.app.input_var.get()
        self.app.preprocess_button.invoke()
        self.wait_for_check()
        self.assertTrue(self.app.result["valid"], self.app.result["errors"])
        self.assertEqual(self.app.result["mode"], "preprocess")
        self.assertEqual(self.app.input_var.get(), raw_input)
        self.assertTrue(Path(self.app.result["report_file"]).is_file())
        self.app.check_button.invoke()
        self.wait_for_check()
        self.assertTrue(self.app.result["valid"], self.app.result["errors"])
        self.assertIn("preprocessing_report", self.app.result)
        self.assertEqual(str(self.app.preprocess_button["state"]), "normal")

    def test_gui_preprocessing_errors_remain_visible_when_checking_successful_subset(self):
        from test_preprocess import song_metadata
        self.make_package()
        folder = self.workspace / "incoming" / "sample"
        (folder / "songlist.json").write_text(json.dumps(song_metadata("sample")), encoding="utf-8")
        bad = self.workspace / "incoming" / "bad"
        bad.mkdir()
        (bad / "2.aff").write_bytes(b"chart")
        self.app.start_preprocess()
        self.wait_for_check()
        self.assertFalse(self.app.result["valid"])
        self.app.start_check()
        self.wait_for_check()
        self.assertFalse(self.app.result["valid"])
        self.assertTrue(any(d["level"] == "error" and d["code"] == "package.invalid" for d in self.app.result["diagnostics"]))

    def test_new_preprocessing_failure_invalidates_previous_work_copy(self):
        from test_preprocess import song_metadata
        self.make_package()
        folder = self.workspace / "incoming" / "sample"
        (folder / "songlist.json").write_text(json.dumps(song_metadata("sample")), encoding="utf-8")
        self.app.start_preprocess()
        self.wait_for_check()
        self.assertIsNotNone(self.app.preprocessed)
        with patch("arcaea_uploader.gui.preprocess_settings", side_effect=ValueError("Input changed")):
            self.app.start_preprocess()
            self.wait_for_check()
        self.assertIsNone(self.app.preprocessed)
        self.assertIsNone(self.app.result)

    def test_builtin_tools_need_no_path_fields_and_license_is_accessible(self):
        self.assertNotIn(("local", "bundler_entry"), self.app.variables)
        self.assertNotIn(("local", "media_script"), self.app.variables)
        self.app.show_licenses()
        windows = [w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)]
        self.assertEqual(len(windows), 1)
        body = windows[0].winfo_children()[0]
        license_text = next(w for w in body.winfo_children() if isinstance(w, tk.Text)).get("1.0", "end")
        self.assertIn("Copyright (c) 2024 Lost-MSth", license_text)
        self.assertIn("Permission is hereby granted", license_text)


if __name__ == "__main__":
    unittest.main()
