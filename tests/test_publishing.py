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

    def test_free_active_edits_deletions_manual_preview_and_retry_install_exact_snapshot(self):
        from base64 import b64encode
        from hashlib import sha256
        old_files = {"char/old.txt": b"delete me", "img/changed.bin": b"old image",
                     "folder_to_file/old.txt": b"old folder", "file_to_folder": b"old file",
                     "rename/source.txt": b"rename me"}
        for relative, data in old_files.items():
            target = self.assets / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        import stat
        (self.assets / "char/old.txt").chmod(stat.S_IREAD)
        history = read_json(self.assets / "metadata.oldjson")
        history[-1]["pathToHash"] = {path: b64encode(sha256(data).digest()).decode() for path, data in old_files.items()}
        (self.assets / "metadata.oldjson").write_text(json.dumps(history), encoding="utf-8")
        plan, batch = self.prepare()
        stage = Path(plan["prepared_assets"])
        (stage / "char/old.txt").unlink()
        (stage / "char/new.bin").write_bytes(b"new resource")
        (stage / "img/changed.bin").write_bytes(b"reviewed image")
        (stage / "folder_to_file/old.txt").unlink()
        (stage / "folder_to_file").rmdir()
        (stage / "folder_to_file").write_bytes(b"new file")
        (stage / "file_to_folder").unlink()
        (stage / "file_to_folder").mkdir()
        (stage / "file_to_folder/child.bin").write_bytes(b"new child")
        (stage / "rename/source.txt").rename(stage / "rename/destination.txt")
        (stage / "songs/dl_validid/preview.ogg").write_bytes(b"manual silent preview")
        logs = []
        MemoryUpload.fail_after = 0
        with self.assertRaises(OSError):
            publish_update(self.settings, self.root, batch, transport_factory=MemoryUpload, on_diagnostic=logs.append)
        self.assertEqual((self.assets / "songs/dl_validid/preview.ogg").read_bytes(), b"manual silent preview")
        self.assertEqual(tree_hashes(self.assets), tree_hashes(stage))
        for relative, data in old_files.items():
            if relative != "rename/source.txt" or not (self.assets / relative).is_file():
                self.assertEqual((batch / "backups/assets" / relative).read_bytes(), data)
        self.assertTrue(any("删除 active/char/old.txt" in entry["message"] for entry in logs))
        record = read_json(batch / "publish.json")
        self.assertTrue(any(e["path"] == "char/old.txt" and e["action"] == "remove" for e in record["review_changes"]))
        self.assertIn("char/old.txt", validate_bundle(batch / record["build"]["bundle"], batch / record["build"]["metadata"])["removed"])
        MemoryUpload.fail_after = None
        with patch("arcaea_uploader.publishing.build_configured_bundle", side_effect=AssertionError("reuse build")):
            self.assertEqual(self.publish(batch)["version"], "7.0.256.5")
        self.assertEqual(tree_hashes(self.assets), tree_hashes(stage))

    def test_active_snapshot_is_frozen_after_build(self):
        _, batch = self.prepare()
        MemoryUpload.fail_after = 0
        with self.assertRaises(OSError):
            self.publish(batch)
        (batch / "prepared/active/extra.bin").write_bytes(b"too late")
        with self.assertRaisesRegex(PlanError, "资源发生变化"):
            self.publish(batch)

    def test_resource_saved_during_build_is_not_silently_adopted(self):
        from arcaea_uploader.publishing import build_configured_bundle
        _, batch = self.prepare()
        original = tree_hashes(self.assets)
        def change_during_build(stage, output, **options):
            result = build_configured_bundle(stage, output, **options)
            (stage / "late-resource.bin").write_bytes(b"changed while building")
            return result
        with patch("arcaea_uploader.publishing.build_configured_bundle", side_effect=change_during_build):
            with self.assertRaisesRegex(PlanError, "资源发生变化"):
                self.publish(batch)
        self.assertEqual(tree_hashes(self.assets), original)
        self.assertEqual(MemoryUpload.connections, 0)

    def test_retry_does_not_treat_newly_learned_background_pairs_as_config_drift(self):
        self.settings["import"]["background_pairs_initialized"] = False
        _, batch = self.prepare()
        MemoryUpload.fail_after = 0
        with self.assertRaises(OSError):
            self.publish(batch)
        MemoryUpload.fail_after = None
        self.assertEqual(self.publish(batch)["version"], "7.0.256.5")

    def test_config_drift_reports_fields_and_workspace_restore_preserves_resource_edits(self):
        from arcaea_uploader.workspaces import workspace_settings
        plan, batch = self.prepare()
        stage = Path(plan["prepared_assets"])
        preview_path = stage / "songs/dl_validid/preview.ogg"
        preview_path.write_bytes(b"manual silent preview")
        (stage / "songs/packlist").write_text('{"packs":[{"id":"newpack"}]}', encoding="utf-8")
        cover = stage / "songs/pack/1080_select_newpack.png"
        cover.parent.mkdir(parents=True)
        cover.write_bytes(b"reviewed pack cover")
        original, reviewed = tree_hashes(self.assets), tree_hashes(stage)
        self.settings["import"]["version"] = "8.0"
        self.settings["import"]["background_pairs"]["other_light"] = "other_conflict"
        self.settings["local"]["input_root"] = str(self.root / "another-input")
        with patch("arcaea_uploader.publishing.build_configured_bundle") as build:
            with self.assertRaises(PlanError) as caught:
                self.publish(batch)
            build.assert_not_called()
        message = str(caught.exception)
        for field in ("import.background_pairs", "local.input_root"):
            self.assertIn(field, message)
        self.assertNotIn("import.version", message)
        self.assertIn("other_light", message)
        self.assertIn("继续所选批次", message)
        self.assertEqual(MemoryUpload.connections, 0)
        self.assertEqual(tree_hashes(self.assets), original)
        self.assertEqual(tree_hashes(stage), reviewed)
        self.settings = workspace_settings(batch.parent, batch, self.settings, self.root)
        self.assertEqual(self.publish(batch)["version"], "7.0.256.5")
        self.assertEqual((self.assets / "songs/dl_validid/preview.ogg").read_bytes(), b"manual silent preview")
        self.assertEqual((self.assets / "songs/pack/1080_select_newpack.png").read_bytes(), b"reviewed pack cover")
        self.assertEqual(read_json(self.assets / "songs/packlist"), {"packs": [{"id": "newpack"}]})

    def test_reviewed_song_version_can_differ_from_the_import_default(self):
        plan, batch = self.prepare()
        path = Path(plan["review_songlist"])
        document = read_json(path)
        document["songs"][0]["version"] = "7.2"
        path.write_text(json.dumps(document), encoding="utf-8")
        logs = []
        publish_update(self.settings, self.root, batch, transport_factory=MemoryUpload, on_diagnostic=logs.append)
        self.assertEqual(read_json(self.assets / "songs/songlist")["songs"][0]["version"], "7.2")
        self.assertTrue(any("保留人工审核 version" in entry["message"] for entry in logs))

    def test_changed_import_default_preserves_mixed_review_versions_and_resumes_same_build(self):
        from arcaea_uploader.workspaces import workspace_settings
        # Exercise implicit background initialization together with a corrected
        # version: restoring the inferred policy must retain that correction.
        self.settings["import"]["background_pairs_initialized"] = False
        second = self.package.parent / "second"
        shutil.copytree(self.package, second)
        (second / "slst.txt").write_text(json.dumps(dict(self.row, id="second", purchase="second")), encoding="utf-8")
        plan, batch = self.prepare()
        review = Path(plan["review_songlist"])
        document = read_json(review)
        rows = {row["id"]: row for row in document["songs"]}
        rows["validid"]["version"] = "7.2"
        review.write_text(json.dumps(document), encoding="utf-8")
        normalized = tree_hashes(batch / "songs")
        self.settings["import"]["version"] = "8.0"
        MemoryUpload.fail_after = 0
        with self.assertRaises(OSError):
            self.publish(batch)
        record = read_json(batch / "publish.json")
        self.assertEqual(record["binding"]["import"]["version"], "8.0")
        self.assertEqual(record["requested_import"]["version"], "8.0")
        self.assertEqual(read_json(review), document)
        self.assertEqual(tree_hashes(batch / "songs"), normalized)
        self.settings = workspace_settings(batch.parent, batch, default_settings(), self.root)
        self.assertEqual(self.settings["import"]["version"], "8.0")
        self.settings["import"]["version"] = "8.1"
        connections = MemoryUpload.connections
        with self.assertRaisesRegex(PlanError, "import.version"):
            self.publish(batch)
        self.assertEqual(MemoryUpload.connections, connections)
        self.settings["import"]["version"] = "8.0"
        MemoryUpload.fail_after = None
        with patch("arcaea_uploader.publishing.build_configured_bundle", side_effect=AssertionError("reuse build")):
            self.assertEqual(self.publish(batch)["version"], "7.0.256.5")
        self.assertEqual({row["id"]: row["version"] for row in read_json(self.assets / "songs/songlist")["songs"]},
                         {"validid": "7.2", "second": "7.1"})

    def test_blank_import_default_does_not_replace_workspace_settings(self):
        _, batch = self.prepare()
        before = digest(batch / "publish.json"), digest(batch / "workspace.json")
        self.settings["import"]["version"] = ""
        with self.assertRaisesRegex(ValueError, "version"):
            self.publish(batch)
        self.assertEqual((digest(batch / "publish.json"), digest(batch / "workspace.json")), before)
        self.assertEqual(MemoryUpload.connections, 0)

    def test_invalid_review_versions_and_remote_dl_still_fail_before_build(self):
        plan, batch = self.prepare()
        path = Path(plan["review_songlist"])
        original, reviewed = tree_hashes(self.assets), read_json(path)
        cases = [("version", value) for value in (None, "", "  ", 3.2, "3.2\n", "3.2\x00")]
        cases.append(("remote_dl", False))
        with patch("arcaea_uploader.publishing.build_configured_bundle") as build:
            for field, value in cases:
                with self.subTest(field=field, value=value):
                    document = json.loads(json.dumps(reviewed))
                    if value is None:
                        document["songs"][0].pop(field)
                    else:
                        document["songs"][0][field] = value
                    path.write_text(json.dumps(document), encoding="utf-8")
                    with self.assertRaisesRegex(PlanError, "validid: " + field):
                        self.publish(batch)
            build.assert_not_called()
        self.assertEqual(tree_hashes(self.assets), original)
        self.assertEqual(MemoryUpload.connections, 0)

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

    def test_live_baseline_and_internal_workcopy_drift_block_publication(self):
        for where in ("active", "prepared", "normalized"):
            with self.subTest(where=where):
                plan, batch = self.prepare()
                path = {"active": self.assets / "songs/songlist",
                        "prepared": batch / "prepared/database/songs/validid/2.aff",
                        "normalized": batch / "songs/validid/2.aff"}[where]
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                with self.assertRaises(PlanError):
                    self.publish(batch)
                path.write_bytes(original)
        self.assertEqual(MemoryUpload.connections, 0)

    def test_source_edits_are_logged_without_changing_reviewed_publication(self):
        plan, batch = self.prepare()
        stage = Path(plan["prepared_assets"])
        reviewed = read_json(stage / "songs/songlist")
        reviewed["songs"][0]["title_localized"]["en"] = "Human review"
        (stage / "songs/songlist").write_text(json.dumps(reviewed), encoding="utf-8")
        original_chart = (batch / "prepared/database/songs/validid/2.aff").read_bytes()
        (self.package / "2.aff").write_bytes(b"new author chart")
        (self.package / "slst.txt").write_text('{"invalid":"unfinished author edit"}', encoding="utf-8")
        (self.package / "base.jpg").unlink()
        (self.package.parent / "extra.txt").write_bytes(b"unrelated file")
        logs = []
        publish_update(self.settings, self.root, batch, transport_factory=MemoryUpload, on_diagnostic=logs.append)
        self.assertEqual(read_json(self.assets / "songs/songlist"), reviewed)
        self.assertEqual((self.database / "songs/validid/2.aff").read_bytes(), original_chart)
        changes = read_json(batch / "publish.json")["source_changes"]
        self.assertEqual(len(changes), 1)
        self.assertEqual({c["action"] for c in changes[0]["changes"]}, {"add", "modify", "remove"})
        self.assertTrue(any("来源目录已有修改" in entry["message"] for entry in logs))

    def test_missing_source_directory_can_publish_its_complete_snapshot(self):
        _, batch = self.prepare()
        shutil.rmtree(self.root / "incoming")
        self.assertEqual(self.publish(batch)["version"], "7.0.256.5")
        self.assertEqual((self.database / "songs/validid/2.aff").read_text(encoding="utf-8"), REAL)

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

    def test_resized_background_enters_bundle_and_transition_names_dot_zero(self):
        background = self.package / "custom.jpg"
        Image.new("RGB", (1920, 1200)).save(background)
        self.settings["bundle"] = {"follow_app_version": False, "app_version": "7.0.257"}
        plan, batch = self.prepare()
        with Image.open(Path(plan["prepared_assets"]) / "img/bg/1080/custom.jpg") as image:
            self.assertEqual(image.size, (1920, 1440))
        result = self.publish(batch)
        self.assertIn("bundle/7.0.257.0.json", MemoryUpload.files)
        self.assertEqual(result["version"], "7.0.257")
        with Image.open(self.assets / "img/bg/1080/custom.jpg") as image:
            self.assertEqual(image.size, (1920, 1440))
        with Image.open(background) as image:
            self.assertEqual(image.size, (1920, 1200))

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
