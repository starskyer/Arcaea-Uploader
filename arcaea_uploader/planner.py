"""Read-only inventory, songlist merging and resource routing.

This module never writes into an input package, assets tree or server database.
Media entries describe preparation work, not final encoded-byte differences.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from typing import Any

from .charts import is_empty_aff, hitsound_files
from .metadata import read_author_metadata
from .import_rules import (portable_id, require_version, normalize_import_row, actual_chart_classes,
                           validate_rating_values)
from .backgrounds import BackgroundIndex, complete_inverse, check_references, split_pairs, learn_pairs, merge_pairs, pair_lookup
from .diagnostics import emit, legacy_diagnostics


class PlanError(ValueError):
    """Invalid configuration or resource layout."""


def read_json(path: Path) -> Any:
    try:
        def reject_constant(value):
            raise ValueError(f"Invalid JSON constant: {value}")
        return json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=reject_constant)
    except (OSError, UnicodeError, ValueError) as exc:
        raise PlanError(f"Cannot read JSON {path}: {exc}") from exc


def digest(path: Path) -> str:
    value = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def valid_id(value: Any) -> bool:
    return (isinstance(value, str) and bool(value)
            and value not in {".", ".."}
            and not re.search(r'[\\/:*?"<>|\x00-\x1f]', value)
            and not value.endswith((" ", ".")))


def song_rows(data: Any) -> list[dict]:
    if isinstance(data, dict):
        data = data.get("songs", [data] if "id" in data else None)
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        raise PlanError("songlist must contain a songs array, a song object, or an array of songs")
    return data


def check_difficulties(row: dict) -> None:
    difficulties = row.get("difficulties", [])
    if not isinstance(difficulties, list):
        raise PlanError(f"{row.get('id')}: difficulties must be an array")
    seen = set()
    for item in difficulties:
        if not isinstance(item, dict):
            raise PlanError(f"{row.get('id')}: difficulty must be an object")
        rating = item.get("ratingClass")
        if type(rating) is not int or rating not in range(5) or rating in seen:
            raise PlanError(f"{row.get('id')}: invalid or duplicate ratingClass {rating!r}")
        seen.add(rating)
    validate_rating_values(row)


def merge_value(old: Any, patch: Any, path: str, conflicts: list) -> Any:
    if isinstance(old, dict) and isinstance(patch, dict):
        result = deepcopy(old)
        for key, value in patch.items():
            result[key] = (merge_value(result[key], value, f"{path}.{key}", conflicts)
                           if key in result else deepcopy(value))
        return result
    if old == patch:
        return deepcopy(old)
    conflicts.append({"field": path, "existing": old, "incoming": patch})
    return deepcopy(old)


def merge_songlist(master: dict, patches: list[dict], *, import_version=None, actual_charts=None,
                   incoming_charts=None, diagnostics=None) -> tuple[dict, list]:
    """Preserve indices and absent values; key difficulties by ratingClass."""
    if not isinstance(master, dict) or not isinstance(master.get("songs"), list):
        raise PlanError("Master songlist must be an object with a songs array")
    result = deepcopy(master)
    existing = {}
    used_idx = set()
    for row in result["songs"]:
        if not isinstance(row, dict) or not valid_id(row.get("id")):
            raise PlanError("Master songlist contains an invalid song ID")
        if row["id"] in existing:
            raise PlanError(f"Duplicate ID in master songlist: {row['id']}")
        idx = row.get("idx")
        if type(idx) is not int or idx < 0:
            raise PlanError(f"Invalid idx in master songlist: {idx!r}")
        used_idx.add(idx)
        check_difficulties(row)
        existing[row["id"]] = row
    next_idx = max(used_idx, default=-1) + 1
    conflicts: list[dict] = []
    seen_patches = set()
    if import_version is not None:
        import_version = require_version(import_version)

    def finalize(row):
        if import_version is None:
            return
        sid = row["id"]
        actual = actual_charts.get(sid, set()) if actual_charts is not None else {
            d["ratingClass"] for d in row.get("difficulties", []) if d.get("rating", -1) >= 0}
        normalize_import_row(row, import_version, actual, lambda level, code, message:
            emit(diagnostics if diagnostics is not None else [], level, code, message, song_id=sid))
    for patch in patches:
        song_id = patch.get("id")
        if not portable_id(song_id) or song_id in seen_patches:
            raise PlanError(f"Invalid or duplicate input song ID: {song_id!r}")
        seen_patches.add(song_id)
        check_difficulties(patch)
        if song_id not in existing:
            row = deepcopy(patch)
            row["idx"] = next_idx
            row["remote_dl"] = True
            row.setdefault("difficulties", [])
            next_idx += 1
            existing[song_id] = row
            result["songs"].append(row)
            finalize(row)
            continue
        row = existing[song_id]
        # idx is allocated locally; remote_dl is required for this import workflow.
        excluded = {"idx", "difficulties", "remote_dl"} | ({"version"} if import_version is not None else set())
        fields = {k: v for k, v in patch.items() if k not in excluded}
        merged = merge_value(row, fields, song_id, conflicts)
        by_rating = {d["ratingClass"]: d for d in merged.get("difficulties", [])}
        for difficulty in patch.get("difficulties", []):
            rating = difficulty["ratingClass"]
            if (incoming_charts is not None and actual_charts is not None and rating in by_rating
                    and rating not in incoming_charts.get(song_id, set()) and rating in actual_charts.get(song_id, set())
                    and difficulty.get("rating", 0) <= 0):
                continue  # A source placeholder cannot overwrite an existing real difficulty.
            if import_version is not None:
                difficulty = {k: v for k, v in difficulty.items() if k != "version"}
            by_rating[rating] = (merge_value(by_rating[rating], difficulty,
                                           f"{song_id}.difficulties[{rating}]", conflicts)
                                  if rating in by_rating else deepcopy(difficulty))
        merged["difficulties"] = [by_rating[key] for key in sorted(by_rating)]
        merged["remote_dl"] = True
        row.clear()
        row.update(merged)
        finalize(row)
    return result, conflicts


def load_package(folder: Path, diagnostics=None) -> tuple[dict, list[Path], list[str]]:
    if not valid_id(folder.name) or folder.is_symlink():
        raise PlanError(f"Invalid package directory: {folder}")
    files = sorted(folder.iterdir(), key=lambda p: p.name.casefold())
    if any(path.is_symlink() or not path.is_file() for path in files):
        raise PlanError(f"{folder.name}: nested directories and links are not supported")
    names = [path.name.casefold() for path in files]
    if len(set(names)) != len(names):
        raise PlanError(f"{folder.name}: case-insensitive filename collision")
    # Other extensions may be notes; never guess which of several songlists to use.
    songlists = [p for p in files if p.name in {"songlist", "songlist.json", "songlist.txt"}]
    if len(songlists) != 1:
        raise PlanError(f"{folder.name}: expected exactly one songlist[.json|.txt]")
    try:
        row, notes = read_author_metadata(songlists[0])
    except ValueError as exc:
        raise PlanError(str(exc)) from exc
    rows = [row]
    if not portable_id(row.get("id")):
        raise PlanError("songlist.id 为空或不符合英文资源 ID 规范；不会自动修改 ID 或目录。")
    if len(rows) != 1 or rows[0].get("id") != folder.name:
        raise PlanError(f"{folder.name}: songlist must describe exactly this folder's song ID")
    check_difficulties(rows[0])
    warnings = []
    for note in notes:
        message = "元数据已恢复为完整单曲对象，请人工复核：" + note
        warnings.append(message)
        if diagnostics is not None:
            emit(diagnostics, "warning", "metadata.recovered", message, song_id=row.get("id"), source=songlists[0])
    empty = [p for p in files if re.fullmatch(r"[0-4]\.aff", p.name) and is_empty_aff(p.read_bytes())]
    if empty:
        files = [p for p in files if p not in empty]
        ignored_ratings = {int(p.stem) for p in empty}
        for difficulty in rows[0].get("difficulties", []):
            if difficulty["ratingClass"] in ignored_ratings:
                difficulty["rating"] = 0
        if diagnostics is not None:
            for path in empty:
                emit(diagnostics, "info", "chart.empty", f"排除默认空 AFF：{path.name}，声明的占位难度 rating 统一为 0。", song_id=row["id"], source=path)
    if not any(re.fullmatch(r"[0-4]\.aff", path.name) for path in files):
        raise PlanError(f"{folder.name}: at least one nonempty chart (0.aff through 4.aff) is required")
    effects = {name.casefold() for path in files if path.suffix == ".aff" for name in hitsound_files(path.read_bytes())}
    excluded_wav = [p for p in files if p.suffix.casefold() == ".wav"]
    files = [p for p in files if p not in excluded_wav]
    for path in excluded_wav:
        message = f"特殊音效 {path.name} 不导入 bundle 或下载 songs，请人工处理。"
        warnings.append(message)
        if diagnostics is not None:
            emit(diagnostics, "warning", "audio.hitsound_excluded", message, song_id=row["id"], source=path)
    for name in effects - {p.name.casefold() for p in excluded_wav}:
        message = f"谱面引用特殊音效 {name}，本次标准包未包含该文件，需人工处理。"
        warnings.append(message)
        if diagnostics is not None:
            emit(diagnostics, "warning", "audio.hitsound_missing", message, song_id=row["id"])
    for path in files:
        if path.suffix.lower() in {".aff", ".ogg"}:
            if not re.fullmatch(r"(?:[0-4]\.aff|(?:base|[0-4])\.ogg)", path.name):
                raise PlanError(f"{folder.name}: unsupported chart/audio filename {path.name}")
        elif path.suffix.lower() == ".jpg" and path.suffix != ".jpg":
            raise PlanError(f"{folder.name}: use lowercase .jpg filenames")
        elif path != songlists[0] and path.suffix != ".jpg" and path.name != "video_1080.mp4":
            if diagnostics is not None:
                emit(diagnostics, "info", "file.ignored", f"忽略非导入文件 {path.name}。", song_id=row["id"], source=path)
    return deepcopy(rows[0]), files, warnings


def resolve_config(config_path: Path) -> dict:
    config_path = config_path.resolve(strict=True)
    config = read_json(config_path)
    if not isinstance(config, dict) or not isinstance(config.get("local"), dict):
        raise PlanError("Configuration must contain a local object")
    local = config["local"]
    try:
        config.setdefault("import", {})["version"] = require_version(config.get("import", {}).get("version"))
    except ValueError as exc:
        raise PlanError(str(exc)) from exc
    for key in ("input_root", "assets_root", "server_project_root", "placeholder_aff"):
        value = local.get(key)
        if not isinstance(value, str) or not value:
            raise PlanError(f"Missing local.{key}")
        path = Path(value).expanduser()
        local[key] = (config_path.parent / path).resolve() if not path.is_absolute() else path.resolve()
    relative_value = local.get("database_relative_path", "latest version/database")
    if not isinstance(relative_value, str) or not relative_value:
        raise PlanError("database_relative_path must be a nonempty string")
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise PlanError("database_relative_path must stay within server_project_root")
    local["database_root"] = (local["server_project_root"] / relative).resolve()
    if not local["database_root"].is_relative_to(local["server_project_root"]):
        raise PlanError("Database path escapes server project")
    for key in ("input_root", "assets_root", "database_root"):
        if not local[key].is_dir():
            raise PlanError(f"Not a directory: {local[key]}")
    if not local["placeholder_aff"].is_file():
        raise PlanError(f"Placeholder chart is missing: {local['placeholder_aff']}")
    return config


def build_plan(config: dict) -> dict:
    # Conflicts are for human review; no automatic overwrite or background fallback.
    local = config["local"]
    assets = local["assets_root"]
    database = local["database_root"]
    master_path = assets / "songs" / "songlist"
    master = read_json(master_path)
    import_version = require_version(config.get("import", {}).get("version"))
    pairs, detected_conflicts = split_pairs(config.get("import", {}).get("background_pairs", {}))
    pair_conflicts = {**config.get("import", {}).get("background_pair_conflicts", {}), **detected_conflicts}
    if not config.get("import", {}).get("background_pairs_initialized", False):
        learned, master_conflicts = learn_pairs(master)
        pair_conflicts = {**pair_conflicts, **master_conflicts}
        pairs = merge_pairs(learned, pairs)
    paired_ids = pair_lookup(pairs)
    backgrounds = BackgroundIndex(assets)
    plan: dict[str, Any] = {
        "schema_version": 1, "mode": "read_only_plan", "publishable": False,
        "errors": [], "warnings": [], "songs": [], "files": [], "preview_jobs": [], "diagnostics": [],
        "note": "No files modified. Media jobs are not final byte differences; build/upload is not implemented.",
    }
    emit(plan["diagnostics"], "info", "background.table", f"使用背景翻转对应表 {len(pairs)} 组，均可双向推导。")
    for bg, choices in pair_conflicts.items():
        if bg not in paired_ids:
            emit(plan["diagnostics"], "warning", "background.table_conflict", f"背景 {bg} 属于无法一一配对的关系组，关联 {choices}，请人工指定。")
    directories = sorted(local["input_root"].iterdir(), key=lambda p: (p.name.casefold(), p.name))
    packages = []
    for folder in directories:
        if not folder.is_dir():
            emit(plan["diagnostics"], "info", "input.ignored", f"忽略输入根目录中的非曲目项 {folder.name}。")
            continue
        try:
            patch, files, warnings = load_package(folder, plan["diagnostics"])
            packages.append((patch, files))
            plan["warnings"].extend(warnings)
        except PlanError as exc:
            plan["errors"].append(str(exc))
    if not packages:
        plan["errors"].append("No valid song packages found")
    incoming = {row["id"]: {int(p.stem) for p in files if re.fullmatch(r"[0-4]\.aff", p.name)} for row, files in packages}
    actual = {sid: ratings | actual_chart_classes(database / "songs" / sid) for sid, ratings in incoming.items()}
    master_merged, conflicts = merge_songlist(master, [row for row, _ in packages], import_version=import_version,
                                            actual_charts=actual, incoming_charts=incoming, diagnostics=plan["diagnostics"])
    idx_to_ids: dict[int, list[str]] = {}
    for row in master["songs"]:
        idx_to_ids.setdefault(row["idx"], []).append(row["id"])
    plan["master_songlist_issues"] = [
        {"idx": idx, "ids": ids, "issue": "duplicate_idx"}
        for idx, ids in idx_to_ids.items() if len(ids) > 1
    ]
    if plan["master_songlist_issues"]:
        plan["errors"].append("Existing master has duplicate idx values; retained for manual review")
    plan["songlist_conflicts"] = conflicts
    if conflicts:
        for conflict in conflicts:
            emit(plan["diagnostics"], "warning", "metadata.merge_conflict",
                 f"songlist {conflict['field']} 存在冲突，暂保留原值 {conflict['existing']!r}；输入 {conflict['incoming']!r}，请人工审核。")
    plan["merged_songlist"] = master_merged
    plan["songlist_changed"] = master_merged != master
    rows = {row["id"]: row for row in master_merged["songs"]}
    original = {row["id"]: row for row in master["songs"]}
    for sid in incoming:
        complete_inverse(rows[sid], pairs, lambda level, code, message, song_id=sid:
                         emit(plan["diagnostics"], level, code, message, song_id=song_id), pair_conflicts)
    claimed_targets: dict[tuple[str, str], str] = {}

    def add_file(source: Path, area: str, relative: str, media: bool = False, placeholder: bool = False):
        root = assets if area == "assets" else database
        target = root / relative
        # Resolve before reading: a target symlink must not escape its resource root.
        if not target.resolve().is_relative_to(root.resolve()):
            raise PlanError(f"Target escapes resource root: {target}")
        if target.exists() and not target.is_file():
            raise PlanError(f"Target is not a file: {target}")
        source_hash = digest(source)
        key = (area, relative.casefold())
        if key in claimed_targets:
            if claimed_targets[key] != source_hash:
                plan["errors"].append(f"Different input files target the same path: {area}/{relative}")
            else:
                emit(plan["diagnostics"], "info", "file.same_in_batch", f"本批已提供同内容资源 {area}/{relative}，跳过重复项。")
            return
        claimed_targets[key] = source_hash
        target_hash = digest(target) if target.is_file() else None
        # Media conversion is deferred; comparing original JPG/OGG bytes is misleading.
        action = "prepare_media" if media else (
            "unchanged" if target_hash == source_hash else "replace" if target_hash else "add")
        if action == "replace":
            plan["errors"].append(f"Existing file differs: {area}/{relative}")
        plan["files"].append({"source": str(source), "area": area, "path": relative,
                              "action": action, "source_sha256": source_hash,
                              "existing_sha256": target_hash, "placeholder": placeholder})

    for patch, files in packages:
        song_id = patch["id"]
        row = rows[song_id]
        available = {path.name: path for path in files}
        existing_server = database / "songs" / song_id
        bundle_folder = assets / "songs" / f"dl_{song_id}"
        plan["songs"].append({"id": song_id, "idx": row["idx"],
                              "kind": "incremental" if song_id in original else "new"})
        try:
            # Incomplete updates may reuse existing download audio and prepared artwork.
            diagnostics = plan["warnings"] if song_id in original else plan["errors"]
            if "base.ogg" not in available and not (existing_server / "base.ogg").is_file():
                diagnostics.append(f"{song_id}: no incoming or existing base.ogg")
            for name, normalized in (("base.jpg", "1080_base.jpg"),
                                     ("base_256.jpg", "1080_base_256.jpg")):
                if name not in available and not (bundle_folder / normalized).is_file():
                    diagnostics.append(f"{song_id}: no incoming {name} or existing {normalized}")
            for path in files:
                if path.suffix in {".aff", ".ogg"} or path.name == "video_1080.mp4":
                    add_file(path, "database", f"songs/{song_id}/{path.name}")
                if path.suffix == ".jpg":
                    if re.fullmatch(r"(?:base|[0-4])(?:_256)?\.jpg", path.name):
                        add_file(path, "assets", f"songs/dl_{song_id}/{path.name}", media=True)
                    else:
                        relative = backgrounds.route(path, lambda level, code, message:
                            emit(plan["diagnostics"], level, code, message, song_id=song_id, source=path))
                        if relative is None:
                            continue
                        add_file(path, "assets", relative)
                elif path.suffix == ".ogg":
                    add_file(path, "assets", f"songs/dl_{song_id}/{path.name}", media=True)
            for rating in range(3):
                name = f"{rating}.aff"
                if name not in available and not (existing_server / name).is_file():
                    add_file(local["placeholder_aff"], "database", f"songs/{song_id}/{name}", placeholder=True)
            # Describe regeneration needs without invoking ffmpeg or deleting audio.
            for name in sorted(available):
                if not name.endswith(".ogg"):
                    continue
                rating = int(name[0]) if name != "base.ogg" else None
                difficulty = next((d for d in row["difficulties"] if d["ratingClass"] == rating), {})
                start = difficulty.get("audioPreview", row.get("audioPreview"))
                end = difficulty.get("audioPreviewEnd", row.get("audioPreviewEnd"))
                if (type(start) not in {int, float} or type(end) not in {int, float}
                        or not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start):
                    plan["errors"].append(f"{song_id}/{name}: invalid or missing preview interval")
                plan["preview_jobs"].append({"id": song_id, "source": str(available[name]),
                                             "audio": name, "ratingClass": rating,
                                             "start_ms": start, "end_ms": end})
            if "video_1080.mp4" in available and not any(
                isinstance(item, dict) and item.get("file_name") == "video_1080.mp4"
                for item in row.get("additional_files", [])
            ):
                plan["warnings"].append(f"{song_id}: video_1080.mp4 is not declared in songlist.additional_files")
            for name in available:
                if re.fullmatch(r"[0-4]\.ogg", name) and name != "3.ogg":
                    plan["warnings"].append(f"{song_id}: upstream preview script does not support {name}; conversion rule pending")
        except (OSError, ValueError) as exc:
            plan["errors"].append(f"{song_id}: {exc}")
    available_bg = backgrounds.existing_ids | {Path(f["path"]).stem.casefold() for f in plan["files"]
        if f["area"] == "assets" and f["path"].startswith("img/bg/")}
    for sid in incoming:
        check_references(rows[sid], available_bg, lambda level, code, message, song_id=sid:
                         emit(plan["diagnostics"], level, code, message, song_id=song_id))
    legacy_diagnostics(plan)
    plan["valid"] = not plan["errors"] and bool(packages)
    return plan
