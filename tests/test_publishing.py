import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image

from arcaea_uploader.bundler import validate_bundle
from arcaea_uploader.planner import PlanError, digest, read_json
from arcaea_uploader.publishing import prepare_update, publish_update, tree_hashes
from arcaea_uploader.server_tasks import load_task, save_task
from arcaea_uploader.settings import default_settings


EMPTY = "AudioOffset:0\n-\ntiming(0,100.00,4.00);"
REAL = "AudioOffset:0\n-\ntiming(0,100.00,4.00);\n(100,1);"


def preview(source, target, song, *, rating_class=None, **kwargs):
    from arcaea_uploader.media import preview_interval
    start, end = preview_interval(song, rating_class)
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    Path(target).write_bytes(f"preview {start} {end}".encode())


class MemoryUpload:
    files = {}
    fail_after = None
    connections = 0

    def __init__(self, settings, **kwargs):
        self.settings = settings

    def __enter__(self):
        type(self).connections += 1
        return self

    def __exit__(self, *_):
        pass

    def publish(self, batch_id, entries, on_log=None):
        for i, entry in enumerate(entries):
            if type(self).fail_after == i:
                raise OSError("synthetic disconnected upload")
            type(self).files[entry["path"]] = Path(entry["source"]).read_bytes()


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.settings = default_settings()
        self.settings["import"]["version"] = "7.1"
        self.settings["import"]["background_pairs_initialized"] = True
        local = self.settings["local"]
        for key, name in (("input_root", "incoming"), ("assets_root", "active"),
                          ("server_project_root", "server"), ("release_root", "releases")):
            local[key] = str(self.root / name)
        self.assets = self.root / "active"
        self.database = self.root / "server/latest version/database"
        (self.assets / "songs").mkdir(parents=True)
        for name, value in (("songlist", {"songs": []}), ("packlist", {"packs": []}), ("unlocks", {"unlocks": []})):
            (self.assets / "songs" / name).write_text(json.dumps(value), encoding="utf-8")
        self.database.mkdir(parents=True)
        placeholder = self.database / "songs/sayonarahatsukoi/0.aff"
        placeholder.parent.mkdir(parents=True)
        placeholder.write_text(EMPTY, encoding="utf-8")
        self.package = self.root / "incoming/Display name/validid"
        self.package.mkdir(parents=True)
        (self.package / "2.aff").write_text(REAL, encoding="utf-8")
        (self.package / "base.ogg").write_bytes(b"full audio fixture")
        Image.new("RGB", (512, 512), "#406090").save(self.package / "base.jpg")
        self.row = {"id": "validid", "title_localized": {"en": "A song"}, "set": "newpack",
                    "artist": "Fixture", "bpm": "120", "bpm_base": 120, "side": 0, "bg": "base_light", "date": 1700000000,
                    "purchase": "validid", "audioPreview": 0, "audioPreviewEnd": 1000,
                    "difficulties": [{"ratingClass": 0, "rating": -1}, {"ratingClass": 1, "rating": 0},
                                     {"ratingClass": 2, "rating": 9}]}
        (self.package / "slst.txt").write_text(json.dumps(self.row), encoding="utf-8")
        (self.assets / "metadata.oldjson").write_text(json.dumps([
            {"applicationVersionNumber": "7.0.256", "versionNumber": "7.0.256.4"}]), encoding="utf-8")
        self.settings["ssh"].update(host="example.test", username="ubuntu", server_project_root="/srv/server")
        self.media = patch("arcaea_uploader.publishing.create_preview", side_effect=preview)
        self.media.start()
        self.addCleanup(self.media.stop)
        MemoryUpload.files, MemoryUpload.fail_after, MemoryUpload.connections = {}, None, 0

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self):
        plan = prepare_update(self.settings, self.root)
        self.assertTrue(plan["valid"], plan["errors"])
        return plan, Path(plan["batch_root"])

    def publish(self, batch):
        return publish_update(self.settings, self.root, batch, transport_factory=MemoryUpload)

    def test_complete_two_stage_workflow_with_final_review_preview_and_task(self):
        original_assets, original_database = tree_hashes(self.assets), tree_hashes(self.database)
        raw = tree_hashes(self.root / "incoming")
        plan, batch = self.prepare()
        self.assertEqual(tree_hashes(self.assets), original_assets)
        self.assertEqual(tree_hashes(self.database), original_database)
        self.assertEqual(tree_hashes(self.root / "incoming"), raw)
        copied = batch / "prepared/database/songs/validid"
        self.assertEqual({p.name for p in copied.iterdir()}, {"0.aff", "1.aff", "2.aff", "base.ogg"})
        jacket = batch / "prepared/active/songs/dl_validid/1080_base_256.jpg"
        with Image.open(jacket) as image:
            self.assertEqual(image.size, (384, 384))
        review = Path(plan["review_songlist"])
        document = read_json(review)
        document["songs"][0].update(purchase="newpack", audioPreview=200, audioPreviewEnd=900)
        review.write_text(json.dumps(document), encoding="utf-8")
        # The user can update remaining documents in the original active.
        (self.assets / "songs/packlist").write_text('{"packs":[{"id":"newpack"}]}', encoding="utf-8")
        result = self.publish(batch)
        self.assertTrue(batch.exists(), "02 must retain the complete workspace until final 03 commit")
        self.assertEqual(result["version"], "7.0.256.5")
        self.assertTrue(result["receipt"]["remote_verified"])
        task = load_task(result["server_task_file"])
        self.assertEqual(task["songs"][0]["purchase"], "newpack")
        self.assertEqual(task["actual"], {"validid": [2]})
        self.assertEqual(task["publication"]["kind"], "verified")
        self.assertEqual((self.assets / "songs/dl_validid/preview.ogg").read_bytes(), b"preview 200 900")
        self.assertEqual(read_json(self.assets / "songs/songlist"), document)
        self.assertEqual(len(read_json(self.assets / "metadata.oldjson")), 2)
        self.assertIn("bundle/7.0.256.5.json", MemoryUpload.files)
        self.assertNotIn("songs/validid/songlist", MemoryUpload.files)
        self.assertEqual(tree_hashes(self.root / "incoming"), raw)
        from arcaea_uploader.server_tasks import run_task
        from arcaea_uploader.workspaces import commit_workspace
        from test_server_tasks import FakeAPI
        run_task(task, FakeAPI(), allow_missing=True, persist=lambda task: save_task(result["server_task_file"], task))
        self.assertTrue(batch.exists(), "03 success still requires explicit commit")
        history = commit_workspace(self.root / "releases", batch)
        self.assertFalse(batch.exists())
        self.assertTrue(read_json(history)["cleanup_complete"])

    def test_upload_failure_resumes_same_build_and_retains_edited_constants(self):
        plan, batch = self.prepare()
        from arcaea_uploader.server_tasks import make_task, task_path
        path = task_path(self.root / "releases", batch.name)
        task = make_task(batch.name, read_json(Path(plan["review_songlist"])), {"validid": [2]}, load_task(path))
        task["constants"]["validid"]["2"] = 9.7
        save_task(path, task)
        MemoryUpload.fail_after = 2
        with self.assertRaises(OSError):
            self.publish(batch)
        record = read_json(batch / "publish.json")
        self.assertEqual(record["state"], "local_verified")
        before = tree_hashes(batch / "bundle")
        self.assertEqual(len(read_json(self.assets / "metadata.oldjson")), 2)
        MemoryUpload.fail_after = None
        with patch("arcaea_uploader.publishing.build_configured_bundle", side_effect=AssertionError("must reuse")):
            result = self.publish(batch)
        self.assertEqual(result["version"], "7.0.256.5")
        self.assertEqual(len(read_json(self.assets / "metadata.oldjson")), 2)
        self.assertEqual(load_task(path)["constants"]["validid"]["2"], 9.7)
        self.assertEqual({p.name: digest(p) for p in (self.database / "bundle").iterdir()}, before)

    def test_retry_does_not_treat_newly_learned_background_pairs_as_config_drift(self):
        self.settings["import"]["background_pairs_initialized"] = False
        _, batch = self.prepare()
        MemoryUpload.fail_after = 0
        with self.assertRaises(OSError):
            self.publish(batch)
        MemoryUpload.fail_after = None
        self.assertEqual(self.publish(batch)["version"], "7.0.256.5")

    def test_invalid_review_and_blank_host_fail_before_live_changes(self):
        plan, batch = self.prepare()
        original = tree_hashes(self.assets)
        path = Path(plan["review_songlist"])
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(PlanError):
            self.publish(batch)
        self.assertEqual(tree_hashes(self.assets), original)
        self.settings["ssh"]["host"] = ""
        with self.assertRaisesRegex(PlanError, "SSH"):
            self.publish(batch)
        self.assertEqual(MemoryUpload.connections, 0)

    def test_partial_local_install_and_leftover_part_resume_without_new_version(self):
        _, batch = self.prepare()
        from arcaea_uploader import publishing
        original_install = publishing._install
        def interrupted(entries, roots, batch, callback):
            first = next(e for e in entries if e["path"] == "metadata.oldjson")
            original_install([first], roots, batch, callback)
            target = self.assets / "songs/songlist"
            temporary = target.with_name("." + target.name + "." + batch.name + ".part")
            temporary.write_bytes(b"interrupted local copy")
            raise OSError("local installation interrupted")
        with patch("arcaea_uploader.publishing._install", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.publish(batch)
        self.assertEqual(len(read_json(self.assets / "metadata.oldjson")), 2)
        with patch("arcaea_uploader.publishing.build_configured_bundle", side_effect=AssertionError("must reuse build")):
            result = self.publish(batch)
        self.assertEqual(result["version"], "7.0.256.5")
        self.assertFalse(list(self.assets.rglob("*.part")))

    def test_review_changed_while_preview_is_rendering_aborts_before_build(self):
        plan, batch = self.prepare()
        path = Path(plan["review_songlist"])
        document = read_json(path)
        document["songs"][0]["audioPreviewEnd"] = 900
        path.write_text(json.dumps(document), encoding="utf-8")
        def edit_during_preview(*args, **kwargs):
            preview(*args, **kwargs)
            changed = read_json(path)
            changed["songs"][0]["purchase"] = "newpack"
            path.write_text(json.dumps(changed), encoding="utf-8")
        with patch("arcaea_uploader.publishing.create_preview", side_effect=edit_during_preview):
            with self.assertRaisesRegex(PlanError, "审核总表发生变化"):
                self.publish(batch)
        self.assertFalse((batch / "bundle").exists())
        self.assertEqual(MemoryUpload.connections, 0)
        self.assertEqual(self.publish(batch)["version"], "7.0.256.5")

    def test_independently_modified_packlist_copies_require_manual_resolution(self):
        _, batch = self.prepare()
        (self.assets / "songs/packlist").write_text('{"packs":[{"id":"one"}]}', encoding="utf-8")
        (batch / "prepared/active/songs/packlist").write_text('{"packs":[{"id":"two"}]}', encoding="utf-8")
        with self.assertRaisesRegex(PlanError, "同时修改"):
            self.publish(batch)
        self.assertEqual(MemoryUpload.connections, 0)

    def test_prepared_draft_is_preserved_when_repreparing_same_batch(self):
        plan, batch = self.prepare()
        path = Path(plan["review_songlist"])
        document = read_json(path)
        document["songs"][0]["title_localized"]["en"] = "Human review"
        path.write_text(json.dumps(document), encoding="utf-8")
        updated = prepare_update(self.settings, self.root, resume_from=batch)
        self.assertTrue(updated["valid"])
        self.assertEqual(read_json(path)["songs"][0]["title_localized"]["en"], "Human review")
        archives = list(batch.glob("additions/import-*/before/songlist"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(read_json(archives[0])["songs"][0]["title_localized"]["en"], "Human review")

    def test_source_baseline_and_workcopy_drift_block_publication(self):
        for where in ("input", "active", "prepared", "normalized"):
            with self.subTest(where=where):
                plan, batch = self.prepare()
                path = {"input": self.package / "2.aff", "active": self.assets / "songs/songlist",
                        "prepared": batch / "prepared/database/songs/validid/2.aff",
                        "normalized": batch / "songs/validid/2.aff"}[where]
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                with self.assertRaises(PlanError):
                    self.publish(batch)
                path.write_bytes(original)
        self.assertEqual(MemoryUpload.connections, 0)

    def test_error_batch_never_prepares_successful_subset(self):
        bad = self.root / "incoming/bad"
        bad.mkdir()
        (bad / "2.aff").write_text(REAL, encoding="utf-8")
        plan = prepare_update(self.settings, self.root)
        self.assertFalse(plan["valid"])
        self.assertFalse((Path(plan["batch_root"]) / "publish.json").exists())
        self.assertFalse((self.assets / "songs/dl_validid").exists())

    def test_incremental_addition_preserves_existing_real_chart_and_replaces_placeholder(self):
        original = dict(self.row, idx=0)
        (self.assets / "songs/songlist").write_text(json.dumps({"songs": [original]}), encoding="utf-8")
        existing = self.database / "songs/validid"
        existing.mkdir(parents=True)
        for rating in range(3):
            (existing / f"{rating}.aff").write_text(REAL if rating == 2 else EMPTY, encoding="utf-8")
        (existing / "base.ogg").write_bytes(b"full audio fixture")
        jackets = self.assets / "songs/dl_validid"
        jackets.mkdir()
        for name in ("1080_base.jpg", "1080_base_256.jpg"):
            Image.new("RGB", (384, 384)).save(jackets / name)
        (jackets / "preview.ogg").write_bytes(b"old preview")
        for file in self.package.iterdir():
            if file.name != "slst.txt":
                file.unlink()
        (self.package / "1.aff").write_text(REAL + "\n(200,2);", encoding="utf-8")
        update = dict(self.row, difficulties=[{"ratingClass": 1, "rating": 7}])
        (self.package / "slst.txt").write_text(json.dumps(update), encoding="utf-8")
        plan, batch = self.prepare()
        reviewed = read_json(Path(plan["review_songlist"]))["songs"][0]
        # Conflicting old placeholder metadata remains human-reviewable.
        next(d for d in reviewed["difficulties"] if d["ratingClass"] == 1)["rating"] = 7
        Path(plan["review_songlist"]).write_text(json.dumps({"songs": [reviewed]}), encoding="utf-8")
        result = self.publish(batch)
        self.assertEqual((existing / "2.aff").read_text(encoding="utf-8"), REAL)
        self.assertIn("(200,2)", (existing / "1.aff").read_text(encoding="utf-8"))
        self.assertEqual((jackets / "preview.ogg").read_bytes(), b"old preview")
        self.assertEqual(load_task(result["server_task_file"])["actual"]["validid"], [1, 2])

    def test_failed_build_recovers_private_history_and_keeps_live_history(self):
        _, batch = self.prepare()
        def fail(stage, output, **kwargs):
            history = read_json(stage / "metadata.oldjson")
            history.append({"applicationVersionNumber": "7.0.256", "versionNumber": "7.0.256.5"})
            (stage / "metadata.oldjson").write_text(json.dumps(history), encoding="utf-8")
            output.mkdir()
            (output / "partial.cb").write_bytes(b"partial")
            raise OSError("build stopped")
        with patch("arcaea_uploader.publishing.build_configured_bundle", side_effect=fail):
            with self.assertRaises(OSError):
                self.publish(batch)
        self.assertEqual(len(read_json(self.assets / "metadata.oldjson")), 1)
        result = self.publish(batch)
        self.assertEqual(result["version"], "7.0.256.5")

    def test_unknown_background_size_blocks_and_application_transition_names_dot_zero(self):
        background = self.package / "custom.jpg"
        Image.new("RGB", (1920, 1200)).save(background)
        blocked = prepare_update(self.settings, self.root)
        self.assertFalse(blocked["valid"])
        background.unlink()
        self.settings["bundle"] = {"follow_app_version": False, "app_version": "7.0.257"}
        _, batch = self.prepare()
        result = self.publish(batch)
        self.assertIn("bundle/7.0.257.0.json", MemoryUpload.files)
        self.assertEqual(result["version"], "7.0.257")

    def test_actual_ffmpeg_preparation_and_bundle_hash_validation(self):
        ffmpeg = os.environ.get("ARCAEA_UPLOADER_TEST_FFMPEG") or shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("FFmpeg unavailable")
        self.media.stop()
        self.settings["local"]["ffmpeg_path"] = ffmpeg
        subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                        "-c:a", "libvorbis", "-y", str(self.package / "base.ogg")], check=True,
                       capture_output=True)
        _, batch = self.prepare()
        MemoryUpload.fail_after = 0
        with self.assertRaises(OSError):
            self.publish(batch)
        record = read_json(batch / "publish.json")
        validate_bundle(batch / record["build"]["bundle"], batch / record["build"]["metadata"])
        preview_file = self.assets / "songs/dl_validid/preview.ogg"
        subprocess.run([ffmpeg, "-v", "error", "-i", str(preview_file), "-f", "null", "-"],
                       check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
