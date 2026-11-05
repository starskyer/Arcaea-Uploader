import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image

from arcaea_uploader.media import MediaError, create_preview, normalize_jacket, preview_interval


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def test_already_sized_thumbnail_is_renamed_and_source_is_preserved(self):
        source = self.root / "base_256.jpg"
        Image.new("RGB", (384, 384), "blue").save(source)
        before = source.read_bytes()
        result = normalize_jacket(source, self.root / "work")
        self.assertEqual(result.name, "1080_base_256.jpg")
        self.assertEqual(result.read_bytes(), before)
        self.assertEqual(source.read_bytes(), before)

    def test_other_thumbnail_sizes_are_resized(self):
        source = self.root / "3_256.jpg"
        Image.new("RGB", (640, 640), "green").save(source)
        output = normalize_jacket(source, self.root / "work")
        with Image.open(output) as image:
            self.assertEqual(image.size, (384, 384))
        with Image.open(source) as image:
            self.assertEqual(image.size, (640, 640))

    def test_preview_times_use_sparse_rating_class_and_inherit_missing_fields(self):
        song = {"audioPreview": 100, "audioPreviewEnd": 2000,
                "difficulties": [{"ratingClass": 3, "audioPreviewEnd": 1500}]}
        self.assertEqual(preview_interval(song, 3), (100, 1500))
        self.assertEqual(preview_interval(song), (100, 2000))
        with self.assertRaises(MediaError):
            preview_interval({"audioPreview": 2000, "audioPreviewEnd": 100})

    def test_failed_conversion_does_not_replace_prior_preview_or_remove_source(self):
        source = self.root / "base.ogg"
        source.write_bytes(b"full audio")
        target = self.root / "preview.ogg"
        target.write_bytes(b"old good preview")
        with patch("arcaea_uploader.media.resolve_ffmpeg", return_value="ffmpeg"), \
             patch("arcaea_uploader.media._run", side_effect=MediaError("conversion failed")):
            with self.assertRaises(MediaError):
                create_preview(source, target, {"audioPreview": 0, "audioPreviewEnd": 1000}, replace=True)
        self.assertEqual(source.read_bytes(), b"full audio")
        self.assertEqual(target.read_bytes(), b"old good preview")
        self.assertEqual(list(self.root.glob(".preview-*")), [])

    def test_existing_preview_requires_explicit_regeneration(self):
        source = self.root / "base.ogg"
        source.write_bytes(b"full audio")
        target = self.root / "preview.ogg"
        target.write_bytes(b"existing")
        with self.assertRaises(FileExistsError):
            create_preview(source, target, {"audioPreview": 0, "audioPreviewEnd": 1000})
        with self.assertRaises(MediaError):
            create_preview(source, source, {"audioPreview": 0, "audioPreviewEnd": 1000}, replace=True)

    def test_real_ffmpeg_conversion_and_reviewed_preview_regeneration(self):
        ffmpeg = shutil.which(os.environ.get("ARCAEA_UPLOADER_TEST_FFMPEG", "ffmpeg"))
        if not ffmpeg:
            self.skipTest("Set ARCAEA_UPLOADER_TEST_FFMPEG or install FFmpeg for integration testing")
        source = self.root / "base.ogg"
        subprocess.run([ffmpeg, "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=3", "-c:a", "libvorbis", str(source)],
                       check=True, capture_output=True, timeout=30,
                       **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
        before = source.read_bytes()
        target = self.root / "preview.ogg"
        create_preview(source, target, {"audioPreview": 0, "audioPreviewEnd": 1000}, ffmpeg_path=ffmpeg)
        self.assertGreater(target.stat().st_size, 0)
        updated = create_preview(source, target, {"audioPreview": 500, "audioPreviewEnd": 1500},
                                 ffmpeg_path=ffmpeg, replace=True)
        self.assertEqual((updated.start_ms, updated.end_ms), (500, 1500))
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".preview-*")), [])
