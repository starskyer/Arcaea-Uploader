import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from PIL import Image

from arcaea_uploader.planner import PlanError, digest, read_json
from arcaea_uploader.publishing import load_preparation, prepare_update, tree_hashes
from arcaea_uploader.server_tasks import load_task


class AccumulationTests(unittest.TestCase):
    def setUp(self):
        from test_publishing import PublishingTests
        self.fixture = PublishingTests("test_complete_two_stage_workflow_with_final_review_preview_and_task")
        self.fixture.setUp()
        self.root, self.settings = self.fixture.root, self.fixture.settings
        self.plan, self.batch = self.fixture.prepare()

    def tearDown(self):
        self.fixture.doCleanups()
        self.fixture.tearDown()

    def package(self, root, sid="second", *, incremental=False):
        from test_publishing import REAL
        folder = root / sid
        folder.mkdir(parents=True, exist_ok=True)
        if incremental:
            (folder / "3.aff").write_text(REAL + "\n(300,2);", encoding="utf-8")
            row = {"id": sid, "difficulties": [{"ratingClass": 3, "rating": 10}]}
        else:
            (folder / "2.aff").write_text(REAL, encoding="utf-8")
            (folder / "base.ogg").write_bytes(b"full audio fixture")
            Image.new("RGB", (512, 512), "#406090").save(folder / "base.jpg")
            row = dict(self.fixture.row, id=sid, purchase=sid, title_localized={"en": sid})
        (folder / "songlist").write_text(json.dumps(row), encoding="utf-8")
        return folder

    def append(self, root):
        self.settings["local"]["input_root"] = str(root)
        return prepare_update(self.settings, self.root, resume_from=self.batch)

    def test_multiple_directories_accumulate_and_publish_once_preserving_review(self):
        review = Path(self.plan["review_songlist"])
        document = read_json(review)
        document["songs"][0].update(purchase="newpack", audioPreviewEnd=900, custom_note="human")
        review.write_text(json.dumps(document), encoding="utf-8")
        packlist = self.batch / "prepared/active/songs/packlist"
        packlist.write_text('{"packs":[{"id":"handwritten"}]}', encoding="utf-8")
        second = self.root / "incoming-two"
        self.package(second)
        plan = self.append(second)
        self.assertEqual(plan["review_songlist"], str(review))
        rows = read_json(review)["songs"]
        self.assertEqual([r["id"] for r in rows], ["validid", "second"])
        self.assertEqual([r["idx"] for r in rows], [0, 1])
        self.assertEqual(rows[0]["custom_note"], "human")
        self.assertEqual(rows[0]["purchase"], "newpack")
        self.assertEqual(read_json(packlist)["packs"][0]["id"], "handwritten")
        third = self.root / "incoming-three"
        self.package(third, "third")
        plan = self.append(third)
        self.assertEqual(len(plan["songs"]), 3)
        self.assertEqual(len(list((self.root / "releases").glob("preprocessed-*"))), 1)
        self.assertFalse(list(self.batch.rglob("*.cb")))
        self.assertEqual(len(read_json(self.fixture.assets / "metadata.oldjson")), 1)
        result = self.fixture.publish(self.batch)
        task = load_task(result["server_task_file"])
        self.assertEqual({s["id"] for s in task["songs"]}, {"validid", "second", "third"})
        self.assertEqual(result["version"], "7.0.256.5")
        self.assertEqual(len(read_json(self.fixture.assets / "metadata.oldjson")), 2)

    def test_same_directory_rescan_is_idempotent_and_preserves_manual_values(self):
        root = self.root / "incoming"
        review = Path(self.plan["review_songlist"])
        document = read_json(review)
        document["songs"][0]["title_localized"]["en"] = "Reviewed title"
        review.write_text(json.dumps(document), encoding="utf-8")
        self.package(root)
        for _ in range(2):
            plan = self.append(root)
            self.assertTrue(plan["valid"])
        rows = read_json(review)["songs"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["title_localized"]["en"], "Reviewed title")
        self.assertEqual(len({r["idx"] for r in rows}), 2)
        self.assertEqual(read_json(self.batch / "report.json")["counts"]["normalized"], 2)

    def test_repeated_import_keeps_reviewed_fields_and_deletions_across_rounds(self):
        review = Path(self.plan["review_songlist"])
        document = read_json(review)
        row = document["songs"][0]
        row.update(set="reviewed_pack", purchase="reviewed_pack", bg="reviewed_bg", bg_inverse="reviewed_inverse")
        row["title_localized"] = {"en": "Reviewed title", "zh-Hans": "人工曲名"}
        row.pop("artist")
        row["difficulties"][2]["rating"] = 10
        row["difficulties"][2]["chartDesigner"] = "Reviewer"
        expected = json.loads(json.dumps(row))
        review.write_text(json.dumps(document), encoding="utf-8")
        for _ in range(3):
            self.append(self.root / "incoming")
            self.assertEqual(read_json(review)["songs"][0], expected)
        self.assertTrue(read_json(self.batch / "publish.json")["review_protected_paths"]["validid"])
        # A genuinely new difficulty must still be imported beside reviewed ones.
        second = self.root / "patch"
        self.package(second, "validid", incremental=True)
        self.append(second)
        result = read_json(review)["songs"][0]
        self.assertEqual(result.pop("difficulties")[:3], expected["difficulties"])
        expected.pop("difficulties")
        self.assertEqual(result, expected)

    def test_legacy_prepared_workspace_resumes_without_losing_review(self):
        from arcaea_uploader.workspaces import workspace_settings
        review = Path(self.plan["review_songlist"])
        document = read_json(review)
        document["songs"][0]["purchase"] = "reviewed"
        review.write_text(json.dumps(document), encoding="utf-8")
        (self.batch / "workspace.json").unlink()
        restored = workspace_settings(self.root / "releases", self.batch, self.settings, self.root)
        self.settings = restored
        self.append(self.root / "incoming")
        self.assertEqual(read_json(review)["songs"][0]["purchase"], "reviewed")
        self.assertTrue((self.batch / "workspace.json").exists())

    def test_same_song_new_difficulty_keeps_previous_chart_audio_and_idx(self):
        second = self.root / "patch"
        self.package(second, "validid", incremental=True)
        before_audio = digest(self.batch / "prepared/database/songs/validid/base.ogg")
        plan = self.append(second)
        self.assertEqual(plan["actual"]["validid"], [2, 3])
        folder = self.batch / "prepared/database/songs/validid"
        self.assertTrue((folder / "2.aff").is_file())
        self.assertTrue((folder / "3.aff").is_file())
        self.assertEqual(digest(folder / "base.ogg"), before_audio)
        self.assertEqual(plan["songs"][0]["idx"], 0)
        self.assertTrue((self.batch / "songs/validid/2.aff").is_file())
        self.assertTrue((self.batch / "songs/validid/3.aff").is_file())
        result = self.fixture.publish(self.batch)
        self.assertEqual(load_task(result["server_task_file"])["actual"]["validid"], [2, 3])

    def test_failed_addition_keeps_previous_files_but_blocks_subset_publication(self):
        second = self.root / "bad-import"
        folder = second / "second"
        folder.mkdir(parents=True)
        (folder / "2.aff").write_bytes(b"chart")
        before = tree_hashes(self.batch / "prepared")
        with self.assertRaisesRegex(PlanError, "追加未完成"):
            self.append(second)
        self.assertEqual(tree_hashes(self.batch / "prepared"), before)
        with self.assertRaisesRegex(PlanError, "未完成的追加"):
            self.fixture.publish(self.batch)
        self.package(second)
        plan = self.append(second)
        self.assertEqual(len(plan["songs"]), 2)
        self.assertNotIn("pending_append", read_json(self.batch / "publish.json"))
        self.assertEqual(self.fixture.publish(self.batch)["version"], "7.0.256.5")

    def test_existing_real_chart_conflict_never_changes_prepared_resources(self):
        root = self.root / "conflict"
        folder = self.package(root, "validid", incremental=True)
        (folder / "2.aff").write_bytes(b"a different real chart")
        before = tree_hashes(self.batch / "prepared")
        with self.assertRaisesRegex(PlanError, "Existing file differs"):
            self.append(root)
        self.assertEqual(tree_hashes(self.batch / "prepared"), before)

    def test_failed_preview_write_leaves_previous_workspace_and_review_intact(self):
        review = Path(self.plan["review_songlist"])
        document = read_json(review)
        document["songs"][0]["audioPreviewEnd"] = 900
        review.write_text(json.dumps(document), encoding="utf-8")
        before = tree_hashes(self.batch / "prepared")
        def fail_after_write(source, target, *args, **kwargs):
            Path(target).write_bytes(b"partial failed preview")
            raise OSError("synthetic media failure")
        with patch("arcaea_uploader.publishing.create_preview", side_effect=fail_after_write):
            with self.assertRaisesRegex(OSError, "synthetic media failure"):
                self.append(self.root / "incoming")
        self.assertEqual(tree_hashes(self.batch / "prepared"), before)
        self.assertEqual(read_json(review), document)

    def test_filesystem_without_hardlinks_still_appends_and_keeps_live_files_unchanged(self):
        original = tree_hashes(self.fixture.assets)
        second = self.root / "patch"
        self.package(second, "validid", incremental=True)
        with patch("arcaea_uploader.private_files.os.link", side_effect=OSError("unsupported")):
            plan = self.append(second)
        self.assertEqual(plan["actual"]["validid"], [2, 3])
        self.assertEqual(tree_hashes(self.fixture.assets), original)

    def test_version_change_requires_separate_batch(self):
        self.settings["import"]["version"] = "7.2"
        with self.assertRaisesRegex(PlanError, "version 必须一致"):
            self.append(self.root / "incoming")

    def test_locked_build_cannot_accept_more_imports(self):
        from test_publishing import MemoryUpload
        MemoryUpload.fail_after = 0
        with self.assertRaises(OSError):
            self.fixture.publish(self.batch)
        with self.assertRaisesRegex(PlanError, "已开始打包"):
            self.append(self.root / "incoming")

    def test_prior_source_drift_is_checked_after_multiple_additions(self):
        second = self.root / "two"
        self.package(second)
        self.append(second)
        (self.fixture.package / "2.aff").write_bytes(b"modified original")
        with self.assertRaisesRegex(PlanError, "资源发生变化"):
            self.fixture.publish(self.batch)

    def test_unsaved_external_race_preserves_newer_saved_songlist(self):
        from test_publishing import preview
        second = self.root / "two"
        self.package(second)
        review = Path(self.plan["review_songlist"])
        def save_during_render(*args, **kwargs):
            preview(*args, **kwargs)
            document = read_json(review)
            document["songs"][0]["custom_note"] = "saved during append"
            review.write_text(json.dumps(document), encoding="utf-8")
        with patch("arcaea_uploader.publishing.create_preview", side_effect=save_during_render):
            with self.assertRaisesRegex(PlanError, "审核文档再次保存"):
                self.append(second)
        self.assertEqual(read_json(review)["songs"][0]["custom_note"], "saved during append")
        self.assertEqual(len(read_json(review)["songs"]), 1)
        self.append(second)
        self.assertEqual(read_json(review)["songs"][0]["custom_note"], "saved during append")

    def test_interrupted_commit_rolls_back_then_continues_same_batch(self):
        from arcaea_uploader import accumulation
        real_write = accumulation.write_json
        second = self.root / "two"
        self.package(second)
        before = tree_hashes(self.batch / "prepared")
        def stop(path, value):
            if Path(path) == self.batch / "plan.json" and len(value.get("songs", [])) == 2:
                raise SystemExit("synthetic process interruption")
            return real_write(path, value)
        with patch("arcaea_uploader.accumulation.write_json", side_effect=stop):
            with self.assertRaises(SystemExit):
                self.append(second)
        self.assertTrue((self.batch / "append-transaction.json").is_file())
        with self.assertRaisesRegex(PlanError, "提交尚未完成"):
            load_preparation(self.root / "releases", self.batch)
        plan = self.append(second)
        self.assertEqual(len(plan["songs"]), 2)
        self.assertFalse((self.batch / "append-transaction.json").exists())
        self.assertTrue((self.batch / "prepared/database/songs/validid/2.aff").is_file())
        self.assertEqual(self.fixture.publish(self.batch)["version"], "7.0.256.5")

    def test_unchanged_wav_is_not_retranscoded_when_rescanning(self):
        ffmpeg = os.environ.get("ARCAEA_UPLOADER_TEST_FFMPEG") or shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("FFmpeg unavailable")
        # Start a separate first batch with real WAV normalization.
        (self.fixture.package / "base.ogg").unlink()
        self.settings["local"]["ffmpeg_path"] = ffmpeg
        subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-y", str(self.fixture.package / "base.wav")], capture_output=True, check=True)
        _, self.batch = self.fixture.prepare()
        old_hash = digest(self.batch / "prepared/database/songs/validid/base.ogg")
        with patch("arcaea_uploader.preprocess.transcode_audio", side_effect=AssertionError("reuse normalized audio")):
            self.append(self.root / "incoming")
            self.append(self.root / "incoming")
        self.assertEqual(digest(self.batch / "prepared/database/songs/validid/base.ogg"), old_hash)


if __name__ == "__main__":
    unittest.main()
