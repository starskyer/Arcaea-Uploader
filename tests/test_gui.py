import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from PIL import Image

from arcaea_uploader.gui import FIELDS, UploaderApp
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
        download = self.workspace / "server" / "latest version" / "database" / "songs" / "sayonarahatsukoi"
        download.mkdir(parents=True)
        (download / "0.aff").write_text("AudioOffset:0\n-\ntiming(0,100.00,4.00);", encoding="utf-8")
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

    def wait_for_workspace_list(self):
        deadline = time.monotonic() + 5
        while self.app.workspace_manager.loading and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertFalse(self.app.workspace_manager.loading)

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
        self.assertTrue(all(str(button["state"]) == "normal" for button in self.app.stage_buttons[:2]))
        self.assertEqual(str(self.app.stage_buttons[2]["state"]), "normal")
        report = self.workspace / "report.json"
        with patch("arcaea_uploader.gui.filedialog.asksaveasfilename", return_value=str(report)):
            self.app.export_report()
        self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["songs"][0]["id"], "sample")
        for name, contents in before.items():
            self.assertEqual(Path(name).read_bytes(), contents)
        self.app.input_var.set(str(self.workspace / "different"))
        self.assertEqual(str(self.app.export_button["state"]), "disabled")
        self.assertIn("重新检查", self.app.summary.get())

    def test_stage01_button_opens_review_and_stage02_installs_and_hands_off_task(self):
        from test_preprocess import song_metadata
        from test_publishing import MemoryUpload, preview
        from arcaea_uploader.publishing import publish_update
        self.make_package()
        (self.workspace / "incoming/sample/songlist.json").write_text(json.dumps(song_metadata("sample")), encoding="utf-8")
        for name in ("packlist", "unlocks"):
            (self.workspace / "active/songs" / name).write_text("{}", encoding="utf-8")
        history = [{"applicationVersionNumber": "7.0.256", "versionNumber": "7.0.256.4"}]
        (self.workspace / "active/metadata.oldjson").write_text(json.dumps(history), encoding="utf-8")
        self.app.variables[("ssh", "host")].set("example.test")
        self.app.variables[("ssh", "username")].set("ubuntu")
        self.app.variables[("ssh", "server_project_root")].set("/srv/server")
        with patch("arcaea_uploader.publishing.create_preview", side_effect=preview), patch.object(self.app, "open_review") as opened:
            self.app.stage_buttons[0].invoke()
            self.wait_for_check()
            opened.assert_called_once()
            self.assertIsNotNone(self.app.prepared_batch)
            batch = self.app.prepared_batch
            self.assertEqual(self.app.server_ops.task["source"]["path"], self.app.result["review_songlist"])
            def publish(*args, **kwargs):
                return publish_update(*args, **kwargs, transport_factory=MemoryUpload)
            MemoryUpload.fail_after, MemoryUpload.files = None, {}
            with patch("arcaea_uploader.gui.messagebox.askyesno", return_value=True), patch(
                    "arcaea_uploader.publishing.publish_update", side_effect=publish):
                self.app.stage_buttons[1].invoke()
                self.wait_for_check()
        self.assertIsNotNone(self.app.result, self.app.details.get("1.0", "end"))
        self.assertEqual(self.app.result["mode"], "published", self.app.details.get("1.0", "end"))
        self.assertTrue(batch.exists())
        self.assertEqual(self.app.prepared_batch, batch)
        self.assertEqual(self.app.server_ops.task["publication"]["kind"], "verified")
        self.assertIn("02 已上传", self.app.summary.get())
        self.assertTrue(all(str(button["state"]) == "normal" for button in self.app.stage_buttons))
        from test_server_tasks import FakeAPI
        self.app.variables[("api", "base_url")].set(FakeAPI.base_url)
        with patch("arcaea_uploader.server_tasks_gui.messagebox.askyesno", return_value=True), patch(
                "arcaea_uploader.server_tasks_gui.ApiClient.from_settings", return_value=FakeAPI()):
            self.app.server_ops.execute()
            self.wait_for_check()
        self.assertTrue(batch.exists())
        self.assertEqual(str(self.app.server_ops.commit_button["state"]), "normal")
        with patch("arcaea_uploader.server_tasks_gui.messagebox.askyesno", return_value=False):
            self.app.server_ops.commit()
        self.assertTrue(batch.exists())
        with patch("arcaea_uploader.server_tasks_gui.messagebox.askyesno", return_value=True):
            self.app.server_ops.commit()
            self.wait_for_check()
        self.assertFalse(batch.exists())
        self.assertIsNone(self.app.prepared_batch)
        self.assertEqual(self.app.result["mode"], "committed")

    def test_tabs_keep_identical_shape_and_padding_when_selected(self):
        style = self.app.style
        self.assertEqual(style.layout("TNotebook.Tab")[0][0], "Uploader.tab")
        self.assertEqual(str(style.lookup("TNotebook.Tab", "padding", ("selected",))),
                         str(style.lookup("TNotebook.Tab", "padding", ("!selected",))))
        self.assertEqual(str(style.lookup("TNotebook.Tab", "relief", ("selected",))), "flat")
        self.assertEqual(str(style.lookup("TNotebook.Tab", "relief", ("!selected",))), "flat")
        self.assertEqual(str(style.lookup("TNotebook.Tab", "expand", ("selected",))),
                         str(style.lookup("TNotebook.Tab", "expand", ("!selected",))))
        self.assertNotEqual(style.lookup("TNotebook.Tab", "background", ("selected",)),
                            style.lookup("TNotebook.Tab", "background", ("!selected",)))

    def test_pending_workspaces_warn_on_new_directory_and_restore_each_server_profile(self):
        from test_publishing import PublishingTests
        fixtures = []
        for host in ("one.example.test", "two.example.test"):
            fixture = PublishingTests("test_complete_two_stage_workflow_with_final_review_preview_and_task")
            fixture.setUp()
            fixtures.append(fixture)
            def cleanup(fixture=fixture):
                fixture.doCleanups()
                fixture.tearDown()
            self.addCleanup(cleanup)
            fixture.settings["ssh"]["host"] = host
            fixture.settings["api"]["base_url"] = "https://" + host + "/api/v1"
        first, second = fixtures
        first_plan, first_batch = first.prepare()
        self.app._fill(first.settings)
        with patch.object(self.app, "open_review"):
            self.app.resume_workspace(first_batch)
        self.app.server_ops.task["constants"]["validid"]["2"] = 9.8
        self.assertTrue(self.app.server_ops.save())
        self.app._fill(second.settings)
        with patch("arcaea_uploader.gui.messagebox.askyesnocancel", return_value=None) as prompt:
            self.assertFalse(self.app.choose_directory_with_pending(second.settings["local"]["input_root"]))
        prompt.assert_called_once()
        self.assertEqual(self.app.prepared_batch, first_batch)
        with patch("arcaea_uploader.gui.messagebox.askyesnocancel", return_value=False):
            self.assertTrue(self.app.choose_directory_with_pending(second.settings["local"]["input_root"]))
        self.assertIsNone(self.app.prepared_batch)
        second_plan, second_batch = second.prepare()
        self.app.close()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = UploaderApp(self.root, self.config)
        self.assertEqual(len(self.app.workspace_entries()), 2)
        with patch.object(self.app, "open_review"):
            self.app.resume_workspace(first_batch)
        self.assertEqual(self.app.variables[("ssh", "host")].get(), "one.example.test")
        self.assertEqual(self.app.server_ops.task["constants"]["validid"]["2"], 9.8)
        with patch.object(self.app, "open_review"):
            self.app.resume_workspace(second_batch)
        self.assertEqual(self.app.variables[("api", "base_url")].get(), "https://two.example.test/api/v1")
        self.app.manage_workspaces()
        self.wait_for_workspace_list()
        self.assertEqual(len(self.app.workspace_manager.tree.get_children()), 2)
        with patch.object(self.root, "state", return_value="normal"), patch(
                "arcaea_uploader.gui.messagebox.askyesnocancel", return_value=False) as prompt:
            self.app.offer_resume()
        self.assertIn("2 个未提交", prompt.call_args.args[1])

    def test_close_saves_a_constant_cell_that_has_not_yet_received_enter(self):
        from arcaea_uploader.server_tasks import load_task
        panel = self.prepare_stage03()
        path = panel.path
        entry = ttk.Entry(self.root)
        entry.insert(0, "9.9")
        panel.editor = (entry, "sample", 2)
        self.app.close()
        self.assertEqual(load_task(path)["constants"]["sample"]["2"], 9.9)

    def test_01_appends_selected_directory_and_new_batch_is_explicit(self):
        from test_preprocess import song_metadata
        from test_publishing import preview
        self.make_package()
        (self.workspace / "incoming/sample/songlist.json").write_text(json.dumps(song_metadata("sample")), encoding="utf-8")
        for name in ("packlist", "unlocks"):
            (self.workspace / "active/songs" / name).write_text("{}", encoding="utf-8")
        (self.workspace / "active/metadata.oldjson").write_text(json.dumps([
            {"applicationVersionNumber": "7.0.256", "versionNumber": "7.0.256.4"}]), encoding="utf-8")
        second = self.workspace / "another-input/second"
        second.mkdir(parents=True)
        (second / "2.aff").write_bytes(b"chart")
        (second / "base.ogg").write_bytes(b"full audio")
        Image.new("RGB", (384, 384)).save(second / "base.jpg")
        (second / "songlist").write_text(json.dumps(song_metadata("second")), encoding="utf-8")
        with patch("arcaea_uploader.publishing.create_preview", side_effect=preview), patch.object(self.app, "open_review"), patch(
                "arcaea_uploader.gui.messagebox.askyesno", return_value=True):
            self.app.stage_buttons[0].invoke()
            self.wait_for_check()
            batch = self.app.prepared_batch
            review = Path(self.app.result["review_songlist"])
            document = json.loads(review.read_text(encoding="utf-8"))
            document["songs"][0]["purchase"] = "reviewed-pack"
            review.write_text(json.dumps(document), encoding="utf-8")
            self.app.server_ops.task["constants"]["sample"]["2"] = 9.7
            self.app.input_var.set(str(second.parent))
            self.app.stage_buttons[0].invoke()
            self.wait_for_check()
            self.assertEqual(self.app.prepared_batch, batch)
            self.assertEqual(len(self.app.result["songs"]), 2, self.app.details.get("1.0", "end"))
            self.assertEqual(self.app.server_ops.task["actual"], {"sample": [2], "second": [2]})
            self.assertEqual(self.app.server_ops.task["constants"]["sample"]["2"], 9.7)
            self.assertEqual(json.loads(review.read_text(encoding="utf-8"))["songs"][0]["purchase"], "reviewed-pack")
            self.assertIn("累计 2 首", self.app.status.get())
            # Closing and selecting this workspace does not rebuild its review.
            self.app.resume_workspace(batch)
            self.assertEqual(self.app.prepared_batch, batch)
            self.assertEqual(len(self.app.result["songs"]), 2)
            self.assertEqual(self.app.server_ops.task["constants"]["sample"]["2"], 9.7)
            self.app.new_batch_button.invoke()
            self.assertIsNone(self.app.prepared_batch)
            self.assertTrue(batch.exists())
            self.app.stage_buttons[0].invoke()
            self.wait_for_check()
            self.assertNotEqual(self.app.prepared_batch, batch)
            self.assertEqual(len(self.app.result["songs"]), 1)

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

    def test_every_configuration_help_opens_without_changing_inputs_or_saving(self):
        before_file = self.config.read_bytes()
        before_values = {key: var.get() for key, var in self.app.variables.items()}
        topics = {(section, key) for _, section, fields in FIELDS for key, _, _ in fields}
        topics.update({("workflow", "stages"), ("import", "background_pairs")})
        self.assertEqual(set(self.app.help_buttons), topics)
        for topic in sorted(topics):
            with self.subTest(topic=topic):
                self.app.help_buttons[topic][-1].invoke()
                self.root.update_idletasks()
                windows = [w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)]
                self.assertEqual(windows, [self.app.help_window])
                self.assertEqual(str(self.app.help_text["state"]), "disabled")
                self.assertIn("示例", self.app.help_text.get("1.0", "end"))
        self.assertEqual(self.config.read_bytes(), before_file)
        self.assertEqual({key: var.get() for key, var in self.app.variables.items()}, before_values)
        self.assertFalse(self.app.busy)
        self.assertIsNone(self.app.result)

    def test_help_closes_and_reopens_while_retaining_configuration(self):
        def buttons(parent):
            for child in parent.winfo_children():
                if isinstance(child, ttk.Button):
                    yield child
                yield from buttons(child)

        self.app.help_buttons[("api", "token_env")][0].invoke()
        dialog = self.app.help_window
        close = next(button for button in buttons(dialog) if button["text"] == "关闭")
        close.invoke()
        self.assertFalse(dialog.winfo_exists())
        self.assertIsNone(self.app.help_window)
        self.app.help_buttons[("local", "input_root")][0].invoke()
        self.assertIn("待更新曲目目录", self.app.help_window.title())
        self.assertIn("工作副本", self.app.help_text.get("1.0", "end"))

    def test_workspace_manager_resumes_saved_batch_after_restart(self):
        from test_preprocess import song_metadata
        self.make_package()
        (self.workspace / "incoming/sample/songlist.json").write_text(
            json.dumps(song_metadata("sample")), encoding="utf-8")
        self.app.start_preprocess()
        self.wait_for_check()
        batch = self.app.result["batch_root"]
        (Path(batch) / "workspace.json").unlink()  # Older batches did not have a marker.
        self.app.close()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = UploaderApp(self.root, self.config)
        self.assertIsNone(self.app.preprocessed)
        self.app.workspace_button.invoke()
        self.wait_for_workspace_list()
        manager = self.app.workspace_manager
        item = manager.tree.get_children()[0]
        manager.tree.selection_set(item)
        manager.select()
        manager.resume_button.invoke()
        self.wait_for_check()
        self.assertTrue(self.app.result["valid"], self.app.result["errors"])
        self.assertEqual(self.app.result["batch_root"], batch)
        self.assertTrue(self.app.result["packages"][0]["reused"])
        self.app.check_button.invoke()
        self.wait_for_check()
        self.assertEqual(self.app.result["preprocessing_report"], str(Path(batch) / "report.json"))

    def test_workspace_window_is_responsive_while_directory_scan_is_pending(self):
        from threading import Event
        release = Event()
        def slow_scan(*args):
            release.wait(3)
            return []
        with patch("arcaea_uploader.workspace_gui.session_entries", side_effect=slow_scan):
            try:
                self.app.manage_workspaces()
                manager = self.app.workspace_manager
                self.assertTrue(manager.loading)
                heartbeat = []
                self.root.after(0, lambda: heartbeat.append(True))
                self.root.update()
                self.assertTrue(heartbeat, "Filesystem scanning blocked Tk events")
                self.assertIn("正在读取", manager.detail.get())
            finally:
                release.set()
            self.wait_for_workspace_list()
            self.assertFalse(manager.tree.get_children())

    def test_workspace_manual_delete_requires_click_confirmation_and_invalidates_cache(self):
        from test_preprocess import song_metadata
        self.make_package()
        (self.workspace / "incoming/sample/songlist.json").write_text(
            json.dumps(song_metadata("sample")), encoding="utf-8")
        self.app.start_preprocess()
        self.wait_for_check()
        batch = Path(self.app.result["batch_root"])
        self.app.workspace_button.invoke()
        self.wait_for_workspace_list()
        manager = self.app.workspace_manager
        manager.tree.selection_set(manager.tree.get_children()[0])
        manager.select()
        with patch("arcaea_uploader.workspace_gui.messagebox.askyesno", return_value=False):
            manager.delete_button.invoke()
        self.assertTrue(batch.exists())
        with patch("arcaea_uploader.workspace_gui.messagebox.askyesno", return_value=True):
            manager.delete_button.invoke()
        self.assertFalse(batch.exists())
        self.assertIsNone(self.app.preprocessed)
        self.assertIsNone(self.app.result)
        self.assertEqual(str(self.app.export_button["state"]), "disabled")
        self.assertTrue((self.workspace / "incoming/sample/2.aff").exists())

    def prepare_stage03(self):
        from test_preprocess import song_metadata
        self.make_package()
        metadata = song_metadata("sample")
        (self.workspace / "incoming/sample/songlist.json").write_text(json.dumps(metadata), encoding="utf-8")
        self.app.start_preprocess()
        self.wait_for_check()
        panel = self.app.server_ops
        self.assertIsNotNone(panel.task, panel.hint.get())
        self.assertFalse(panel.path.exists(), "Preprocessing should not write a 03 draft implicitly")
        source = self.workspace / "reviewed-songlist"
        source.write_text(json.dumps({"songs": [metadata]}), encoding="utf-8")
        with patch("arcaea_uploader.server_tasks_gui.filedialog.askopenfilename", return_value=str(source)):
            panel.choose_source()
        return panel

    def test_stage03_missing_constant_prompt_can_cancel_without_network(self):
        panel = self.prepare_stage03()
        with patch("arcaea_uploader.server_tasks_gui.messagebox.askyesno", return_value=False) as prompt:
            with patch("arcaea_uploader.server_tasks_gui.ApiClient.from_settings") as client:
                panel.execute()
                client.assert_not_called()
        self.assertIn("sample/2", prompt.call_args.args[1])
        self.assertFalse(self.app.busy)

    def test_inline_constant_is_saved_and_stage03_executes_against_fixture_api(self):
        from test_server_tasks import FakeAPI
        from arcaea_uploader.server_tasks import load_task
        panel = self.prepare_stage03()
        entry = ttk.Entry(self.root)
        entry.insert(0, "9.8")
        panel.editor = (entry, "sample", 2)
        self.assertTrue(panel.commit_editor())
        self.assertEqual(load_task(panel.path)["constants"]["sample"]["2"], 9.8)
        api = FakeAPI()
        with patch("arcaea_uploader.server_tasks_gui.messagebox.askyesno", return_value=True):
            with patch("arcaea_uploader.server_tasks_gui.ApiClient.from_settings", return_value=api):
                panel.execute()
                self.wait_for_check()
        self.assertEqual(panel.task["state"], "complete")
        self.assertEqual(api.songs["sample"]["charts"][2]["chart_const"], 9.8)
        self.assertEqual(api.songs["sample"]["charts"][0]["chart_const"], -1)
        before = len(api.writes())
        with patch("arcaea_uploader.server_tasks_gui.messagebox.askyesno", return_value=True):
            with patch("arcaea_uploader.server_tasks_gui.ApiClient.from_settings", return_value=api):
                panel.execute()
                self.wait_for_check()
        self.assertEqual(len(api.writes()) - before, 3, "A fresh manual confirmation must refresh uploaded resources again")


if __name__ == "__main__":
    unittest.main()
