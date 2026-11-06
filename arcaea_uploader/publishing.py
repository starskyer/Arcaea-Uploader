"""Stage 01 review workspaces and resumable stage 02 publication."""

from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import re
import shutil
from time import perf_counter
from tempfile import mkdtemp, TemporaryDirectory

from .backgrounds import BackgroundIndex, check_references
from .bundler import build_configured_bundle, validate_bundle
from .bundle_versions import read_history, resolve_bundle_versions
from .import_rules import actual_chart_classes, portable_id
from .media import create_preview, normalize_jacket, preview_interval
from .planner import PlanError, build_plan, check_difficulties, digest, read_json, resolve_config
from .preprocess import preprocess_settings
from .private_files import copy_writable, replace_copy
from .server_tasks import (confirm_uploaded, load_task, make_task, read_reviewed, save_task,
                           task_lock, task_path)
from .settings import effective_settings, normalize_settings, user_data_dir
from .ssh_upload import SSHUpload, remote_database
from .workspaces import (ensure_plain_tree, linked, load_workspace, snapshot_workspace_settings,
                         sync_workspace_task, timestamp, workspace_lock, write_json)


EDITABLE = {"songs/songlist", "songs/packlist", "songs/unlocks"}
DOWNLOAD = re.compile(r"(?:[0-4]\.aff|(?:base|[0-4])\.ogg|video_1080\.mp4)")


def tree_hashes(root):
    root = Path(root)
    if not root.exists():
        return {}
    ensure_plain_tree(root)
    result, names = {}, set()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            if relative.casefold() in names:
                raise PlanError(f"资源路径大小写冲突：{relative}")
            names.add(relative.casefold())
            result[relative] = digest(path)
    return result


def _binding(settings):
    return {"import": settings["import"], "bundle": settings["bundle"],
            "local": {key: settings["local"][key] for key in (
                "input_root", "assets_root", "server_project_root", "database_relative_path", "placeholder_aff")}}


def _database(settings):
    local = settings["local"]
    project = Path(local["server_project_root"]).resolve()
    relative = Path(local["database_relative_path"])
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise PlanError("本地 database 路径必须在服务端项目内。")
    result = project / relative
    if not result.resolve().is_relative_to(project):
        raise PlanError("本地 database 路径越界。")
    return result


def _safe_roots(settings):
    local = settings["local"]
    paths = [Path(local[key]).resolve() for key in ("input_root", "assets_root", "server_project_root", "release_root")]
    for index, first in enumerate(paths):
        for second in paths[index + 1:]:
            if first.is_relative_to(second) or second.is_relative_to(first):
                raise PlanError("原始曲目、active、服务端项目与发布工作区必须相互独立，不能嵌套。")


def load_preparation(root, batch):
    load_workspace(root, batch)
    batch = Path(batch).resolve()
    ensure_plain_tree(batch)
    if (batch / "append-transaction.json").is_file():
        journal = read_json(batch / "append-transaction.json")
        if not journal.get("committed"):
            raise PlanError("追加提交尚未完成，请在工作区管理中继续此批次以恢复。")
    record = read_json(batch / "publish.json")
    if (record.get("owner") != "Arcaea-Uploader/publication" or record.get("schema_version") != 1
            or record.get("batch_id") != batch.name):
        raise PlanError("不是可识别的发布批次。")
    return record


def _download_baseline(database, ids):
    result = {}
    for sid in ids:
        folder = _target(database, "songs/" + sid)
        for name, value in tree_hashes(folder).items():
            if DOWNLOAD.fullmatch(name):
                result[f"songs/{sid}/{name}"] = value
    return result


def _check_hashes(root, expected, *, except_paths=()):
    current = tree_hashes(root)
    if {k: v for k, v in current.items() if k not in except_paths} != {
            k: v for k, v in expected.items() if k not in except_paths}:
        raise PlanError(f"资源发生变化，请重新执行 01（保留审核内容供参照）：{root}")


def _config_for_plan(settings, normalized, path):
    config = deepcopy(settings)
    config["local"]["input_root"] = str(normalized)
    write_json(path, config)
    return resolve_config(path)


def _log(callback, level, message):
    if callback:
        callback({"level": level, "code": "publication." + level, "message": message, "song_id": None})


def apply_prepared_files(plan, staged_assets, staged_database, temporary_root):
    """Apply a validated plan to a private preparation candidate."""
    for entry in plan["files"]:
        source = Path(entry["source"])
        if digest(source) != entry["source_sha256"]:
            raise PlanError("准备期间标准副本发生变化，请重新准备。")
        target = (staged_assets if entry["area"] == "assets" else staged_database) / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry["area"] == "assets" and source.suffix == ".jpg" and entry["action"] == "prepare_media":
            with TemporaryDirectory(prefix=".jacket-check-", dir=temporary_root) as temporary:
                normalized = normalize_jacket(source, Path(temporary))
                destination = target.parent / normalized.name
                if destination.exists() and digest(destination) != digest(normalized):
                    raise PlanError(f"已存在不同曲绘，请人工处理后重新准备：{entry['path']}")
                replace_copy(normalized, destination)
        elif entry["area"] == "assets" and source.suffix == ".ogg":
            continue
        else:
            replace_copy(source, target)


def prepare_update(data, origin, *, resume_from=None, on_diagnostic=None, _rebuild=False):
    """Prepare in isolation; never build, upload or modify the original assets."""
    settings = effective_settings(data, origin)
    requested_import = deepcopy(normalize_settings(data, origin)["import"])
    _safe_roots(settings)
    local = settings["local"]
    if resume_from and not _rebuild and (Path(resume_from) / "publish.json").is_file():
        from .accumulation import append_update
        return append_update(data, origin, resume_from, on_diagnostic=on_diagnostic)
    # Validate local baseline before creating a potentially large copy.
    assets, database = Path(local["assets_root"]), _database(settings)
    for name in ("songlist", "packlist", "unlocks"):
        read_json(assets / "songs" / name)
    from .charts import is_empty_aff
    if not is_empty_aff(Path(local["placeholder_aff"]).read_bytes()):
        raise PlanError("指定的占位 AFF 包含实际内容或无法识别；请选择仅含 AudioOffset、分隔线与 timing 的空谱。")
    resolve_bundle_versions(read_history(assets), **settings["bundle"])
    started = perf_counter()
    _log(on_diagnostic, "info", "01：核验原始曲目与完整 active 基线…")
    baseline = tree_hashes(assets)
    input_hashes = tree_hashes(Path(local["input_root"]))
    report = preprocess_settings(settings, origin, resume_from=resume_from, on_diagnostic=on_diagnostic)
    batch = Path(report["batch_root"])
    with workspace_lock(local["release_root"], batch):
        ensure_plain_tree(batch)
        snapshot_workspace_settings(batch, settings)
        plan = build_plan(_config_for_plan(settings, report["normalized_root"], batch / "plan-config.json"))
        plan["diagnostics"] = report["diagnostics"] + plan["diagnostics"]
        from .diagnostics import summarize
        summarize(plan)
        plan.update(mode="prepare", batch_root=str(batch), preprocessing_report=report["report_file"],
                    valid=not plan["errors"], publishable=False)
        if not plan["valid"]:
            write_json(batch / "plan.json", plan)
            if (batch / "publish.json").is_file():
                previous = read_json(batch / "publish.json")
                previous["state"] = "blocked"
                write_json(batch / "publish.json", previous)
            return plan
        _check_hashes(assets, baseline)
        _check_hashes(Path(local["input_root"]), input_hashes)
        ids = [song["id"] for song in plan["songs"]]
        download_baseline = _download_baseline(database, ids)
        for entry in plan["files"]:
            if entry["area"] == "database" and download_baseline.get(entry["path"]) != entry["existing_sha256"]:
                raise PlanError("预处理与预检期间本地下载资源发生变化，请重试。")
        # A new preparation never silently overwrites an earlier human draft.
        stage = batch / "prepared"
        if stage.exists():
            archive = Path(mkdtemp(prefix="review-previous-", dir=batch))
            stage.rename(archive / "prepared")
            _log(on_diagnostic, "warning", "上次审核副本已存入 " + str(archive))
        write_json(batch / "publish.json", {"owner": "Arcaea-Uploader/publication", "schema_version": 1,
            "batch_id": batch.name, "state": "preparing", "binding": _binding(settings), "requested_import": requested_import})
        write_json(batch / "plan.json", plan)
        staged_assets, staged_database = stage / "active", stage / "database"
        copying = perf_counter()
        _log(on_diagnostic, "info", f"基线核验及规划完成（{copying - started:.1f} 秒）；复制完整 active，共 {len(baseline)} 个文件…")
        shutil.copytree(assets, staged_assets, copy_function=copy_writable)
        _log(on_diagnostic, "info", f"active 复制完成（{perf_counter() - copying:.1f} 秒）；准备增量资源与预览…")
        for sid in ids:
            folder = staged_database / "songs" / sid
            folder.mkdir(parents=True)
            for relative in download_baseline:
                if relative.startswith(f"songs/{sid}/"):
                    copy_writable(database / relative, staged_database / relative)
        apply_prepared_files(plan, staged_assets, staged_database, batch)
        write_json(staged_assets / "songs/songlist", plan["merged_songlist"])
        record = {"owner": "Arcaea-Uploader/publication", "schema_version": 1,
                  "batch_id": batch.name, "created_at": timestamp(), "state": "preparing",
                  "binding": _binding(settings), "baseline": baseline, "input_sha256": input_hashes,
                  "requested_import": requested_import,
                  "normalized_sha256": tree_hashes(batch / "songs"), "database_baseline": download_baseline,
                  "ids": ids, "preview_jobs": plan["preview_jobs"], "preview_signatures": {},
                  "actual": {sid: sorted(actual_chart_classes(staged_database / "songs" / sid)) for sid in ids}}
        original_rows = {row["id"]: row for row in read_json(assets / "songs/songlist")["songs"]}
        merged_rows = {row["id"]: row for row in plan["merged_songlist"]["songs"]}
        for sid in ids:
            for source in (staged_database / "songs" / sid).glob("*.ogg"):
                if not re.fullmatch(r"(?:base|[0-4])\.ogg", source.name) or sid not in original_rows:
                    continue
                rating = None if source.stem == "base" else int(source.stem)
                name = "preview.ogg" if rating is None else f"{rating}_preview.ogg"
                key, relative = f"{sid}/{name}", f"songs/dl_{sid}/{name}"
                try:
                    before = preview_interval(original_rows[sid], rating)
                    after = preview_interval(merged_rows[sid], rating)
                except ValueError:
                    continue
                if (before == after and baseline.get(relative) is not None
                        and download_baseline.get(f"songs/{sid}/{source.name}") == digest(source)):
                    record["preview_signatures"][key] = {
                        "input": [digest(source), *after], "sha256": baseline[relative]}
                    _log(on_diagnostic, "info", f"音频与预览区间未变，复用 {key}。")
        _render_previews(batch, record, plan["merged_songlist"], local["ffmpeg_path"], on_diagnostic)
        _check_hashes(assets, baseline)
        _check_hashes(Path(local["input_root"]), input_hashes)
        record.update(state="review", prepared_assets=tree_hashes(staged_assets),
                      prepared_database=tree_hashes(staged_database))
        record["source_roots"] = [{"path": local["input_root"], "sha256": input_hashes}]
        write_json(batch / "publish.json", record)
        plan["review_songlist"] = str(staged_assets / "songs/songlist")
        plan["prepared_assets"] = str(staged_assets)
        plan["prepared_database"] = str(staged_database)
        plan["actual"] = record["actual"]
        write_json(batch / "plan.json", plan)
        sync_workspace_task(local["release_root"], batch, plan["merged_songlist"], record["actual"])
        _log(on_diagnostic, "info", f"01 已完成（{perf_counter() - started:.1f} 秒），请审核并保存 " + plan["review_songlist"])
        return plan


def _render_previews(batch, record, document, ffmpeg, callback):
    assets = batch / "prepared/active"
    database = batch / "prepared/database"
    rows = {row["id"]: row for row in document["songs"]}
    # Include existing full audio so final review can alter preview intervals in
    # an incremental chart-only patch without needing the author to resend it.
    for sid in record["ids"]:
        folder = assets / "songs" / ("dl_" + sid)
        for source in sorted((database / "songs" / sid).glob("*.ogg")):
            if not re.fullmatch(r"(?:base|[0-4])\.ogg", source.name):
                continue
            rating = None if source.stem == "base" else int(source.stem)
            name = "preview.ogg" if rating is None else f"{rating}_preview.ogg"
            start, end = preview_interval(rows[sid], rating)
            signature = [digest(source), start, end]
            key = f"{sid}/{name}"
            target = folder / name
            previous = record["preview_signatures"].get(key)
            if previous and previous["input"] == signature and target.is_file() and digest(target) == previous["sha256"]:
                continue
            if target.exists() and target.stat().st_nlink > 1:
                replace_copy(target, target)
            create_preview(source, target, rows[sid], rating_class=rating, ffmpeg_path=ffmpeg, replace=True)
            record["preview_signatures"][key] = {"input": signature, "sha256": digest(target)}
            if "prepared_assets" in record:
                # Checkpoint only this generated file, never adopt unrelated
                # human edits. A later failed render can continue safely.
                record["prepared_assets"]["songs/dl_" + key] = digest(target)
                write_json(batch / "publish.json", record)
            _log(callback, "info", f"生成预览 {sid}/{name}：{start}–{end} ms。")


def _validate_review(batch, record, document, version, callback):
    if not isinstance(document, dict) or not isinstance(document.get("songs"), list):
        raise PlanError("审核总表必须包含 songs 数组。")
    ids, indices, rows = set(), set(), {}
    for row in document["songs"]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            raise PlanError("审核总表含无效 ID。")
        sid, idx = row["id"], row.get("idx")
        if sid.casefold() in ids or type(idx) is not int or idx < 0 or idx in indices:
            raise PlanError("审核总表存在重复 ID 或无效 / 重复 idx。")
        ids.add(sid.casefold())
        indices.add(idx)
        check_difficulties(row)
        rows[sid] = row
    draft = read_json(batch / "plan.json")["merged_songlist"]
    if rows.keys() != {row["id"] for row in draft["songs"]}:
        raise PlanError("审核阶段不能新增、删除或重命名曲目；请通过 01 重新准备。")
    original = {row["id"]: row for row in draft["songs"]}
    for sid, row in rows.items():
        if sid not in record["ids"]:
            if row != original[sid]:
                raise PlanError(f"审核修改了本批范围外的曲目 {sid}，请恢复或另建批次。")
            continue
        if not portable_id(sid) or row.get("remote_dl") is not True or row.get("version") != version:
            raise PlanError(f"{sid}: ID、remote_dl 或本次 version 无效。")
        if row["idx"] != original[sid]["idx"]:
            raise PlanError(f"{sid}: 审核阶段不能修改自动分配的 idx。")
        difficulties = {d["ratingClass"]: d for d in row.get("difficulties", [])}
        for rating in record["actual"][sid]:
            item = difficulties.get(rating)
            if item is None or type(item.get("rating")) not in {int, float} or item["rating"] < 0:
                raise PlanError(f"{sid}/{rating}: 真实谱面缺少合法难度声明。")
        for rating, difficulty in difficulties.items():
            if rating not in record["actual"][sid] and difficulty.get("rating") != 0:
                raise PlanError(f"{sid}/{rating}: 空难度 rating 必须为 0。")
        folder = batch / "prepared/database/songs" / sid
        for rating in range(3):
            if not (folder / f"{rating}.aff").is_file():
                raise PlanError(f"{sid}: 缺少必需的 {rating}.aff。")
        for entry in row.get("additional_files", []):
            name = entry.get("file_name") if isinstance(entry, dict) else None
            if not isinstance(name, str) or not DOWNLOAD.fullmatch(name) or not (folder / name).is_file():
                raise PlanError(f"{sid}: additional_files 引用未准备的资源 {name!r}。")
        for difficulty in difficulties.values():
            if difficulty.get("audioOverride") and not (folder / f"{difficulty['ratingClass']}.ogg").is_file():
                raise PlanError(f"{sid}: audioOverride 缺少对应难度音频。")
        jackets = batch / "prepared/active/songs" / ("dl_" + sid)
        for name in ("1080_base.jpg", "1080_base_256.jpg", "preview.ogg"):
            if not (jackets / name).is_file():
                raise PlanError(f"{sid}: 缺少 {name}。")
        available = BackgroundIndex(batch / "prepared/active").existing_ids
        check_references(row, available, lambda level, code, message: _log(callback, level, message))


def _sync_documents(assets, stage, record):
    # Users can edit packlist/unlocks in their familiar active directory or the
    # review copy. Never overwrite two independently edited versions silently.
    for relative in ("songs/packlist", "songs/unlocks"):
        source, target = assets / relative, stage / relative
        source_hash, target_hash, old = digest(source), digest(target), record["baseline"][relative]
        if source_hash != old:
            if target_hash not in {old, source_hash}:
                raise PlanError(f"原 active 和审核副本同时修改了 {relative}，请人工统一后重试。")
            shutil.copy2(source, target)
        read_json(target)


@contextmanager
def _resource_lock(settings):
    # Independent of release_root, so two configurations cannot publish the same
    # active tree concurrently into different workspaces.
    directory = user_data_dir() / ".resource-locks"
    directory.mkdir(parents=True, exist_ok=True)
    key = sha256(os.path.normcase(settings["local"]["assets_root"]).encode()).hexdigest()
    with task_lock(directory / (key + ".json")):
        yield


def _target(root, relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts:
        raise PlanError("发布清单路径越界。")
    result = root.joinpath(*path.parts)
    cursor = result
    while cursor != root.parent:
        if os.path.lexists(cursor) and linked(cursor):
            raise PlanError("本地发布目标不能包含链接或目录联接。")
        if cursor == root:
            break
        cursor = cursor.parent
    return result


def _install(entries, roots, batch, callback):
    # Preflight every file before exposing any part of this publication.
    for entry in entries:
        target = _target(roots[entry["area"]], entry["path"])
        current = digest(target) if target.is_file() else None
        if target.exists() and not target.is_file() or current not in {entry.get("existing_sha256"), entry["sha256"]}:
            raise PlanError(f"本地资源与审核基线不一致：{entry['area']}/{entry['path']}")
        if digest(Path(entry["source"])) != entry["sha256"]:
            raise PlanError("待发布文件被改动。")
    for entry in entries:
        target = _target(roots[entry["area"]], entry["path"])
        if target.is_file() and digest(target) == entry["sha256"]:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            backup = batch / "backups" / entry["area"] / entry["path"]
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                shutil.copy2(target, backup)
        temporary = target.with_name("." + target.name + "." + batch.name + ".part")
        if os.path.lexists(temporary) and linked(temporary):
            raise PlanError("本地暂存文件不能是链接。")
        try:
            shutil.copy2(entry["source"], temporary)
            if digest(temporary) != entry["sha256"]:
                raise PlanError("本地暂存文件核验失败。")
            current = digest(target) if target.is_file() else None
            if current not in {entry.get("existing_sha256"), entry["sha256"]}:
                raise PlanError("本地就位期间目标发生变化。")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        _log(callback, "info", f"本地就位 {entry['area']}/{entry['path']}")
    for entry in entries:
        if digest(_target(roots[entry["area"]], entry["path"])) != entry["sha256"]:
            raise PlanError("本地最终清单核验失败。")


def publish_update(data, origin, batch, *, on_diagnostic=None, on_host_key=None, transport_factory=SSHUpload):
    settings = effective_settings(data, origin)
    _safe_roots(settings)
    remote_root = remote_database(settings)  # Blank host fails before building.
    root, batch = Path(settings["local"]["release_root"]), Path(batch).resolve()
    with _resource_lock(settings), workspace_lock(root, batch):
        from .accumulation import recover_append, check_sources
        recover_append(batch)
        record = load_preparation(root, batch)
        if record.get("state") in {"awaiting_stage03", "committed_cleanup_pending"}:
            receipt = read_json(batch / "receipt.json")
            return {"mode": "published", "batch_root": str(batch), "batch_id": batch.name,
                    "version": record["build"]["version"], "server_task_file": str(batch / "server-task.json"),
                    "receipt": receipt}
        if record.get("pending_append"):
            raise PlanError("本批仍有未完成的追加，请修复并再次执行 01，不能只发布先前成功的部分。")
        if record.get("state") in {"preparing", "blocked"}:
            raise PlanError("01 尚未完成，请先继续准备并解决 ERROR。")
        if normalize_settings(data, origin)["import"] == record.get("requested_import"):
            # A successful local install can teach new background pairs. Those
            # inferred changes are not edits to the user's original settings.
            settings["import"] = deepcopy(record["binding"]["import"])
        if record["binding"] != _binding(settings):
            raise PlanError("01 后导入配置或本地路径发生变化，请恢复原配置或重新准备。")
        snapshot_workspace_settings(batch, settings)
        local = settings["local"]
        assets, database = Path(local["assets_root"]), _database(settings)
        stage, staged_database = batch / "prepared/active", batch / "prepared/database"
        check_sources(record)
        _check_hashes(batch / "songs", record["normalized_sha256"])
        target_binding = {key: settings["ssh"][key] for key in ("host", "port", "username")}
        target_binding["database"] = remote_root
        if record.get("target") and record["target"] != target_binding:
            raise PlanError("该批次已绑定另一远端目标；请恢复该目标后继续。")
        if not record.get("build"):
            _check_hashes(assets, record["baseline"], except_paths={"songs/packlist", "songs/unlocks"})
            if _download_baseline(database, record["ids"]) != record["database_baseline"]:
                raise PlanError("本地下载资源基线发生变化，请重新准备。")
            _check_hashes(stage, record["prepared_assets"], except_paths=EDITABLE | {"metadata.oldjson"})
            _check_hashes(staged_database, record["prepared_database"])
            _sync_documents(assets, stage, record)
            reviewed_sha = digest(stage / "songs/songlist")
            reviewed = read_json(stage / "songs/songlist")
            _validate_review(batch, record, reviewed, settings["import"]["version"], on_diagnostic)
            _render_previews(batch, record, reviewed, local["ffmpeg_path"], on_diagnostic)
            if digest(stage / "songs/songlist") != reviewed_sha:
                raise PlanError("生成预览期间审核总表发生变化，请重试。")
            _check_hashes(staged_database, record["prepared_database"])
            check_sources(record)
            preview_paths = {"songs/dl_" + key for key in record["preview_signatures"]}
            _check_hashes(stage, record["prepared_assets"], except_paths=EDITABLE | preview_paths | {"metadata.oldjson"})
            # Persist new previews before the build checkpoint, so a failed
            # build can be retried without misidentifying generated files as edits.
            record["prepared_assets"] = tree_hashes(stage)
            # Bundler only advances this private snapshot. Restore the original
            # history before retrying a failed/interrupted build, never live history.
            history = stage / "metadata.oldjson"
            baseline_history = record["baseline"].get("metadata.oldjson")
            if baseline_history:
                shutil.copy2(assets / "metadata.oldjson", history)
            else:
                history.unlink(missing_ok=True)
            output = batch / "bundle"
            if output.exists():
                archive = Path(mkdtemp(prefix="build-previous-", dir=batch))
                output.rename(archive / "bundle")
            record["state"] = "building"
            write_json(batch / "publish.json", record)
            _log(on_diagnostic, "info", "调用内置 Bundler，打包审核后的资源。")
            bundle = build_configured_bundle(stage, output, **settings["bundle"])
            metadata = validate_bundle(bundle.bundle, bundle.metadata)
            if digest(stage / "songs/songlist") != reviewed_sha:
                raise PlanError("打包期间审核总表发生变化，请重试。")
            _check_hashes(stage, record["prepared_assets"], except_paths={"metadata.oldjson"})
            expected = resolve_bundle_versions(read_history(assets), **settings["bundle"])
            if (metadata["versionNumber"] != expected.bundle_version
                    or metadata["applicationVersionNumber"] != expected.app_version
                    or metadata["previousVersionNumber"] != expected.previous_version
                    or len(read_history(stage)) != len(read_history(assets)) + 1):
                raise PlanError("Bundler 版本链或历史核验失败。")
            entries = []
            current_baseline = tree_hashes(assets)
            for relative, value in tree_hashes(stage).items():
                if value != current_baseline.get(relative):
                    entries.append({"area": "assets", "path": relative, "source": str(stage / relative),
                                    "sha256": value, "existing_sha256": current_baseline.get(relative)})
            for relative, value in tree_hashes(staged_database).items():
                entries.append({"area": "database", "path": relative, "source": str(staged_database / relative),
                                "sha256": value, "existing_sha256": record["database_baseline"].get(relative)})
            for path in (bundle.bundle, bundle.metadata):
                entries.append({"area": "database", "path": "bundle/" + path.name, "source": str(path),
                                "sha256": digest(path), "existing_sha256": None})
            record.update(state="built", target=target_binding, build={"version": bundle.version,
                "bundle": str(bundle.bundle.relative_to(batch)), "metadata": str(bundle.metadata.relative_to(batch)),
                "songlist_sha256": digest(stage / "songs/songlist"), "entries": entries,
                "final_assets": tree_hashes(stage)})
            write_json(batch / "publish.json", record)
        build = record["build"]
        _check_hashes(stage, build["final_assets"])
        _check_hashes(staged_database, record["prepared_database"])
        validate_bundle(batch / build["bundle"], batch / build["metadata"])
        # Any changed live file must be either the original or this exact build,
        # permitting recovery after a partial local install without new versions.
        # Recover only this batch's known local atomic-copy temporary files.
        # Their paths are derived from validated manifest destinations.
        for entry in build["entries"]:
            target = _target(assets if entry["area"] == "assets" else database, entry["path"])
            temporary = target.with_name("." + target.name + "." + batch.name + ".part")
            if os.path.lexists(temporary):
                if linked(temporary) or not temporary.is_file():
                    raise PlanError("本批遗留暂存文件不是普通文件。")
                temporary.unlink()
        current = tree_hashes(assets)
        for relative in current.keys() | build["final_assets"].keys():
            if current.get(relative) not in {record["baseline"].get(relative), build["final_assets"].get(relative)}:
                raise PlanError("本地 active 与本次构建不一致，请核对后重试。")
        _install(build["entries"], {"assets": assets, "database": database}, batch, on_diagnostic)
        _check_hashes(assets, build["final_assets"])
        record.update(state="local_verified", local_verified_at=timestamp())
        write_json(batch / "publish.json", record)
        _log(on_diagnostic, "info", "本地完整核验通过，开始通过 SSH 上传。")
        entries = [entry for entry in build["entries"] if entry["area"] == "database"]
        with transport_factory(settings, on_host_key=on_host_key) as transport:
            transport.publish(batch.name, entries, on_log=lambda level, message: _log(on_diagnostic, level, message))
        record.update(state="remote_verified", remote_verified_at=timestamp())
        write_json(batch / "publish.json", record)
        receipt = {"batch_id": batch.name, "local_verified": True, "remote_verified": True,
                   "songlist_sha256": build["songlist_sha256"]}
        path, task = sync_workspace_task(root, batch, read_json(stage / "songs/songlist"), record["actual"])
        confirm_uploaded(task, receipt)
        save_task(path, task, workspace_locked=True)
        receipt["server_task_file"] = str(path)
        write_json(batch / "receipt.json", receipt)
        record["state"] = "awaiting_stage03"
        write_json(batch / "publish.json", record)
    _log(on_diagnostic, "info", "02 上传核验完成，工作区完整保留；请完成 03 后确认提交。")
    return {"mode": "published", "batch_root": str(batch), "batch_id": batch.name, "version": build["version"],
            "server_task_file": str(path), "receipt": receipt}
