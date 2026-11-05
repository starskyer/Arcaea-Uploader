"""Media helpers adapted from the maintainer's resize_and_preview.py.

The original snapshot and change record live in _legacy/. Helpers operate only
on explicitly supplied work files; no whole-assets traversal or implicit deletion.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory

from PIL import Image


class MediaError(ValueError):
    """Invalid media settings or failed FFmpeg processing."""


def resolve_ffmpeg(configured: str = "") -> str:
    value = configured.strip() or "ffmpeg"
    found = shutil.which(value)
    if not found:
        raise MediaError("找不到 FFmpeg，请在配置页指定可执行文件，或将 FFmpeg 加入 PATH。")
    return found


def transcode_audio(source: Path, target: Path, *, ffmpeg_path: str = "") -> Path:
    """Convert a full MP3/WAV to Vorbis, preserving the original input."""
    source, target = Path(source), Path(target)
    if source.resolve() == target.resolve():
        raise MediaError("音频源文件与转码输出必须分开。")
    if target.exists():
        raise FileExistsError(target)
    ffmpeg = resolve_ffmpeg(ffmpeg_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".transcode-", dir=target.parent) as temp:
        output = Path(temp) / "audio.ogg"
        _run(ffmpeg, ["-i", str(source), "-vn", "-c:a", "libvorbis", "-q:a", "6", "-y", str(output)])
        if not output.is_file() or not output.stat().st_size:
            raise MediaError("未生成有效的 OGG 音频。")
        _run(ffmpeg, ["-i", str(output), "-f", "null", "-"])
        output.replace(target)
    return target


def _run(ffmpeg: str, args: list[str], timeout: int = 120):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", *args],
                       check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, **options)
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise MediaError(f"FFmpeg 处理失败：{detail[-2000:]}") from exc


def normalize_jacket(source: Path, destination: Path, *, replace: bool = False) -> Path:
    """Normalize a work-copy jacket; resize thumbnails and keep source intact.

Independent full-size jackets retain their names until their client convention
is finalized. They are not implicitly renamed to an invented 1080_* form.
"""
    source, destination = Path(source), Path(destination)
    if not re.fullmatch(r"(?:base|[0-4])(?:_256)?\.jpg", source.name):
        raise MediaError(f"Not a jacket filename: {source.name}")
    name = {"base.jpg": "1080_base.jpg", "base_256.jpg": "1080_base_256.jpg"}.get(source.name, source.name)
    target = destination / name
    if source.resolve() == target.resolve():
        raise MediaError("曲绘源文件与输出文件必须分开。")
    if target.exists() and not replace:
        raise FileExistsError(target)
    destination.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".jacket-", dir=destination) as temp:
        staged = Path(temp) / name
        with Image.open(source) as image:
            if source.stem.endswith("_256") and image.size != (384, 384):
                image.resize((384, 384), Image.Resampling.LANCZOS).save(staged, format=image.format, quality=90, optimize=True)
            else:
                image.verify()
                shutil.copyfile(source, staged)
        staged.replace(target)
    return target


def make_thumbnail(source: Path, target: Path):
    """Create a missing thumbnail while preserving the source image encoding.

    An extension/encoding difference is deliberately neither fixed nor logged.
    Existing files are copied by preprocessing; resizing remains an explicit job.
    """
    source, target = Path(source), Path(target)
    if target.exists() or source.resolve() == target.resolve():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".thumbnail-", dir=target.parent) as temp:
        staged = Path(temp) / target.name
        with Image.open(source) as image:
            image.resize((384, 384), Image.Resampling.LANCZOS).save(staged, format=image.format, quality=90, optimize=True)
        staged.replace(target)
    return target


def preview_interval(song: dict, rating_class: int | None = None) -> tuple[int, int]:
    if rating_class is not None and (type(rating_class) is not int or rating_class not in range(5)):
        raise MediaError("ratingClass 必须是 0–4。")
    matches = [d for d in song.get("difficulties", []) if d.get("ratingClass") == rating_class]
    if len(matches) > 1:
        raise MediaError("songlist 中存在重复 ratingClass。")
    difficulty = matches[0] if rating_class is not None and matches else {}
    start = difficulty.get("audioPreview", song.get("audioPreview"))
    end = difficulty.get("audioPreviewEnd", song.get("audioPreviewEnd"))
    if type(start) is not int or type(end) is not int or start < 0 or end <= start:
        raise MediaError("预览起止时间必须是非负毫秒整数，且结束时间大于开始时间。")
    return start, end


@dataclass(frozen=True)
class PreviewResult:
    file: Path
    start_ms: int
    end_ms: int


def create_preview(source: Path, target: Path, song: dict, *, rating_class: int | None = None,
                   ffmpeg_path: str = "", replace: bool = False) -> PreviewResult:
    """Generate and validate a preview; full source audio is never removed.

The caller supplies the output filename, so unresolved independent-audio client
filenames are not guessed here. Existing previews are regenerated only on an
explicit replace request after reviewing the final songlist.
"""
    source, target = Path(source), Path(target)
    if source.resolve() == target.resolve():
        raise MediaError("预览不能覆盖完整源音频。")
    if not source.is_file():
        raise FileNotFoundError(source)
    if target.exists() and not replace:
        raise FileExistsError(target)
    start, end = preview_interval(song, rating_class)
    ffmpeg = resolve_ffmpeg(ffmpeg_path)
    duration = (end - start) / 1000
    target.parent.mkdir(parents=True, exist_ok=True)
    # Preserve the original two-stage PCM -> faded Vorbis workflow.
    with TemporaryDirectory(prefix=".preview-", dir=target.parent) as temp:
        wav = Path(temp) / "clip.wav"
        preview = Path(temp) / "preview.ogg"
        _run(ffmpeg, ["-i", str(source), "-ss", str(start / 1000), "-to", str(end / 1000),
                      "-vn", "-c:a", "pcm_s16le", "-y", str(wav)])
        fade = f"afade=t=in:st=0:d=0.5,afade=t=out:st={max(0, duration - 2)}:d=2"
        _run(ffmpeg, ["-i", str(wav), "-af", fade, "-c:a", "libvorbis", "-y", str(preview)])
        if not preview.is_file() or not preview.stat().st_size:
            raise MediaError("FFmpeg 未生成有效的预览文件。")
        _run(ffmpeg, ["-i", str(preview), "-f", "null", "-"])
        preview.replace(target)
    return PreviewResult(target, start, end)
