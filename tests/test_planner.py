from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from PIL import Image

from arcaea_uploader.planner import (
    PlanError, build_plan, digest, load_package, merge_songlist, resolve_config,
)


class MergeTests(unittest.TestCase):
    def test_add_difficulty_preserves_index_metadata_and_original(self):
        master = {"format": "custom", "songs": [{"id": "song", "idx": 18,
                  "remote_dl": False, "version": "old", "date": 1, "bg_inverse": "night", "bg": "day",
                  "title_localized": {"en": "Title"}, "difficulties": [
                      {"ratingClass": 2, "rating": 9}]}]}
        before = deepcopy(master)
        merged, conflicts = merge_songlist(master, [{"id": "song", "idx": 0,
                     "difficulties": [{"ratingClass": 3, "rating": 10}]}])
        self.assertEqual(master, before)
        self.assertEqual(merged["format"], "custom")
        self.assertEqual(merged["songs"][0]["idx"], 18)
        self.assertEqual(merged["songs"][0]["title_localized"], {"en": "Title"})
        self.assertEqual([d["ratingClass"] for d in merged["songs"][0]["difficulties"]], [2, 3])
        self.assertTrue(merged["songs"][0]["remote_dl"])
        keys = list(merged["songs"][0])
        self.assertEqual(keys[0], "idx")
        self.assertEqual(keys[keys.index("date"):keys.index("date") + 3], ["date", "version", "remote_dl"])
        self.assertEqual(keys[keys.index("bg") + 1], "bg_inverse")
        self.assertEqual(conflicts, [])

    def test_conflict_retains_existing_value_and_merges_new_fields(self):
        master = {"songs": [{"id": "song", "idx": 2, "artist": "Old",
                            "difficulties": [{"ratingClass": 3, "rating": 9}]}]}
        merged, conflicts = merge_songlist(master, [{"id": "song", "artist": "New",
                      "difficulties": [{"ratingClass": 3, "rating": 10, "chartDesigner": "A"}]}])
        self.assertEqual(len(conflicts), 2)
        row = merged["songs"][0]
        self.assertEqual(row["artist"], "Old")
        self.assertEqual(row["difficulties"][0], {"ratingClass": 3, "rating": 9, "chartDesigner": "A"})

    def test_new_idx_uses_max_and_repeated_merge_is_stable(self):
        master = {"songs": [{"id": "a", "idx": 20}, {"id": "b", "idx": 3}]}
        patches = [{"id": "c", "idx": 0}, {"id": "d"}]
        merged, _ = merge_songlist(master, patches)
        self.assertEqual([row["idx"] for row in merged["songs"]], [20, 3, 21, 22])
        repeated, conflicts = merge_songlist(merged, patches)
        self.assertEqual(repeated, merged)
        self.assertEqual(conflicts, [])

    def test_duplicates_and_invalid_classes_rejected(self):
        for difficulties in ([{"ratingClass": 3}, {"ratingClass": 3}],
                             [{"ratingClass": 5}], [{"ratingClass": True}]):
            with self.subTest(difficulties=difficulties), self.assertRaises(PlanError):
                merge_songlist({"songs": []}, [{"id": "song", "difficulties": difficulties}])
        with self.assertRaises(PlanError):
            merge_songlist({"songs": [{"id": "a", "idx": -1}]}, [])


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.assets = self.root / "资源包 active"
        self.database = self.root / "server" / "latest version" / "database"
        self.incoming = self.root / "待更新"
        (self.assets / "songs").mkdir(parents=True)
        (self.database / "songs").mkdir(parents=True)
        self.incoming.mkdir()
        self.placeholder = self.root / "placeholder.aff"
        self.placeholder.write_bytes(b"AudioOffset:0\n-\ntiming(0,100.00,4.00);\n")
        self.write_master([])
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps({"import": {"version": "7.0"}, "local": {
            "input_root": str(self.incoming), "assets_root": str(self.assets),
            "server_project_root": str(self.root / "server"),
            "placeholder_aff": str(self.placeholder),
        }}), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def write_master(self, songs):
        (self.assets / "songs" / "songlist").write_text(json.dumps({"songs": songs}), encoding="utf-8")

    def package(self, song_id="sample", partial=False, metadata=None, extension=""):
        folder = self.incoming / song_id
        folder.mkdir()
        patch = metadata or {"id": song_id, "audioPreview": 0, "audioPreviewEnd": 5000,
                             "difficulties": [{"ratingClass": 2, "rating": 9}]}
        (folder / f"songlist{extension}").write_text(json.dumps({"songs": [patch]}), encoding="utf-8-sig")
        (folder / ("3.aff" if partial else "2.aff")).write_bytes(b"real chart")
        if not partial:
            (folder / "base.ogg").write_bytes(b"source audio")
            for name in ("base.jpg", "base_256.jpg"):
                Image.new("RGB", (800, 800)).save(folder / name)
        return folder

    def plan(self):
        return build_plan(resolve_config(self.config_path))

    def test_new_song_routing_and_fill_only_missing_charts(self):
        folder = self.package()
        (folder / "video_1080.mp4").write_bytes(b"video")
        plan = self.plan()
        self.assertTrue(plan["valid"], plan["errors"])
        targets = {(f["area"], f["path"]): f for f in plan["files"]}
        self.assertEqual(targets[("database", "songs/sample/2.aff")]["placeholder"], False)
        for rating in (0, 1):
            self.assertTrue(targets[("database", f"songs/sample/{rating}.aff")]["placeholder"])
        self.assertIn(("database", "songs/sample/video_1080.mp4"), targets)
        self.assertIn(("assets", "songs/dl_sample/base.jpg"), targets)
        self.assertNotIn(("assets", "songs/dl_sample/2.aff"), targets)
        self.assertNotIn(("database", "songs/sample/base.jpg"), targets)
        self.assertNotIn(("database", "songs/songlist"), targets)
        self.assertFalse(plan["publishable"])

    def test_user_incremental_example_preserves_real_chart(self):
        self.write_master([{"id": "sample", "idx": 7, "difficulties": [{"ratingClass": 2, "rating": 9}]}])
        server_song = self.database / "songs" / "sample"
        server_song.mkdir()
        (server_song / "2.aff").write_bytes(b"existing real chart")
        self.package(partial=True, metadata={"id": "sample", "difficulties": [{"ratingClass": 3, "rating": 10}]})
        plan = self.plan()
        self.assertTrue(plan["valid"], plan["errors"])
        entries = {f["path"]: f for f in plan["files"] if f["area"] == "database"}
        self.assertNotIn("songs/sample/2.aff", entries)
        self.assertIn("songs/sample/3.aff", entries)
        self.assertEqual([d["ratingClass"] for d in plan["merged_songlist"]["songs"][0]["difficulties"]], [2, 3])
        self.assertEqual((server_song / "2.aff").read_bytes(), b"existing real chart")
        self.assertEqual(plan["songs"][0]["kind"], "incremental")

    def test_background_sizes_and_unknown_resolution(self):
        folder = self.package()
        for name, size in (("wide", (1920, 1440)), ("normal", (1440, 1080)),
                           ("small", (1280, 960))):
            Image.new("RGB", size).save(folder / f"{name}.jpg")
        plan = self.plan()
        paths = {f["path"] for f in plan["files"] if f["area"] == "assets"}
        self.assertIn("img/bg/1080/wide.jpg", paths)
        self.assertIn("img/bg/normal.jpg", paths)
        self.assertIn("img/bg/small.jpg", paths)
        self.assertNotIn("img/bg/unknown.jpg", paths)
        Image.new("RGB", (100, 100)).save(folder / "unknown.jpg")
        plan = self.plan()
        self.assertTrue(plan["valid"], plan["errors"])
        self.assertTrue(any(f["path"] == "img/bg/1080/unknown.jpg" and f["action"] == "prepare_media" for f in plan["files"]))

    def test_identical_download_file_is_unchanged(self):
        folder = self.package()
        destination = self.database / "songs" / "sample"
        destination.mkdir()
        (destination / "2.aff").write_bytes((folder / "2.aff").read_bytes())
        entries = self.plan()["files"]
        chart = next(f for f in entries if f["path"] == "songs/sample/2.aff")
        self.assertEqual(chart["action"], "unchanged")

    def test_existing_duplicate_idx_is_reported_without_reassigning_old_indices(self):
        self.write_master([{"id": "old_a", "idx": 218}, {"id": "old_b", "idx": 218}])
        self.package()
        plan = self.plan()
        self.assertFalse(plan["valid"])
        self.assertEqual(plan["master_songlist_issues"], [
            {"idx": 218, "ids": ["old_a", "old_b"], "issue": "duplicate_idx"}])
        self.assertEqual([row["idx"] for row in plan["merged_songlist"]["songs"]], [218, 218, 219])

    def test_no_resource_tree_is_changed_by_plan(self):
        self.package()
        def snapshot():
            return {str(p.relative_to(self.root)): digest(p) for p in self.root.rglob("*") if p.is_file()}
        before = snapshot()
        self.plan()
        self.assertEqual(before, snapshot())

    def test_independent_audio_uses_rating_class_not_array_index(self):
        folder = self.package(metadata={"id": "sample", "audioPreview": 0, "audioPreviewEnd": 5000,
                              "difficulties": [{"ratingClass": 3, "audioPreview": 10, "audioPreviewEnd": 3000}]})
        (folder / "3.ogg").write_bytes(b"independent audio")
        plan = self.plan()
        job = next(j for j in plan["preview_jobs"] if j["audio"] == "3.ogg")
        self.assertEqual((job["start_ms"], job["end_ms"]), (10, 3000))

    def test_songlist_extensions_and_duplicate_candidates(self):
        for number, extension in enumerate(("", ".json", ".txt")):
            folder = self.package(song_id=f"song{number}", extension=extension)
            self.assertEqual(load_package(folder)[0]["id"], folder.name)
        (folder / "songlist.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(PlanError):
            load_package(folder)

    def test_bad_package_does_not_hide_other_packages(self):
        self.package()
        bad = self.package(song_id="bad")
        (bad / "songlist").write_text("not json", encoding="utf-8")
        plan = self.plan()
        self.assertFalse(plan["valid"])
        self.assertEqual([s["id"] for s in plan["songs"]], ["sample"])

    def test_same_background_target_with_different_content_blocks_batch(self):
        for song_id, color in (("first", "red"), ("second", "blue")):
            folder = self.package(song_id=song_id)
            Image.new("RGB", (1280, 960), color).save(folder / "shared.jpg")
        plan = self.plan()
        self.assertTrue(any("same path" in e for e in plan["errors"]))

    def test_config_path_traversal_rejected(self):
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["local"]["database_relative_path"] = "../escape"
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaises(PlanError):
            resolve_config(self.config_path)

    def test_invalid_chart_name_and_mismatched_id_rejected(self):
        folder = self.package()
        (folder / "5.aff").write_bytes(b"invalid chart")
        with self.assertRaises(PlanError):
            load_package(folder)
        (folder / "5.aff").unlink()
        (folder / "songlist").write_text(json.dumps({"id": "other"}), encoding="utf-8")
        with self.assertRaises(PlanError):
            load_package(folder)

    def test_cli_emits_json_and_uses_exit_code_for_validation_errors(self):
        self.package()
        command = [sys.executable, "-m", "arcaea_uploader", "plan", "--config", str(self.config_path)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["valid"])
        (self.incoming / "sample" / "unknown.jpg").write_bytes(b"not a valid image")
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)["valid"])


if __name__ == "__main__":
    unittest.main()
