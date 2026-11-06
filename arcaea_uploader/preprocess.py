"""Discover heterogeneous chart collections and produce standard work copies.

No input file, production assets or server database is modified. Ambiguous
packages remain in the report and never silently enter the normalized batch.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
from tempfile import mkdtemp

from .charts import is_empty_aff, hitsound_files
from .media import transcode_audio, make_thumbnail
from .backgrounds import BackgroundIndex, complete_inverse, check_references, split_pairs, learn_pairs, merge_pairs, pair_lookup
from .diagnostics import emit, summarize
from .import_rules import portable_id, require_version, normalize_import_row, actual_chart_classes
from .metadata import read_author_metadata, strip_trailing_commas as _strip_trailing_commas
from .planner import PlanError, check_difficulties, digest, read_json, song_rows
from .workspaces import (ensure_plain_tree, initialize_workspace, load_workspace,
                         timestamp, workspace_lock, write_json)


METADATA_NAMES = {base + suffix for base in ("songlist", "slst") for suffix in ("", ".json", ".txt")}
_CHART = re.compile(r"[0-4]\.aff", re.I)
REQUIRED_FIELDS = ("artist", "bpm", "bpm_base", "set", "purchase", "audioPreview", "audioPreviewEnd",
                   "side", "bg", "date")


def _linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0)
                                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _discover(root: Path) -> tuple[list[Path], list[dict]]:
    folders, metadata_folders, notices = [], [], []
    for current, dirs, files in os.walk(root, followlinks=False):
        folder = Path(current)
        safe_dirs = []
        for name in sorted(dirs, key=str.casefold):
            path = folder / name
            if name.casefold() == "arcade":
                notices.append({"code": "ignored_arcade", "source": str(path)})
            elif _linked(path):
                notices.append({"code": "ignored_link", "source": str(path)})
            elif len(path.relative_to(root).parts) > 20:
                notices.append({"code": "directory_too_deep", "source": str(path)})
            else:
                safe_dirs.append(name)
        dirs[:] = safe_dirs
        if any(name.lower().endswith(".aff") for name in files):
            folders.append(folder)
        if any(name.casefold() in METADATA_NAMES for name in files):
            metadata_folders.append(folder)
    folders.extend(p for p in metadata_folders if not any(f.is_relative_to(p) for f in folders))
    return sorted(set(folders), key=lambda p: str(p).casefold()), notices


def _inherit_missing(patch: dict, baseline: dict, prefix="") -> list[str]:
    inherited = []
    for key, value in baseline.items():
        if key == "difficulties":
            # Do not import unrelated baseline difficulty entries into a patch.
            continue
        if key == "bg_inverse" and patch.get("bg") and patch.get("bg") != baseline.get("bg"):
            continue
        name = f"{prefix}.{key}" if prefix else key
        if key not in patch:
            patch[key] = deepcopy(value)
            inherited.append(name)
        elif isinstance(patch[key], dict) and isinstance(value, dict):
            inherited.extend(_inherit_missing(patch[key], value, name))
    return inherited


def scan_collection(input_root: Path, *, master: dict | None = None, import_version="", assets_root=None,
                    existing_songs=None, background_pairs=None, background_pair_conflicts=None,
                    initialize_background_pairs=True, on_diagnostic=None) -> dict:
    import_version = require_version(import_version)
    background_pairs, detected_conflicts = split_pairs(background_pairs or {})
    pair_conflicts = {**(background_pair_conflicts or {}), **detected_conflicts}
    if master and initialize_background_pairs:
        learned, master_conflicts = learn_pairs(master)
        pair_conflicts = {**pair_conflicts, **master_conflicts}
        background_pairs = merge_pairs(learned, background_pairs)
    paired_ids = pair_lookup(background_pairs)
    backgrounds = BackgroundIndex(assets_root)
    root = Path(input_root).resolve(strict=True)
    if not root.is_dir():
        raise PlanError("待更新路径必须是目录。")
    candidates, notices = _discover(root)
    baseline = {}
    if master is not None:
        for row in song_rows(master):
            song_id = row.get("id")
            if not portable_id(song_id) or song_id in baseline:
                raise PlanError("已有总 songlist 含无效或重复 ID，无法用于自动补全。")
            baseline[song_id] = row
    report = {"schema_version": 1, "mode": "preprocess_scan", "input_root": str(root),
              "packages": [], "notices": notices, "errors": [], "publishable": False,
              "diagnostics": [], "import_version": import_version}
    emit(report["diagnostics"], "info", "background.table", f"使用背景对应表 {len(background_pairs)} 组，均可双向推导。", callback=on_diagnostic)
    for bg, choices in pair_conflicts.items():
        if bg not in paired_ids:
            emit(report["diagnostics"], "warning", "background.table_conflict",
                 f"背景 {bg} 属于无法一一配对的关系组，关联 {choices}，未自动选定。", callback=on_diagnostic)
    for notice in notices:
        level = "info" if notice["code"] == "ignored_arcade" else "warning"
        emit(report["diagnostics"], level, notice["code"],
             "忽略 Arcade 工程目录。" if level == "info" else "已跳过链接或过深目录，请确认输入范围。",
             source=notice["source"], callback=on_diagnostic)
    for folder in candidates:
        record = {"source": str(folder), "id": None, "status": "ready", "errors": [], "warnings": [],
                  "actions": [], "ignored_aff": [], "missing_fields": [], "songlist": None, "diagnostics": []}
        report["packages"].append(record)
        def log(level, code, message):
            emit(record["diagnostics"], level, code, message, song_id=record["id"], source=folder, callback=on_diagnostic)
        try:
            # Include wrapper files only when that wrapper contains one candidate.
            sources = [folder]
            parent = folder.parent
            while parent.is_relative_to(root) and parent != root and parent not in candidates:
                if sum(p.is_relative_to(parent) for p in candidates) != 1:
                    if any(p.is_file() and p.name.casefold() in METADATA_NAMES for p in parent.iterdir()):
                        record["errors"].append("上层 songlist 对应多个子曲目目录，需人工确定所属关系。")
                    break
                sources.append(parent)
                parent = parent.parent
            files = []
            for source in sources:
                for path in sorted(source.iterdir(), key=lambda p: p.name.casefold()):
                    if path.is_file() and not _linked(path):
                        files.append(path)
            chart_files = [p for p in files if p.suffix.casefold() == ".aff"]
            empty_files = {p for p in chart_files if _CHART.fullmatch(p.name) and is_empty_aff(p.read_bytes())}
            record["ignored_aff"] = [str(p) for p in sorted(empty_files)]
            metadata = [p for p in files if p.name.casefold() in METADATA_NAMES]
            if len(metadata) > 1:
                raise PlanError("找到多份 songlist/slst，需人工选定。")
            if metadata:
                before = digest(metadata[0])
                patch, notes = read_author_metadata(metadata[0])
                if digest(metadata[0]) != before:
                    raise PlanError("读取时 songlist 发生变化，请重试。")
                record["metadata_snapshot"] = {"source": str(metadata[0]), "sha256": before}
            else:
                raise PlanError("缺少 songlist/slst，无法取得明确的 songlist.id；不会根据目录名推断 ID。")
            for note in notes:
                log("warning", "metadata.recovered", "已恢复完整单曲 JSON 对象或兼容原始文本格式，请人工复核：" + note)
            sid = patch.get("id")
            record["id"] = sid
            if not portable_id(sid):
                raise PlanError("songlist.id 为空或不符合英文资源 ID 规范；不会改写 ID 或重命名原目录。")
            if folder.name != sid:
                log("info", "package.rename_planned", f"标准副本目录将由 {folder.name} 归一为有效 songlist.id：{sid}。")
            if metadata and metadata[0].name != "songlist":
                log("info", "metadata.filename", f"元数据文件名 {metadata[0].name} 归一为 songlist。")
            for path in sorted(empty_files):
                log("info", "chart.empty", f"排除默认空 AFF：{path.name}；对应声明难度作为占位处理。")
            if chart_files and len(empty_files) == len(chart_files):
                record["status"] = "ignored"
                log("info", "chart.no_update", "只有默认空谱，本包不作为实际更新。")
                continue

            actions = {}

            def add(path: Path, target: str, kind="copy"):
                if target.casefold() in {name.casefold() for name in actions}:
                    raise PlanError(f"多个源文件对应同一目标 {target}，需人工选择。")
                actions[target] = {"kind": kind, "source": str(path), "target": target,
                                   "source_sha256": digest(path)}

            real_ratings = set()
            unknown_audio = []
            sound_files = {name.casefold() for path in chart_files for name in hitsound_files(path.read_bytes())}
            known_main = any(p.stem.casefold() == "base" and p.suffix.casefold() in {".ogg", ".mp3", ".wav"} for p in files)
            record["sound_effects"] = sorted(sound_files)
            for path in files:
                name = path.name.casefold()
                if path in metadata:
                    continue
                if path.suffix.casefold() == ".aff":
                    if not _CHART.fullmatch(name):
                        record["errors"].append(f"谱面文件名无法映射到 0–4：{path.name}")
                    elif path in empty_files:
                        pass
                    else:
                        add(path, name)
                        real_ratings.add(int(path.stem))
                elif path.suffix.casefold() in {".ogg", ".mp3", ".wav"}:
                    if path.suffix.casefold() == ".wav" and (name in sound_files or
                        (known_main and not re.fullmatch(r"(?:base|[0-4])\.wav", name))):
                        log("warning", "audio.hitsound_excluded", f"特殊音效 {path.name} 不导入 bundle 或下载 songs，谱面引用保留，请人工处理。")
                        continue
                    if re.fullmatch(r"(?:base|[0-4])\.(?:ogg|mp3|wav)", name):
                        add(path, path.stem.casefold() + ".ogg", "copy" if path.suffix.casefold() == ".ogg" else "transcode")
                    elif name == "preview.ogg" or name.endswith("_preview.ogg"):
                        log("info", "audio.preview_ignored", f"跳过已有预览 {path.name}，后续按审核后的时间生成。")
                    else:
                        unknown_audio.append(path)
                elif path.suffix.casefold() == ".jpg":
                    target = {"1080_base.jpg": "base.jpg", "1080_base_256.jpg": "base_256.jpg"}.get(name, path.stem + ".jpg")
                    if re.fullmatch(r"(?:base|[0-4])(?:_256)?\.jpg", name):
                        target = name
                    if not re.fullmatch(r"(?:base|[0-4])(?:_256)?\.jpg", target):
                        route = backgrounds.route(path, log)
                        if route is None:
                            continue
                        add(path, target)
                        actions[target]["background_target"] = route
                        continue
                    # Do not inspect or report suffix/encoding mismatches.
                    add(path, target)
                elif name == "video_1080.mp4":
                    add(path, name)
                else:
                    log("info", "file.ignored", f"忽略非导入文件 {path.name}。")
            available_names = {p.name.casefold() for p in files}
            for name in sorted(sound_files - available_names):
                log("warning", "audio.hitsound_missing", f"谱面引用特殊音效 {name}，本包未找到该文件；音效分发需人工处理。")
            if unknown_audio:
                if len(unknown_audio) == 1 and "base.ogg" not in actions:
                    path = unknown_audio[0]
                    add(path, "base.ogg", "copy" if path.suffix.casefold() == ".ogg" else "transcode")
                    log("warning", "audio.main_inferred", f"唯一未命名音频 {path.name} 将作为 base.ogg，请人工确认。")
                else:
                    record["errors"].append("存在无法确定用途的多份音频，需人工选择完整主音频。")
            if not real_ratings:
                record["status"] = "ignored" if record["ignored_aff"] and not record["errors"] else "blocked"
                if record["status"] == "blocked":
                    record["errors"].append("没有可用于更新的非空 AFF 谱面。")
                record["actions"] = list(actions.values())
                continue
            # Keep declared placeholder slots; normalize their rating after
            # collecting the actual incoming and existing chart availability.
            check_difficulties(patch)
            patch.setdefault("difficulties", [])
            existing_difficulties = {d["ratingClass"]: d for d in baseline.get(sid, {}).get("difficulties", [])}
            existing_actual = actual_chart_classes(Path(existing_songs) / sid if existing_songs else None)
            supplied = {d["ratingClass"] for d in patch["difficulties"]}
            for difficulty in patch["difficulties"]:
                rating = difficulty["ratingClass"]
                if (rating not in real_ratings and rating in existing_actual and rating in existing_difficulties
                        and difficulty.get("rating", 0) <= 0):
                    difficulty.clear()
                    difficulty.update(deepcopy(existing_difficulties[rating]))
                    log("info", "metadata.keep_existing_chart", f"ratingClass {rating} 本次仅为占位，保留已有真实谱面的描述。")
                if rating in existing_difficulties:
                    inherited = _inherit_missing(difficulty, existing_difficulties[rating])
                    for field in inherited:
                        log("info", "metadata.inherited", f"从已有总表补全难度 {difficulty['ratingClass']} 的 {field}。")
            for rating in sorted(real_ratings - supplied):
                if rating in existing_difficulties:
                    patch["difficulties"].append(deepcopy(existing_difficulties[rating]))
                    log("info", "metadata.inherited", f"从已有总表补全实际难度 ratingClass {rating} 的描述。")
                else:
                    record["missing_fields"].append(f"difficulties[{rating}]")
            if sid in baseline:
                inherited = _inherit_missing(patch, baseline[sid])
                for field in inherited:
                    log("info", "metadata.inherited", f"从已有总表补全缺失字段 {field}。")
            else:
                record["missing_fields"].extend(key for key in REQUIRED_FIELDS if key not in patch or patch[key] is None
                                                 or (key != "purchase" and patch[key] == ""))
                if not isinstance(patch.get("title_localized"), dict) or not patch["title_localized"].get("en"):
                    record["missing_fields"].append("title_localized.en")
                if "base.ogg" not in actions:
                    record["errors"].append("新曲目缺少完整主音频。")
                if "base.jpg" not in actions:
                    record["errors"].append("新曲目缺少 base.jpg 曲绘。")
            if "base.jpg" in actions and "base_256.jpg" not in actions:
                source = Path(actions["base.jpg"]["source"])
                add(source, "base_256.jpg", "thumbnail")
            for rating in range(5):
                large, small = f"{rating}.jpg", f"{rating}_256.jpg"
                if large in actions and small not in actions:
                    add(Path(actions[large]["source"]), small, "thumbnail")
            for difficulty in patch["difficulties"]:
                if difficulty["ratingClass"] in real_ratings and "rating" not in difficulty:
                    record["missing_fields"].append(f"difficulties[{difficulty['ratingClass']}].rating")
            effective_actual = real_ratings | existing_actual
            normalize_import_row(patch, import_version, effective_actual, log)
            complete_inverse(patch, background_pairs, log, pair_conflicts)
            record["songlist"] = patch
            record["actions"] = list(actions.values())
            if record["missing_fields"]:
                record["errors"].append("缺少需人工补全的信息：" + ", ".join(record["missing_fields"]))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            record["errors"].append(str(exc))
        if record["errors"] or record["status"] == "blocked":
            record["status"] = "blocked"
    ids = Counter(p["id"].casefold() for p in report["packages"] if isinstance(p["id"], str))
    for record in report["packages"]:
        if isinstance(record["id"], str) and ids[record["id"].casefold()] > 1:
            record["status"] = "blocked"
            record["errors"].append("多个来源对应同一个曲目 ID，不会自动覆盖或拼接。")
    claims = {}
    for record in report["packages"]:
        if record["status"] != "ready":
            continue
        for action in record["actions"]:
            if "background_target" not in action:
                continue
            key = Path(action["background_target"]).name.casefold()
            claims.setdefault(key, []).append((record, action))
    for key, entries in claims.items():
        if len({a["source_sha256"] for _, a in entries}) > 1:
            for record, _ in entries:
                record["errors"].append(f"本批多个来源提供不同内容的同名背景 {key}，无法自动选择。")
                record["status"] = "blocked"
    available_bg = backgrounds.existing_ids | {Path(a["background_target"]).stem.casefold()
        for r in report["packages"] if r["status"] == "ready" for a in r["actions"] if "background_target" in a}
    for record in report["packages"]:
        def record_log(level, code, message):
            emit(record["diagnostics"], level, code, message, song_id=record["id"], source=record["source"], callback=on_diagnostic)
        if record["songlist"]:
            check_references(record["songlist"], available_bg, record_log)
        for error in record["errors"]:
            record_log("error", "package.invalid", error)
        summarize(record)
        report["diagnostics"].extend(record["diagnostics"])
    if not candidates:
        emit(report["diagnostics"], "error", "input.empty", "没有找到包含 AFF 或 songlist/slst 的曲目包。", callback=on_diagnostic)
    summarize(report)
    report["counts"] = dict(Counter(p["status"] for p in report["packages"]))
    report["valid"] = not report["errors"] and bool(report["counts"].get("ready"))
    return report


def preprocess_collection(input_root: Path, output_base: Path, *, master: dict | None = None,
                          ffmpeg_path: str = "", import_version="", assets_root=None, existing_songs=None,
                          background_pairs=None, background_pair_conflicts=None, initialize_background_pairs=True,
                          on_diagnostic=None, resume_from: Path | None = None) -> dict:
    """Create or resume one batch; reuse only hash-verified, unchanged packages."""
    root, output_base = Path(input_root).resolve(strict=True), Path(output_base).resolve()
    if output_base.is_relative_to(root):
        raise PlanError("预处理输出必须放在原始收集目录之外，避免下一次扫描重复导入。")
    report = scan_collection(root, master=master, import_version=import_version, assets_root=assets_root,
                             existing_songs=existing_songs, background_pairs=background_pairs,
                             background_pair_conflicts=background_pair_conflicts, initialize_background_pairs=initialize_background_pairs,
                             on_diagnostic=on_diagnostic)
    if resume_from is None:
        output_base.mkdir(parents=True, exist_ok=True)
        batch = Path(mkdtemp(prefix="preprocessed-", dir=output_base))
        initialize_workspace(batch, root, import_version)
    else:
        previous = load_workspace(output_base, resume_from)
        if Path(previous["input_root"]).resolve() != root:
            raise PlanError("继续处理必须使用该批次原来的输入根目录。")
        batch = Path(resume_from).resolve()
    context = {"master": master, "import_version": import_version, "assets_root": assets_root,
               "existing_songs": existing_songs, "background_pairs": background_pairs,
               "background_pair_conflicts": background_pair_conflicts,
               "initialize_background_pairs": initialize_background_pairs, "ffmpeg_path": ffmpeg_path}
    context_hash = _fingerprint(context)
    with workspace_lock(output_base, batch):
        previous = load_workspace(output_base, batch) if resume_from is not None else None
        ensure_plain_tree(batch)
        if not (batch / "workspace.json").exists():
            initialize_workspace(batch, root, import_version)
        return _materialize_batch(report, batch, previous, context_hash, ffmpeg_path, on_diagnostic)


def _fingerprint(data):
    return sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False,
                             default=str).encode("utf-8")).hexdigest()


def _package_fingerprint(record):
    return _fingerprint({key: record.get(key) for key in ("source", "id", "songlist", "actions", "metadata_snapshot")})


def _output_manifest(folder):
    return {path.name: digest(path) for path in sorted(folder.iterdir()) if path.is_file()}


def _reusable(record, old, folder, same_context):
    if (not same_context or record["status"] != "ready" or old.get("status") != "normalized"
            or old.get("package_fingerprint") != _package_fingerprint(record)
            or not folder.is_dir() or not old.get("output_sha256")):
        return False
    return (all(path.is_file() for path in folder.iterdir())
            and _output_manifest(folder) == old["output_sha256"])


def _materialize_batch(report, batch, previous, context_hash, ffmpeg_path, on_diagnostic):
    songs = batch / "songs"
    songs.mkdir(exist_ok=True)
    report.update(mode="preprocess", complete=False, valid=False, batch_root=str(batch), normalized_root=str(songs),
                  report_file=str(batch / "report.json"), context_sha256=context_hash,
                  updated_at=timestamp(), resumed=previous is not None)

    reusable = {}
    if previous:
        old_records = {p.get("source"): p for p in previous["packages"]}
        for record in report["packages"]:
            folder = songs / (record["id"] if portable_id(record["id"]) else "__invalid__")
            old = old_records.get(record["source"], {})
            if _reusable(record, old, folder, previous.get("context_sha256") == context_hash):
                reusable[record["id"]] = old
        emit(report["diagnostics"], "info", "workspace.resumed",
             f"继续批次 {batch.name}；可复用 {len(reusable)} 首已核验副本，其余重新处理。", callback=on_diagnostic)
        # Stale or manually edited copies stay available for inspection but must
        # not enter the new songs/ set after a changed or removed input package.
        stale = [path for path in songs.iterdir() if path.name not in reusable]
        if stale:
            archive = batch / "work"
            archive.mkdir(exist_ok=True)
            archive = Path(mkdtemp(prefix="previous-", dir=archive))
            for path in stale:
                destination = archive / path.name
                if path.resolve().parent != songs or not destination.resolve().is_relative_to(batch):
                    raise PlanError("旧工作副本路径越界。")
                path.rename(destination)
            emit(report["diagnostics"], "warning", "workspace.previous_saved",
                 f"{len(stale)} 份旧副本未复用，已保留在 {archive}；请核对其中人工编辑。", callback=on_diagnostic)

    def write_report():
        summarize(report)
        report["counts"] = dict(Counter(p["status"] for p in report["packages"]))
        report["updated_at"] = timestamp()
        write_json(Path(report["report_file"]), report)

    write_report()
    for index, record in enumerate(report["packages"]):
        if record["status"] != "ready":
            continue
        if record["id"] in reusable:
            snapshots = [(action["source"], action["source_sha256"]) for action in record["actions"]]
            if record.get("metadata_snapshot"):
                snapshots.append((record["metadata_snapshot"]["source"], record["metadata_snapshot"]["sha256"]))
            if any(digest(Path(source)) != expected for source, expected in snapshots):
                raise PlanError("继续处理期间源文件发生变化，请重新继续该批次。")
            record.update(status="normalized", output=str(songs / record["id"]),
                          package_fingerprint=_package_fingerprint(record),
                          output_sha256=reusable[record["id"]]["output_sha256"], reused=True)
            entry = emit(record["diagnostics"], "info", "package.reused", "复用已核验的标准副本，无需再次复制或转码。",
                         song_id=record["id"], source=record["source"], callback=on_diagnostic)
            report["diagnostics"].append(entry)
            write_report()
            continue
        work_root = batch / "work"
        work_root.mkdir(exist_ok=True)
        work = Path(mkdtemp(prefix=f"{index}-", dir=work_root))
        try:
            metadata_snapshot = record.get("metadata_snapshot")
            if metadata_snapshot and digest(Path(metadata_snapshot["source"])) != metadata_snapshot["sha256"]:
                raise PlanError("预处理期间 songlist 发生变化，请重试。")
            # Pin each input snapshot before consuming it, to avoid mixing edits.
            for action in record["actions"]:
                source = Path(action["source"])
                if digest(source) != action["source_sha256"]:
                    raise PlanError(f"预处理期间源文件发生变化：{source}")
                target = work / action["target"]
                if target.parent != work:
                    raise PlanError("无效的工作副本文件名。")
                if action["kind"] == "thumbnail":
                    make_thumbnail(source, target)
                elif action["kind"] == "transcode":
                    transcode_audio(source, target, ffmpeg_path=ffmpeg_path)
                else:
                    shutil.copyfile(source, target)
                if digest(source) != action["source_sha256"]:
                    raise PlanError(f"处理时源文件发生变化：{source}")
            (work / "songlist").write_text(json.dumps({"songs": [record["songlist"]]}, ensure_ascii=False,
                                                       indent=2, allow_nan=False) + "\n", encoding="utf-8")
            if metadata_snapshot and digest(Path(metadata_snapshot["source"])) != metadata_snapshot["sha256"]:
                raise PlanError("处理时 songlist 发生变化，请重试。")
            target = songs / record["id"]
            if not work.resolve().is_relative_to(batch.resolve()) or not target.resolve().is_relative_to(songs.resolve()):
                raise PlanError("工作副本路径越出本次输出目录。")
            work.rename(target)
            record["output"] = str(target)
            record["status"] = "normalized"
            record["package_fingerprint"] = _package_fingerprint(record)
            record["output_sha256"] = _output_manifest(target)
            entry = emit(record["diagnostics"], "info", "package.written", f"标准副本已完成：{target}",
                         song_id=record["id"], source=record["source"], callback=on_diagnostic)
            report["diagnostics"].append(entry)
        except (OSError, ValueError) as exc:
            record["status"] = "blocked"
            entry = emit(record["diagnostics"], "error", "package.write_failed", str(exc),
                         song_id=record["id"], source=record["source"], callback=on_diagnostic)
            report["diagnostics"].append(entry)
            # Failed work stays outside songs/ for diagnosis; never delete input.
        summarize(record)
        write_report()
    summarize(report)
    report["counts"] = dict(Counter(p["status"] for p in report["packages"]))
    report["valid"] = not report["errors"] and bool(report["counts"].get("normalized"))
    report["complete"] = True
    write_report()
    return report


def preprocess_settings(data: dict, origin: Path, *, scan_only: bool = False, on_diagnostic=None,
                        resume_from: Path | None = None) -> dict:
    from .settings import effective_settings
    config = effective_settings(data, origin)
    local = config["local"]
    if not local["input_root"]:
        raise PlanError("请先选择待更新曲目目录。")
    master_path = Path(local["assets_root"]) / "songs" / "songlist" if local["assets_root"] else None
    master = read_json(master_path) if master_path and master_path.is_file() else None
    options = {"master": master, "import_version": config["import"]["version"], "assets_root": local["assets_root"] or None,
               "existing_songs": (Path(local["server_project_root"]) / local["database_relative_path"] / "songs") if local["server_project_root"] else None,
               "background_pairs": config["import"]["background_pairs"],
               "background_pair_conflicts": config["import"]["background_pair_conflicts"],
               "initialize_background_pairs": not config["import"]["background_pairs_initialized"], "on_diagnostic": on_diagnostic}
    if scan_only:
        if resume_from is not None:
            raise PlanError("只读扫描不能同时继续工作区。")
        return scan_collection(Path(local["input_root"]), **options)
    if not local["release_root"]:
        raise PlanError("请先在配置页指定发布工作区。")
    return preprocess_collection(Path(local["input_root"]), Path(local["release_root"]),
                                 ffmpeg_path=local["ffmpeg_path"], resume_from=resume_from, **options)
