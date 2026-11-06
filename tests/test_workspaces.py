import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image

from arcaea_uploader.planner import PlanError
from arcaea_uploader.preprocess import preprocess_collection
from arcaea_uploader.workspaces import (cleanup_published_workspace, delete_workspace,
    list_workspaces, load_workspace, workspace_lock)
from test_preprocess import PLAYABLE, song_metadata


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.input = self.root / "incoming"
        self.input.mkdir()
        self.output = self.root / "releases"

    def tearDown(self):
        self.temp.cleanup()

    def package(self, sid="sample"):
        folder = self.input / sid
        folder.mkdir()
        (folder / "songlist").write_text(json.dumps(song_metadata(sid)), encoding="utf-8")
        (folder / "2.aff").write_bytes(PLAYABLE)
        (folder / "base.ogg").write_bytes(b"full audio")
        for name in ("base.jpg", "base_256.jpg"):
            Image.new("RGB", (384, 384)).save(folder / name)
        return folder

    def run_batch(self, batch=None, version="7.0"):
        return preprocess_collection(self.input, self.output, import_version=version, resume_from=batch)

    def test_listing_empty_root_is_read_only(self):
        self.assertEqual(list_workspaces(self.output), [])
        self.assertFalse(self.output.exists())

    def test_resume_fixed_package_reuses_success_across_sessions(self):
        good = self.package("a_good")
        bad = self.package("b_bad")
        metadata = song_metadata("b_bad")
        metadata["difficulties"][0]["rating"] = "bad"
        (bad / "songlist").write_text(json.dumps(metadata), encoding="utf-8")
        first = self.run_batch()
        self.assertFalse(first["valid"])
        batch = Path(first["batch_root"])
        self.assertEqual(list_workspaces(self.output)[0]["state"], "需处理 / 可继续")
        (bad / "songlist").write_text(json.dumps(song_metadata("b_bad")), encoding="utf-8")
        original_copy = shutil.copyfile
        def copy(source, target):
            self.assertNotEqual(Path(source).parent, good, "Unchanged success was copied again")
            return original_copy(source, target)
        with patch("arcaea_uploader.preprocess.shutil.copyfile", side_effect=copy):
            second = self.run_batch(batch)
        self.assertTrue(second["valid"], second["errors"])
        self.assertEqual(second["batch_root"], str(batch))
        self.assertTrue(second["packages"][0]["reused"])
        self.assertEqual(len(list(self.output.glob("preprocessed-*"))), 1)

    def test_checkpoint_recovers_after_interruption(self):
        self.package("a_good")
        self.package("b_later")
        original_copy = shutil.copyfile
        def interrupt(source, target):
            if Path(source).parent.name == "b_later":
                raise KeyboardInterrupt()
            return original_copy(source, target)
        with patch("arcaea_uploader.preprocess.shutil.copyfile", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_batch()
        batch = next(self.output.glob("preprocessed-*"))
        self.assertFalse(load_workspace(self.output, batch)["complete"])
        recovered = self.run_batch(batch)
        self.assertTrue(recovered["valid"], recovered["errors"])
        self.assertTrue(recovered["packages"][0]["reused"])

    def test_source_changes_rebuild_only_affected_package(self):
        a = self.package("a")
        self.package("b")
        first = self.run_batch()
        batch = Path(first["batch_root"])
        (a / "2.aff").write_bytes(PLAYABLE + b"(200,2);\n")
        result = self.run_batch(batch)
        self.assertFalse(result["packages"][0].get("reused", False))
        self.assertTrue(result["packages"][1]["reused"])
        self.assertEqual((batch / "songs/a/2.aff").read_bytes(), (a / "2.aff").read_bytes())

    def test_edited_output_is_preserved_outside_songs_and_rebuilt(self):
        self.package()
        first = self.run_batch()
        batch = Path(first["batch_root"])
        chart = batch / "songs/sample/2.aff"
        chart.write_bytes(b"manual edit")
        result = self.run_batch(batch)
        self.assertEqual(chart.read_bytes(), PLAYABLE)
        previous = list((batch / "work").glob("previous-*/sample/2.aff"))
        self.assertEqual(previous[0].read_bytes(), b"manual edit")
        self.assertTrue(any(d["code"] == "workspace.previous_saved" for d in result["diagnostics"]))

    def test_removed_input_cannot_linger_as_a_normalized_song(self):
        a = self.package("a")
        self.package("b")
        batch = Path(self.run_batch()["batch_root"])
        # Test fixtures only: verify before removing this explicitly created path.
        self.assertTrue(a.resolve().is_relative_to(self.input))
        shutil.rmtree(a)
        result = self.run_batch(batch)
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual([p.name for p in (batch / "songs").iterdir()], ["b"])

    def test_changed_import_version_invalidates_reuse(self):
        self.package()
        batch = Path(self.run_batch()["batch_root"])
        result = self.run_batch(batch, version="7.1")
        self.assertFalse(result["packages"][0].get("reused", False))
        metadata = json.loads((batch / "songs/sample/songlist").read_text(encoding="utf-8"))
        self.assertEqual(metadata["songs"][0]["version"], "7.1")

    def test_legacy_report_is_recognized_and_safely_rebuilt_once(self):
        self.package()
        first = self.run_batch()
        batch = Path(first["batch_root"])
        (batch / "workspace.json").unlink()
        first.pop("context_sha256")
        for package in first["packages"]:
            package.pop("output_sha256")
            package.pop("package_fingerprint")
        (batch / "report.json").write_text(json.dumps(first), encoding="utf-8")
        self.assertTrue(list_workspaces(self.output)[0]["managed"])
        result = self.run_batch(batch)
        self.assertTrue(result["valid"], result["errors"])
        self.assertFalse(result["packages"][0].get("reused", False))

    def test_cleanup_rejects_root_external_directory_and_mismatched_report(self):
        self.package()
        batch = Path(self.run_batch()["batch_root"])
        for target in (self.output, self.input):
            with self.subTest(target=target), self.assertRaises((ValueError, OSError)):
                delete_workspace(self.output, target)
        self.assertTrue(self.input.exists())
        (batch / "workspace.json").unlink()
        report = json.loads((batch / "report.json").read_text(encoding="utf-8"))
        report["batch_root"] = str(self.input)
        (batch / "report.json").write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaises(PlanError):
            delete_workspace(self.output, batch)
        self.assertTrue(batch.exists())

    def test_active_lock_blocks_resume_and_cleanup_then_releases(self):
        self.package()
        batch = Path(self.run_batch()["batch_root"])
        with workspace_lock(self.output, batch):
            with self.assertRaises(PlanError):
                delete_workspace(self.output, batch)
            with self.assertRaises(PlanError):
                self.run_batch(batch)
        delete_workspace(self.output, batch)
        self.assertFalse(batch.exists())
        self.assertTrue((self.input / "sample/2.aff").exists())

    def test_owned_corrupt_report_can_be_cleaned_but_not_resumed(self):
        self.package()
        batch = Path(self.run_batch()["batch_root"])
        (batch / "report.json").write_text("{broken", encoding="utf-8")
        row = list_workspaces(self.output)[0]
        self.assertTrue(row["managed"])
        self.assertIsNone(row["report"])
        with self.assertRaises(PlanError):
            self.run_batch(batch)
        delete_workspace(self.output, batch)
        self.assertFalse(batch.exists())

    def test_complete_receipt_cleans_only_batch_and_keeps_small_audit(self):
        self.package()
        batch = Path(self.run_batch()["batch_root"])
        receipt = {"batch_id":batch.name, "local_verified":True, "remote_verified":True, "refresh":"failed"}
        with self.assertRaises(PlanError):
            cleanup_published_workspace(self.output, batch, receipt)
        self.assertTrue(batch.exists())
        receipt["refresh"] = "succeeded"
        receipt["token"] = "must-not-be-archived"
        archive = cleanup_published_workspace(self.output, batch, receipt)
        self.assertFalse(batch.exists())
        self.assertTrue(json.loads(archive.read_text(encoding="utf-8"))["cleanup_complete"])
        self.assertNotIn("must-not-be-archived", archive.read_text(encoding="utf-8"))
        self.assertTrue((self.input / "sample/2.aff").exists())

    def test_linked_directory_is_neither_traversed_nor_deleted(self):
        self.package()
        batch = Path(self.run_batch()["batch_root"])
        link = batch / "outside"
        try:
            link.symlink_to(self.input, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Symlink permission unavailable: {exc}")
        try:
            with self.assertRaises(PlanError):
                delete_workspace(self.output, batch)
            self.assertTrue((self.input / "sample/2.aff").exists())
        finally:
            link.unlink()

    def test_stage02_cleanup_retains_verified_stage03_task_for_later(self):
        from arcaea_uploader.server_tasks import (confirm_uploaded, load_task, make_task,
            read_reviewed, run_task, save_task, task_path)
        from test_server_tasks import FakeAPI
        self.package()
        batch = Path(self.run_batch()["batch_root"])
        source = batch / "reviewed-songlist"
        document = {"songs": [song_metadata("sample")]}
        source.write_text(json.dumps(document), encoding="utf-8")
        task = read_reviewed(make_task(batch.name, document, {"sample": [2]}), source)
        receipt = {"batch_id": batch.name, "local_verified": True, "remote_verified": True,
                   "songlist_sha256": task["source"]["sha256"]}
        confirm_uploaded(task, receipt)
        path = task_path(self.output, task["id"])
        save_task(path, task)
        receipt["server_task_file"] = str(path)
        before = source.read_bytes()
        source.write_bytes(before + b"\n")
        with self.assertRaises(PlanError):
            cleanup_published_workspace(self.output, batch, receipt)
        self.assertTrue(batch.exists())
        source.write_bytes(before)
        cleanup_published_workspace(self.output, batch, receipt)
        self.assertFalse(batch.exists())
        loaded = load_task(path)
        run_task(loaded, FakeAPI(), allow_missing=True)
        self.assertEqual(loaded["state"], "complete")

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_windows_junction_cannot_redirect_cleanup_into_originals(self):
        self.package()
        batch = Path(self.run_batch()["batch_root"])
        link = batch / "outside"
        subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(self.input)],
                       check=True, capture_output=True)
        try:
            with self.assertRaises(PlanError):
                delete_workspace(self.output, batch)
            self.assertTrue((self.input / "sample/2.aff").exists())
        finally:
            # Remove only this verified junction, never its target contents.
            from arcaea_uploader.workspaces import linked
            self.assertTrue(linked(link))
            self.assertTrue(link.parent.resolve().is_relative_to(self.output))
            os.rmdir(link)


if __name__ == "__main__":
    unittest.main()
