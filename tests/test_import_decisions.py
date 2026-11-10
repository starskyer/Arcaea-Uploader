from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from PIL import Image

from arcaea_uploader.backgrounds import BackgroundIndex, complete_inverse, learn_pairs
from arcaea_uploader.import_rules import actual_chart_classes
from arcaea_uploader.planner import load_package, merge_songlist
from arcaea_uploader.preprocess import preprocess_collection, scan_collection
from arcaea_uploader.settings import default_settings, effective_settings, save_settings, load_settings
from test_preprocess import song_metadata, EMPTY, PLAYABLE


class ImportDecisionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.input = self.root / "incoming"
        self.input.mkdir()
        self.assets = self.root / "active"
        (self.assets / "songs").mkdir(parents=True)
        (self.assets / "img/bg/1080").mkdir(parents=True)
        for name in ("base_light.jpg", "base_conflict.jpg"):
            (self.assets / "img/bg" / name).write_bytes(b"existing background")
        self.output = self.root / "releases"

    def tearDown(self):
        self.temp.cleanup()

    def package(self, folder="example", row=None):
        path = self.input / folder
        path.mkdir()
        (path / "2.aff").write_bytes(PLAYABLE)
        (path / "base.ogg").write_bytes(b"full audio")
        Image.new("RGB", (384, 384)).save(path / "base.jpg")
        Image.new("RGB", (384, 384)).save(path / "base_256.jpg")
        (path / "songlist").write_text(json.dumps(row or song_metadata()), encoding="utf-8")
        return path

    def scan(self, **kwargs):
        return scan_collection(self.input, assets_root=self.assets, import_version="2026.11", **kwargs)

    def process(self, **kwargs):
        return preprocess_collection(self.input, self.output, assets_root=self.assets, import_version="2026.11", **kwargs)

    def test_version_overrides_song_only_without_generating_difficulty_versions(self):
        row = song_metadata()
        row.pop("version")
        row["remote_dl"] = False
        row["difficulties"] = [{"ratingClass": 0, "rating": -1, "version": "old"},
                               {"ratingClass": 2, "rating": 9}]
        folder = self.package(row=row)
        before = (folder / "songlist").read_bytes()
        report = self.process()
        self.assertTrue(report["valid"], report["errors"])
        result = report["packages"][0]["songlist"]
        self.assertEqual(result["version"], "2026.11")
        self.assertTrue(result["remote_dl"])
        self.assertEqual(result["difficulties"][0]["version"], "old")
        self.assertNotIn("version", result["difficulties"][1])
        self.assertEqual(result["difficulties"][0]["rating"], 0)
        self.assertEqual((folder / "songlist").read_bytes(), before)

    def test_missing_import_version_stops_before_creating_a_batch(self):
        self.package()
        with self.assertRaisesRegex(ValueError, "version"):
            preprocess_collection(self.input, self.output)
        self.assertFalse(self.output.exists())

    def test_invalid_or_missing_id_is_error_and_original_folder_stays(self):
        row = song_metadata()
        row["id"] = ""
        folder = self.package(row=row)
        report = self.process()
        self.assertFalse(report["valid"])
        self.assertEqual(report["diagnostic_counts"]["error"], 1)
        self.assertTrue(folder.exists())
        self.assertEqual(list(Path(report["normalized_root"]).iterdir()), [])
        self.assertFalse(any("rename" in d["code"] for d in report["diagnostics"]))

    def test_valid_work_copy_rename_is_info(self):
        self.package("outer_name")
        report = self.process()
        self.assertTrue((Path(report["normalized_root"]) / "example").is_dir())
        self.assertTrue((self.input / "outer_name").is_dir())
        self.assertTrue(any(d["code"] == "package.rename_planned" and d["level"] == "info" for d in report["diagnostics"]))

    def test_complete_object_with_extra_closing_brackets_recovers_as_warning(self):
        folder = self.package()
        (folder / "songlist").write_text(json.dumps(song_metadata()) + "\n ]\n }", encoding="utf-8")
        report = self.process()
        self.assertTrue(report["valid"], report["errors"])
        self.assertTrue(any(d["code"] == "metadata.recovered" and d["level"] == "warning" for d in report["diagnostics"]))

    def test_rating_non_numeric_values_are_errors_not_placeholder_repairs(self):
        folder = self.package()
        for value in ("DJ", "9", None, True):
            row = song_metadata()
            row["difficulties"][0]["rating"] = value
            (folder / "songlist").write_text(json.dumps(row), encoding="utf-8")
            report = self.scan()
            self.assertFalse(report["valid"])
            self.assertTrue(any(d["level"] == "error" and "rating" in d["message"] for d in report["diagnostics"]))

    def test_actual_chart_negative_rating_is_error(self):
        row = song_metadata()
        row["difficulties"][0]["rating"] = -1
        self.package(row=row)
        self.assertFalse(self.scan()["valid"])

    def test_declared_nonexistent_chart_ratings_are_all_zero(self):
        row = song_metadata()
        row["difficulties"] += [{"ratingClass": 0, "rating": -1}, {"ratingClass": 1, "rating": 0},
                                {"ratingClass": 3, "rating": 10}]
        self.package(row=row)
        report = self.scan()
        values = {d["ratingClass"]: d["rating"] for d in report["packages"][0]["songlist"]["difficulties"]}
        self.assertEqual(values, {0: 0, 1: 0, 2: 9, 3: 0})

    def test_hitsound_wav_is_warning_and_excluded_from_both_import_sides(self):
        folder = self.package()
        raw = PLAYABLE + b"arc(100,200,0,1,s,0,1,0,glass_wav,true)[arctap(200)];\n"
        (folder / "2.aff").write_bytes(raw)
        (folder / "glass.wav").write_bytes(b"custom effect")
        report = self.process()
        self.assertTrue(report["valid"], report["errors"])
        output = Path(report["normalized_root"]) / "example"
        self.assertFalse((output / "glass.wav").exists())
        self.assertEqual((output / "2.aff").read_bytes(), raw)
        self.assertTrue(any(d["code"] == "audio.hitsound_excluded" and d["level"] == "warning" for d in report["diagnostics"]))
        diagnostics = []
        _, files, _ = load_package(folder, diagnostics)
        self.assertFalse(any(p.suffix == ".wav" for p in files))
        self.assertTrue(any(d["level"] == "warning" for d in diagnostics))

    def test_png_bytes_under_jpg_suffix_become_real_jpeg_without_changing_sources(self):
        folder = self.package()
        for name in ("base.jpg", "base_256.jpg"):
            Image.new("RGBA", (384, 384), (0, 0, 255, 120)).save(folder / name, format="PNG")
        original = {name: (folder / name).read_bytes() for name in ("base.jpg", "base_256.jpg")}
        report = self.process()
        output = Path(report["normalized_root"]) / "example"
        self.assertTrue(report["valid"], report["errors"])
        for name, raw in original.items():
            self.assertEqual((folder / name).read_bytes(), raw)
            with Image.open(output / name) as image:
                self.assertEqual((image.format, image.mode), ("JPEG", "RGB"))

    def test_existing_background_is_skipped_before_size_or_encoding_read(self):
        folder = self.package()
        (self.assets / "img/bg/1080/custom.jpg").write_bytes(b"existing")
        (folder / "custom.jpg").write_bytes(b"not an image; must not be decoded")
        report = self.process()
        self.assertTrue(report["valid"], report["errors"])
        self.assertFalse((Path(report["normalized_root"]) / "example/custom.jpg").exists())
        self.assertTrue(any(d["code"] == "background.skip_existing" and d["level"] == "info" for d in report["diagnostics"]))

    def test_nonstandard_background_is_resized_to_hd_without_changing_source(self):
        folder = self.package()
        image = folder / "custom.jpg"
        Image.new("RGB", (1920, 1440)).save(image)
        report = self.scan()
        self.assertTrue(report["valid"], report["errors"])
        action = next(a for a in report["packages"][0]["actions"] if a["target"] == "custom.jpg")
        self.assertEqual(action["background_target"], "img/bg/1080/custom.jpg")
        Image.new("RGB", (1920, 1200)).save(image)
        original = image.read_bytes()
        report = self.process()
        self.assertTrue(report["valid"], report["errors"])
        with Image.open(Path(report["normalized_root"]) / "example/custom.jpg") as resized:
            self.assertEqual((resized.size, resized.format), ((1920, 1440), "JPEG"))
        self.assertEqual(image.read_bytes(), original)
        self.assertTrue(any(d["code"] == "background.resize" and d["level"] == "info" for d in report["diagnostics"]))

    def test_only_png_base_supplies_both_jpeg_jackets_and_preserves_original(self):
        folder = self.package()
        for name in ("base.jpg", "base_256.jpg"):
            (folder / name).unlink()
        source = folder / "base.png"
        Image.new("RGBA", (640, 500), (0, 0, 255, 0)).save(source)
        original = source.read_bytes()
        report = self.process()
        self.assertTrue(report["valid"], report["errors"])
        output = Path(report["normalized_root"]) / "example"
        for name, size in (("base.jpg", (640, 500)), ("base_256.jpg", (384, 384))):
            with Image.open(output / name) as image:
                self.assertEqual((image.format, image.size, image.mode), ("JPEG", size, "RGB"))
                self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
        self.assertEqual(source.read_bytes(), original)

    def test_explicit_pair_requires_both_supplied_images_and_does_not_learn_inference(self):
        row = song_metadata()
        row.update(bg="new_light", bg_inverse="new_conflict")
        folder = self.package(row=row)
        Image.new("RGB", (300, 200)).save(folder / "new_light.png")
        self.assertEqual(self.scan()["learned_background_pairs"], {})
        Image.new("RGB", (400, 300)).save(folder / "new_conflict.png")
        report = self.scan()
        self.assertEqual(report["learned_background_pairs"], {"new_light": "new_conflict"})
        self.assertTrue(any(d["code"] == "background.pair_discovered" and d["level"] == "info" for d in report["diagnostics"]))
        row.pop("bg_inverse")
        (folder / "songlist").write_text(json.dumps(row), encoding="utf-8")
        self.assertEqual(self.scan()["learned_background_pairs"], {})

    def test_input_pair_cannot_replace_an_existing_background_partner(self):
        row = song_metadata()
        row.update(bg="new_light", bg_inverse="new_conflict")
        folder = self.package(row=row)
        for name in ("new_light", "new_conflict"):
            Image.new("RGB", (400, 300)).save(folder / (name + ".png"))
        report = self.scan(background_pairs={"new_light": "confirmed"})
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["learned_background_pairs"], {})
        self.assertTrue(any(d["code"] == "background.pair_conflict" for d in report["diagnostics"]))

    def test_background_missing_reference_warns_without_blocking(self):
        row = song_metadata()
        row["bg"] = "unknown_light"
        self.package(row=row)
        report = self.scan()
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["packages"][0]["songlist"]["bg_inverse"], "unknown_conflict")
        self.assertTrue(any(d["code"] == "background.missing" and d["level"] == "warning" for d in report["diagnostics"]))

    def test_background_references_can_be_supplied_by_a_later_package(self):
        row = song_metadata("first")
        row["bg"], row["bg_inverse"] = "new_light", "new_conflict"
        first = self.package("first", row)
        second = self.package("second", song_metadata("second"))
        Image.new("RGB", (1920, 1440)).save(first / "new_light.jpg")
        Image.new("RGB", (1280, 960)).save(second / "new_conflict.jpg")
        report = self.scan()
        self.assertTrue(report["valid"], report["errors"])
        self.assertFalse(any(d["code"] == "background.missing" for d in report["diagnostics"]))

    def test_master_initializes_undirected_pairs_and_excludes_ambiguous_groups(self):
        master = {"songs": [{"bg": "special", "bg_inverse": "other", "difficulties": []},
                             {"bg": "third", "bg_inverse": "other"},
                             {"bg": "amb_light", "bg_inverse": "one"}, {"bg": "amb_light", "bg_inverse": "two"},
                             {"bg": "safe_light", "bg_inverse": "safe_conflict"}]}
        pairs, conflicts = learn_pairs(master)
        self.assertEqual(pairs, {"safe_light": "safe_conflict"})
        self.assertEqual(conflicts["amb_light"], ["one", "two"])
        self.assertEqual(conflicts["other"], ["special", "third"])
        self.assertEqual(set(conflicts), {"amb_light", "one", "two", "special", "third", "other"})
        row = {"bg": "amb_light"}
        messages = []
        complete_inverse(row, pairs, lambda *entry: messages.append(entry), conflicts)
        self.assertNotIn("bg_inverse", row)
        self.assertEqual(messages[0][0], "warning")

    def test_difficulty_background_without_explicit_inverse_is_not_a_new_relationship(self):
        master = {"songs": [{"bg": "observer_light", "bg_inverse": "observer_conflict",
            "difficulties": [{"ratingClass": 3, "bg": "observer_conflict"}]}]}
        pairs, conflicts = learn_pairs(master)
        self.assertEqual(pairs, {"observer_light": "observer_conflict"})
        self.assertEqual(conflicts, {})

    def test_preprocess_source_negative_placeholder_keeps_existing_real_difficulty(self):
        baseline = song_metadata()
        baseline["difficulties"][0]["chartDesigner"] = "Existing real author"
        patch = song_metadata()
        patch["difficulties"] = [{"ratingClass": 2, "rating": -1, "chartDesigner": ""},
                                 {"ratingClass": 3, "rating": 10}]
        folder = self.package(row=patch)
        (folder / "2.aff").write_bytes(EMPTY)
        (folder / "3.aff").write_bytes(PLAYABLE)
        server = self.root / "download/example"
        server.mkdir(parents=True)
        (server / "2.aff").write_bytes(PLAYABLE)
        report = self.scan(master={"songs": [baseline]}, existing_songs=server.parent)
        self.assertTrue(report["valid"], report["errors"])
        difficulties = {d["ratingClass"]: d for d in report["packages"][0]["songlist"]["difficulties"]}
        self.assertEqual(difficulties[2]["rating"], baseline["difficulties"][0]["rating"])
        self.assertEqual(difficulties[2]["chartDesigner"], "Existing real author")
        self.assertEqual(difficulties[3]["rating"], 10)

    def test_settings_seed_current_master_and_keep_manual_override(self):
        master = {"songs": [{"bg": "special", "bg_inverse": "from_master"}]}
        (self.assets / "songs/songlist").write_text(json.dumps(master), encoding="utf-8")
        settings = default_settings()
        settings["local"]["assets_root"] = str(self.assets)
        seeded = effective_settings(settings, self.root)
        self.assertEqual(seeded["import"]["background_pairs"], {"special": "from_master"})
        self.assertTrue(seeded["import"]["background_pairs_initialized"])
        settings["import"]["background_pairs"] = {"special": "manual"}
        seeded = effective_settings(settings, self.root)
        self.assertEqual(seeded["import"]["background_pairs"]["special"], "manual")
        path = self.root / "settings.json"
        save_settings(path, seeded)
        self.assertTrue(load_settings(path)["import"]["background_pairs_initialized"])

    def test_merge_forces_version_without_conflict_and_preserves_existing_real_chart_metadata(self):
        master = {"songs": [{"id": "example", "idx": 7, "version": "old", "remote_dl": False,
                             "difficulties": [{"ratingClass": 2, "rating": 9, "version": "old", "chartDesigner": "Real"}]},
                            {"id": "untouched", "idx": 8, "version": "keep"}]}
        original = deepcopy(master)
        patch = {"id": "example", "version": "source", "remote_dl": False,
                 "difficulties": [{"ratingClass": 2, "rating": 0, "version": "source", "chartDesigner": ""},
                                  {"ratingClass": 3, "rating": 10, "version": "source"},
                                  {"ratingClass": 0, "rating": -1}]}
        merged, conflicts = merge_songlist(master, [patch], import_version="2026.11",
            actual_charts={"example": {2, 3}}, incoming_charts={"example": {3}})
        self.assertEqual(conflicts, [])
        result = merged["songs"][0]
        self.assertTrue(result["remote_dl"])
        self.assertEqual(result["version"], "2026.11")
        by_rating = {d["ratingClass"]: d for d in result["difficulties"]}
        self.assertEqual(by_rating[2]["chartDesigner"], "Real")
        self.assertEqual(by_rating[0]["rating"], 0)
        self.assertEqual(by_rating[2]["version"], "old")
        self.assertNotIn("version", by_rating[0])
        self.assertNotIn("version", by_rating[3])
        self.assertEqual(merged["songs"][1], original["songs"][1])
        self.assertEqual(master, original)

    def test_timing_only_server_placeholders_are_not_real_charts(self):
        folder = self.root / "download"
        folder.mkdir()
        (folder / "0.aff").write_bytes(EMPTY.rstrip().rstrip(b";"))
        (folder / "2.aff").write_bytes(PLAYABLE)
        self.assertEqual(actual_chart_classes(folder), {2})

    def test_source_diagnostic_callback_has_all_three_levels(self):
        self.package("renamed")
        folder = self.package("bad", song_metadata(""))
        # Missing bg_inverse on a nonstandard bg produces a warning in the good package.
        row = song_metadata()
        row["bg"] = "custom"
        (self.input / "renamed/songlist").write_text(json.dumps(row), encoding="utf-8")
        events = []
        report = self.scan(on_diagnostic=events.append)
        self.assertEqual({event["level"] for event in events}, {"info", "warning", "error"})
        self.assertFalse(report["valid"])
        self.assertTrue(any(d["level"] == "error" and d["source"] == str(folder) for d in events))
