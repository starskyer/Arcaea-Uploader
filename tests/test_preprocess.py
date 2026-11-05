from copy import deepcopy
from functools import partial
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image

from arcaea_uploader.charts import is_empty_aff
from arcaea_uploader.media import MediaError
from arcaea_uploader.planner import PlanError, load_package
from arcaea_uploader.preprocess import preprocess_collection as _preprocess_collection, read_author_metadata, scan_collection as _scan_collection

preprocess_collection = partial(_preprocess_collection, import_version="7.0")
scan_collection = partial(_scan_collection, import_version="7.0")


EMPTY = b"AudioOffset:0\n-\ntiming(0,100.00,4.00);\n"
PLAYABLE = EMPTY + b"(100,1);\n"


def song_metadata(song_id="example"):
    return {"id": song_id, "title_localized": {"en": "Example"}, "artist": "Artist", "bpm": "100",
            "bpm_base": 100, "set": "custom", "purchase": "", "audioPreview": 0,
            "audioPreviewEnd": 1000, "side": 0, "bg": "base_light", "date": 1, "version": "1.0",
            "difficulties": [{"ratingClass": 2, "rating": 9}]}


class EmptyChartTests(unittest.TestCase):
    def test_timing_only_templates_not_size_are_excluded(self):
        self.assertTrue(is_empty_aff(EMPTY))
        self.assertTrue(is_empty_aff(b"\n\t  "))
        self.assertTrue(is_empty_aff(b"\xef\xbb\xbf" + EMPTY.replace(b"\n", b"\r\n")))
        self.assertTrue(is_empty_aff(b"AudioOffset:0\n-\n" + b"timing(0,167,4);\n" * 100))
        self.assertFalse(is_empty_aff(PLAYABLE))
        self.assertFalse(is_empty_aff(EMPTY + b"hold(100,200,1);\n"))
        self.assertFalse(is_empty_aff(b"unknown syntax"))
        self.assertFalse(is_empty_aff(EMPTY + b"arc(1,2,0,1,s,0,1,0,none,false);"))


class PreprocessTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.incoming = self.root / "collected"
        self.incoming.mkdir()
        self.output = self.root / "releases"

    def tearDown(self):
        self.temp.cleanup()

    def package(self, relative="example", metadata=None, name="songlist"):
        folder = self.incoming / relative
        folder.mkdir(parents=True)
        (folder / "2.aff").write_bytes(PLAYABLE)
        (folder / "base.ogg").write_bytes(b"full audio")
        Image.new("RGB", (600, 600), "red").save(folder / "base.jpg")
        (folder / name).write_text(json.dumps(metadata or song_metadata(), ensure_ascii=False), encoding="utf-8-sig")
        return folder

    def snapshot(self):
        return {str(p.relative_to(self.incoming)): p.read_bytes() for p in self.incoming.rglob("*") if p.is_file()}

    def test_nested_alias_id_normalization_arcade_exclusion_and_source_preservation(self):
        folder = self.package("原始曲名/inner_name", name="slst.txt")
        arcade = folder / "Arcade"
        arcade.mkdir()
        (arcade / "3.aff").write_bytes(PLAYABLE)
        before = self.snapshot()
        report = preprocess_collection(self.incoming, self.output)
        self.assertTrue(report["valid"], report["errors"])
        output = Path(report["normalized_root"]) / "example"
        self.assertTrue(output.is_dir())
        self.assertFalse((output / "Arcade").exists())
        self.assertFalse((output / "3.aff").exists())
        self.assertTrue((output / "songlist").is_file())
        self.assertEqual(load_package(output)[0]["id"], "example")
        with Image.open(output / "base_256.jpg") as image:
            self.assertEqual(image.size, (384, 384))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(report["notices"][0]["code"], "ignored_arcade")
        self.assertTrue(json.loads(Path(report["report_file"]).read_text(encoding="utf-8"))["complete"])

    def test_mixed_case_standard_resource_names_are_normalized(self):
        folder = self.package(name="SLST.JSON")
        for old, new in (("2.aff", "2.AFF"), ("base.ogg", "BASE.OGG"), ("base.jpg", "BASE.JPG")):
            (folder / old).rename(folder / new)
        report = preprocess_collection(self.incoming, self.output)
        self.assertTrue(report["valid"], report["errors"])
        output = Path(report["normalized_root"]) / "example"
        self.assertTrue((output / "base.jpg").is_file())
        self.assertEqual(load_package(output)[0]["id"], "example")

    def test_strict_planner_also_ignores_empty_chart_patches(self):
        metadata = song_metadata()
        metadata["difficulties"].append({"ratingClass": 3, "rating": 10})
        folder = self.package(metadata=metadata)
        (folder / "3.aff").write_bytes(EMPTY)
        row, files, warnings = load_package(folder)
        self.assertNotIn("3.aff", [p.name for p in files])
        self.assertEqual([d["ratingClass"] for d in row["difficulties"]], [2, 3])
        self.assertEqual(row["difficulties"][1]["rating"], 0)
        self.assertTrue((folder / "3.aff").exists())

    def test_wrapper_metadata_and_media_are_combined_only_for_single_inner_package(self):
        folder = self.package("outer/inner")
        for name in ("songlist", "base.ogg", "base.jpg"):
            (folder / name).rename(folder.parent / name)
        report = preprocess_collection(self.incoming, self.output)
        self.assertTrue(report["valid"], report["errors"])
        self.assertTrue((Path(report["normalized_root"]) / "example" / "base.ogg").is_file())

    def test_shared_parent_metadata_is_not_guessed_for_multiple_children(self):
        for name in ("first", "second"):
            folder = self.package(f"outer/{name}", song_metadata(name))
            (folder / "songlist").unlink()
        (self.incoming / "outer" / "songlist").write_text(json.dumps(song_metadata()), encoding="utf-8")
        report = scan_collection(self.incoming, master={"songs": [song_metadata("first"), song_metadata("second")]})
        self.assertEqual(report["counts"], {"blocked": 2})
        self.assertTrue(all("上层 songlist" in p["errors"][0] for p in report["packages"]))

    def test_empty_chart_and_its_metadata_patch_are_removed_without_losing_real_chart(self):
        metadata = song_metadata()
        metadata["difficulties"].insert(0, {"ratingClass": 0, "rating": 1})
        folder = self.package(metadata=metadata)
        (folder / "0.aff").write_bytes(EMPTY)
        report = preprocess_collection(self.incoming, self.output)
        output = Path(report["normalized_root"]) / "example"
        self.assertFalse((output / "0.aff").exists())
        row, files, _ = load_package(output)
        self.assertEqual([d["ratingClass"] for d in row["difficulties"]], [0, 2])
        self.assertEqual(row["difficulties"][0]["rating"], 0)
        self.assertEqual((output / "2.aff").read_bytes(), PLAYABLE)
        self.assertIn(str(folder / "0.aff"), report["packages"][0]["ignored_aff"])

    def test_only_empty_chart_is_skipped(self):
        folder = self.package()
        (folder / "2.aff").write_bytes(EMPTY)
        report = preprocess_collection(self.incoming, self.output)
        self.assertEqual(report["counts"], {"ignored": 1})
        self.assertEqual(list(Path(report["normalized_root"]).iterdir()), [])
        self.assertFalse(report["valid"])

    def test_existing_master_fills_missing_values_but_preserves_supplied_values(self):
        folder = self.package(metadata={"id": "example", "artist": "New Artist",
                                        "difficulties": [{"ratingClass": 2, "rating": 10}]})
        master = {"songs": [song_metadata()]}
        original = deepcopy(master)
        report = preprocess_collection(self.incoming, self.output, master=master)
        self.assertTrue(report["valid"], report["errors"])
        row = report["packages"][0]["songlist"]
        self.assertEqual(row["artist"], "New Artist")
        self.assertEqual(row["title_localized"], {"en": "Example"})
        self.assertEqual(row["difficulties"][0]["rating"], 10)
        self.assertEqual(master, original)

    def test_new_song_missing_semantic_information_is_blocked(self):
        self.package(metadata={"id": "example", "difficulties": [{"ratingClass": 2}]})
        report = preprocess_collection(self.incoming, self.output)
        record = report["packages"][0]
        self.assertEqual(record["status"], "blocked")
        self.assertIn("artist", record["missing_fields"])
        self.assertIn("difficulties[2].rating", record["missing_fields"])
        self.assertFalse((Path(report["normalized_root"]) / "example").exists())

    def test_missing_songlist_is_error_even_when_folder_matches_master(self):
        folder = self.package()
        (folder / "songlist").unlink()
        report = preprocess_collection(self.incoming, self.output, master={"songs": [song_metadata()]})
        self.assertFalse(report["valid"])
        self.assertEqual(report["packages"][0]["status"], "blocked")

    def test_missing_id_is_error_instead_of_inferred_from_english_folder(self):
        metadata = song_metadata()
        metadata.pop("id")
        self.package(metadata=metadata)
        report = scan_collection(self.incoming)
        self.assertFalse(report["valid"])
        self.assertTrue(any("songlist.id" in error for error in report["errors"]))

    def test_invalid_id_is_not_sanitized_or_used_as_path(self):
        for name, song_id in (("folder_a", "../escape"), ("folder_b", "CON"), ("folder_c", "Song Name")):
            self.package(name, song_metadata(song_id))
        report = preprocess_collection(self.incoming, self.output)
        self.assertEqual(report["counts"], {"blocked": 3})
        self.assertEqual(list(Path(report["normalized_root"]).iterdir()), [])

    def test_duplicate_ids_are_reported_instead_of_overwritten(self):
        self.package("folder_a", song_metadata("example"))
        self.package("folder_b", song_metadata("EXAMPLE"))
        report = preprocess_collection(self.incoming, self.output)
        self.assertEqual(report["counts"], {"blocked": 2})
        self.assertEqual(list(Path(report["normalized_root"]).iterdir()), [])

    def test_audio_target_conflict_requires_manual_choice(self):
        folder = self.package()
        (folder / "base.wav").write_bytes(b"another audio")
        report = scan_collection(self.incoming)
        self.assertEqual(report["counts"], {"blocked": 1})
        self.assertIn("base.ogg", report["errors"][0])

    def test_single_nonstandard_audio_name_is_reported_as_main_audio(self):
        folder = self.package()
        (folder / "base.ogg").unlink()
        (folder / "the music.mp3").write_bytes(b"source mp3")
        report = scan_collection(self.incoming)
        self.assertTrue(report["valid"], report["errors"])
        action = next(a for a in report["packages"][0]["actions"] if a["target"] == "base.ogg")
        self.assertEqual(action["kind"], "transcode")
        self.assertTrue(any(d["code"] == "audio.main_inferred" and d["level"] == "warning" for d in report["diagnostics"]))

    def test_trailing_commas_are_fixed_without_changing_comma_like_title_text(self):
        folder = self.package()
        row = song_metadata()
        row["title_localized"]["en"] = 'Title,] and escaped "quote"'
        text = json.dumps(row).replace('"rating": 9}', '"rating": 9,}') + ","
        (folder / "songlist").write_text(text, encoding="utf-8")
        parsed, notes = read_author_metadata(folder / "songlist")
        self.assertEqual(parsed, row)
        self.assertIn("removed_trailing_commas", notes)

    def test_invalid_json_values_and_duplicate_keys_are_not_guessed(self):
        folder = self.package()
        for text in ('{"id":"example","rating":DJ}', '{"id":"example","id":"other"}'):
            (folder / "songlist").write_text(text, encoding="utf-8")
            report = scan_collection(self.incoming)
            self.assertEqual(report["counts"], {"blocked": 1})

    def test_gb18030_metadata_is_converted_and_reported(self):
        folder = self.package()
        row = song_metadata()
        row["title_localized"]["en"] = "中文曲名"
        (folder / "songlist").write_bytes(json.dumps(row, ensure_ascii=False).encode("gb18030"))
        parsed, notes = read_author_metadata(folder / "songlist")
        self.assertEqual(parsed, row)
        self.assertIn("converted_metadata_encoding:gb18030", notes)

    def test_read_only_scan_does_not_create_output_or_modify_sources(self):
        self.package()
        before = self.snapshot()
        self.assertTrue(scan_collection(self.incoming)["valid"])
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.output.exists())

    def test_output_inside_source_is_rejected(self):
        self.package()
        with self.assertRaises(PlanError):
            preprocess_collection(self.incoming, self.incoming / "work")
        self.assertFalse((self.incoming / "work").exists())

    def test_repeated_runs_use_separate_batches(self):
        self.package()
        first = preprocess_collection(self.incoming, self.output)
        second = preprocess_collection(self.incoming, self.output)
        self.assertNotEqual(first["batch_root"], second["batch_root"])
        self.assertTrue(Path(first["report_file"]).exists())

    def test_failed_transcode_does_not_publish_a_partial_song(self):
        folder = self.package()
        (folder / "base.ogg").rename(folder / "base.wav")
        before = self.snapshot()
        with patch("arcaea_uploader.preprocess.transcode_audio", side_effect=MediaError("conversion failed")):
            report = preprocess_collection(self.incoming, self.output)
        self.assertEqual(report["counts"], {"blocked": 1})
        self.assertFalse((Path(report["normalized_root"]) / "example").exists())
        self.assertEqual(self.snapshot(), before)

    def test_metadata_only_folder_is_blocked_even_with_other_valid_songs(self):
        self.package()
        folder = self.incoming / "other"
        folder.mkdir()
        (folder / "songlist").write_text(json.dumps(song_metadata("other")), encoding="utf-8")
        report = scan_collection(self.incoming)
        self.assertFalse(report["valid"])
        self.assertEqual(report["counts"], {"ready": 1, "blocked": 1})
        self.assertTrue(report["errors"])

    def test_cli_scan_needs_no_server_and_writes_no_normalized_files(self):
        self.package()
        config = self.root / "config.json"
        config.write_text(json.dumps({"import": {"version": "7.0"}, "local": {"input_root": str(self.incoming), "release_root": str(self.output)}}), encoding="utf-8")
        result = subprocess.run([sys.executable, "-m", "arcaea_uploader", "preprocess", "--scan-only", "--config", str(config)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["mode"], "preprocess_scan")
        self.assertFalse(self.output.exists())

    def test_real_wav_transcode_keeps_original_and_produces_standard_package(self):
        ffmpeg = shutil.which(os.environ.get("ARCAEA_UPLOADER_TEST_FFMPEG", "ffmpeg"))
        if not ffmpeg:
            self.skipTest("FFmpeg not configured")
        folder = self.package()
        (folder / "base.ogg").unlink()
        wav = folder / "base.wav"
        subprocess.run([ffmpeg, "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(wav)],
                       check=True, capture_output=True, timeout=30,
                       **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
        before = self.snapshot()
        report = preprocess_collection(self.incoming, self.output, ffmpeg_path=ffmpeg)
        self.assertTrue(report["valid"], report["errors"])
        output = Path(report["normalized_root"]) / "example"
        self.assertGreater((output / "base.ogg").stat().st_size, 0)
        self.assertFalse((output / "base.wav").exists())
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(load_package(output)[0]["id"], "example")
