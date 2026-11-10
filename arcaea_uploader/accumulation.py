"""Accumulate stage-01 imports without replacing an existing human review."""

from collections import Counter
from copy import deepcopy
from pathlib import Path
import shutil
from tempfile import mkdtemp
from time import perf_counter, sleep

from .diagnostics import emit, summarize
from .import_rules import actual_chart_classes, portable_id
from .metadata import order_song_fields
from .planner import PlanError, build_plan, digest, merge_songlist, read_json
from .preprocess import preprocess_collection, scan_collection
from .private_files import clone_candidate, copy_writable, replace_copy
from .review_merge import retain_review_edits
from .publishing import (_binding, _check_hashes, _config_for_plan, _database, _download_baseline, _restore_inferred_policy,
    _log, _render_previews, _safe_roots, apply_prepared_files, asset_changes, load_preparation, tree_hashes)
from .settings import effective_settings, normalize_settings, remember_background_pairs
from .workspaces import (ensure_plain_tree, load_workspace, remove_plain_tree, snapshot_workspace_settings,
                         sync_workspace_task, timestamp, workspace_lock, write_json)


METADATA = ("publish.json", "plan.json", "report.json")


def source_roots(record):
    return record.get("source_roots") or [{"path": record["binding"]["local"]["input_root"],
                                           "sha256": record["input_sha256"]}]


def report_source_changes(record, callback, *, incoming=None):
    """Author packages are provenance; completed imports use private snapshots.

    During an explicit 01, the selected source is checked separately before and
    after copying. Other sources can be edited without invalidating that copy.
    """
    reports = []
    for source in source_roots(record):
        path = Path(source["path"])
        if incoming is not None and path.is_relative_to(incoming):
            continue
        try:
            current = tree_hashes(path)
        except (OSError, PlanError) as error:
            reports.append({"path": str(path), "error": str(error)})
            _log(callback, "info", f"来源目录暂时无法核对：{path}；继续使用已准备的工作区副本。")
            continue
        previous = source["sha256"]
        if incoming is not None and incoming.is_relative_to(path):
            prefix = incoming.relative_to(path).as_posix() + "/"
            current = {k: v for k, v in current.items() if not k.startswith(prefix)}
            previous = {k: v for k, v in previous.items() if not k.startswith(prefix)}
        changes = asset_changes(previous, current)
        if changes:
            reports.append({"path": str(path), "changes": changes})
            _log(callback, "info", f"来源目录已有修改：{path}；本轮未重新导入的曲目继续使用工作区副本。"
                 "这些来源修改不会自动进入本批，如需采用请在 02 前选择该来源执行 01。")
            labels = {"add": "新增", "modify": "修改", "remove": "删除"}
            for change in changes:
                _log(callback, "info", f"来源{labels[change['action']]}：{path / change['path']}")
    return reports


def _rename_private(source, target):
    """Retry transient Windows locks and explain persistent sharing failures."""
    for delay in (0.0, 0.2, 0.5):
        if delay:
            sleep(delay)
        try:
            source.rename(target)
            return
        except PermissionError as error:
            if delay == 0.5:
                raise PlanError(f"无法切换追加目录：{source}\n"
                    "Windows 拒绝重命名，目录或其中的文件可能正被编辑器、图片/音频播放器或终端占用，"
                    "也可能没有重命名权限。请先保存并关闭打开此工作区文件的程序及位于其中的终端，"
                    "再在工作区管理中继续所选批次，重试 01；无需删除工作区或撤销审核修改。") from error


def _remove_owned(batch, path):
    batch, path = Path(batch).resolve(), Path(path).resolve()
    if path == batch or not path.is_relative_to(batch):
        raise PlanError("追加过程目录越界。")
    if path.exists():
        ensure_plain_tree(path)
        remove_plain_tree(path)


def recover_append(batch):
    """Caller holds the workspace lock. Roll back an interrupted commit."""
    batch = Path(batch).resolve()
    journal = batch / "append-transaction.json"
    if not journal.exists():
        return
    load_workspace(batch.parent, batch)
    ensure_plain_tree(batch)
    data = read_json(journal)
    relative = Path(data.get("attempt", ""))
    if relative.is_absolute() or len(relative.parts) != 2 or relative.parts[0] != "additions" or ".." in relative.parts:
        raise PlanError("追加恢复记录的路径无效。")
    attempt = batch / relative
    if not attempt.is_dir() or not attempt.resolve().is_relative_to(batch):
        raise PlanError("追加恢复副本缺失，请保留批次并人工检查。")
    if data.get("committed"):
        for name in ("previous-prepared", "previous-songs", "preprocessed"):
            _remove_owned(batch, attempt / name)
    else:
        for name in ("prepared", "songs"):
            previous = attempt / ("previous-" + name)
            if previous.exists():
                current = batch / name
                if current.exists():
                    destination = attempt / ("uncommitted-" + name)
                    if destination.exists():
                        _remove_owned(batch, destination)
                    _rename_private(current, destination)
                _rename_private(previous, current)
        for name in METADATA:
            snapshot = attempt / "before" / name
            if not snapshot.is_file():
                raise PlanError("追加恢复记录缺少原始快照。")
            write_json(batch / name, read_json(snapshot))
    journal.unlink()


def _commit(batch, attempt, record, plan, report):
    before = attempt / "before"
    before.mkdir()
    for name in METADATA:
        shutil.copy2(batch / name, before / name)
    for name in ("songlist", "packlist", "unlocks"):
        shutil.copy2(batch / "prepared/active/songs" / name, before / name)
    journal = batch / "append-transaction.json"
    transaction = {"attempt": attempt.relative_to(batch).as_posix(), "committed": False}
    write_json(journal, transaction)
    try:
        for name in ("prepared", "songs"):
            _rename_private(batch / name, attempt / ("previous-" + name))
            _rename_private(attempt / name, batch / name)
        for name, value in (("report.json", report), ("plan.json", plan), ("publish.json", record)):
            write_json(batch / name, value)
        transaction["committed"] = True
        write_json(journal, transaction)
    except Exception:
        recover_append(batch)
        raise
    # This also works after a crash between marking committed and cleanup.
    recover_append(batch)


def _cache(record, report, batch):
    cache = {(c["kind"], c["source_sha256"], c["target"]): deepcopy(c)
             for c in record.get("normalization_cache", [])}
    for package in report["packages"]:
        if package["status"] != "normalized":
            continue
        for action in package["actions"]:
            output = batch / "songs" / package["id"] / action["target"]
            if output.is_file():
                cache[(action["kind"], action["source_sha256"], action["target"])] = {
                    "kind": action["kind"], "source_sha256": action["source_sha256"], "target": action["target"],
                    "output": str(output), "sha256": digest(output)}
    return cache


def _combined_report(previous, incoming, plan, batch, canonical):
    rows = {row["id"]: row for row in plan["merged_songlist"]["songs"]}
    packages = {p["id"]: deepcopy(p) for p in previous["packages"] if p["status"] == "normalized"}
    for package in incoming["packages"]:
        if package["status"] != "normalized":
            continue
        old = packages.get(package["id"], {})
        actions = {a["target"]: a for a in old.get("actions", [])}
        actions.update({a["target"]: a for a in package["actions"]})
        packages[package["id"]] = deepcopy(package)
        packages[package["id"]]["actions"] = list(actions.values())
    for sid, package in packages.items():
        package.update(output=str(batch / "songs" / sid), songlist=deepcopy(rows[sid]),
                       output_sha256=tree_hashes(canonical / sid))
    report = deepcopy(incoming)
    report.update(mode="preprocess", batch_root=str(batch), report_file=str(batch / "report.json"),
                  normalized_root=str(batch / "songs"), packages=list(packages.values()),
                  counts=dict(Counter(p["status"] for p in packages.values())), complete=True, valid=True,
                  diagnostics=previous["diagnostics"] + incoming["diagnostics"], updated_at=timestamp())
    summarize(report)
    return report


def append_update(data, origin, batch, *, on_diagnostic=None):
    settings = effective_settings(data, origin)
    _safe_roots(settings)
    local = settings["local"]
    root, batch = Path(local["release_root"]), Path(batch).resolve()
    restart = False
    with workspace_lock(root, batch):
        load_workspace(root, batch)
        recover_append(batch)
        record = load_preparation(root, batch)
        if record.get("build") or record.get("state") == "building":
            raise PlanError("该批次已开始打包，不能继续追加；请执行 02，或明确新建批次。")
        if "prepared_assets" not in record:
            restart = True  # Legacy/incomplete initial 01, no complete review to append to.
        else:
            return _append_locked(settings, origin, batch, record, data, on_diagnostic)
    if restart:
        from .publishing import prepare_update
        return prepare_update(data, origin, resume_from=batch, on_diagnostic=on_diagnostic, _rebuild=True)


def _append_locked(settings, origin, batch, record, raw_settings, callback):
    started = perf_counter()
    _log(callback, "info", "01 追加：核验已有暂存内容与来源…")
    requested_import = normalize_settings(raw_settings, origin)["import"]
    _restore_inferred_policy(settings, requested_import, record)
    current, previous = _binding(settings), record["binding"]
    if settings["import"]["version"] != previous["import"]["version"]:
        _log(callback, "info", f"本轮导入 version 改为 {settings['import']['version']}；保留已审核的逐曲 version。")
    if (current["bundle"] != previous["bundle"] or any(current["local"][key] != value
            for key, value in previous["local"].items() if key != "input_root")):
        raise PlanError("追加时须沿用原批次的本地资源路径、占位谱面和应用版本选项。")
    incoming = Path(settings["local"]["input_root"])
    pending = record.get("pending_append")
    if pending and Path(pending["input_root"]) != incoming:
        raise PlanError("上次追加仍有 ERROR，请选择该待追加目录修复后重试，或新建独立批次。")
    assets, database = Path(settings["local"]["assets_root"]), _database(settings)
    _check_hashes(assets, record["baseline"], except_paths={"songs/packlist", "songs/unlocks"})
    if _download_baseline(database, record["ids"]) != record["database_baseline"]:
        raise PlanError("本地下载资源基线发生变化，请保留当前审核副本并新建批次。")
    reviewed_assets = tree_hashes(batch / "prepared/active")
    _check_hashes(batch / "prepared/database", record["prepared_database"])
    _check_hashes(batch / "songs", record["normalized_sha256"])
    report_source_changes(record, callback, incoming=incoming)
    previous_report, previous_plan = load_workspace(settings["local"]["release_root"], batch), read_json(batch / "plan.json")
    reviewed = read_json(batch / "prepared/active/songs/songlist")
    merge_songlist(reviewed, [])  # Validate JSON metadata before using it as a baseline.
    reviewed_rows = {row["id"]: row for row in reviewed["songs"]}
    original_rows = {row["id"]: row for row in previous_plan["merged_songlist"]["songs"]}
    if reviewed_rows.keys() != original_rows.keys() or any(
            row["idx"] != original_rows[sid]["idx"] for sid, row in reviewed_rows.items()):
        raise PlanError("审核中不能删除、重命名曲目或修改 idx；追加曲目应放在输入目录交由 01 处理。")
    if any(row != original_rows[sid] for sid, row in reviewed_rows.items() if sid not in record["ids"]):
        raise PlanError("审核修改了本批范围外的曲目，请恢复该条目后再追加。")
    review_hashes = {name: digest(batch / "prepared/active/songs" / name) for name in ("songlist", "packlist", "unlocks")}
    additions = batch / "additions"
    additions.mkdir(exist_ok=True)
    attempt = Path(mkdtemp(prefix="import-", dir=additions))
    record["pending_append"] = {"input_root": str(incoming), "attempt": attempt.relative_to(batch).as_posix(),
                                 "started_at": timestamp(), "errors": []}
    write_json(batch / "publish.json", record)
    try:
        result = _prepare_candidate(settings, batch, attempt, record, previous_report, previous_plan, reviewed,
                                    review_hashes, requested_import, callback, reviewed_assets)
        _log(callback, "info", f"01 追加总耗时 {perf_counter() - started:.1f} 秒。")
        return result
    except Exception as error:
        # A failed addition leaves the previous working files and review intact,
        # but cannot silently disappear from the eventual publication.
        if not (batch / "append-transaction.json").exists():
            current_record = read_json(batch / "publish.json")
            if current_record.get("pending_append"):
                current_record["pending_append"]["errors"] = [str(error)]
                write_json(batch / "publish.json", current_record)
        raise


def _prepare_candidate(settings, batch, attempt, record, old_report, old_plan, reviewed, review_hashes, requested_import, callback, reviewed_assets):
    local = settings["local"]
    assets, database, incoming = Path(local["assets_root"]), _database(settings), Path(local["input_root"])
    input_hashes = tree_hashes(incoming)
    stage, downloads = attempt / "prepared/active", attempt / "prepared/database"
    _log(callback, "info", f"追加到批次 {batch.name}；保留此前 {len(record['ids'])} 首曲目与已保存的审核改动。")
    copying = perf_counter()
    clone_candidate(batch / "prepared", attempt / "prepared")
    _log(callback, "info", f"事务副本就绪（{perf_counter() - copying:.1f} 秒）；扫描本次增量…")
    policy = {"master": reviewed, "assets_root": stage, "import_version": settings["import"]["version"],
              "background_pairs": settings["import"]["background_pairs"],
              "background_pair_conflicts": settings["import"]["background_pair_conflicts"],
              "initialize_background_pairs": not settings["import"]["background_pairs_initialized"]}
    # Discover IDs first, then add only the needed local song baselines. Never
    # copy the entire server database or point a link into the live deployment.
    discovery = scan_collection(incoming, existing_songs=database / "songs", **policy)
    new_ids = list(dict.fromkeys(p["id"] for p in discovery["packages"] if portable_id(p["id"])))
    extra_baseline = _download_baseline(database, [sid for sid in new_ids if sid not in record["ids"]])
    for relative in extra_baseline:
        target = downloads / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        copy_writable(database / relative, target)
    cache = _cache(record, old_report, batch)
    incoming_report = preprocess_collection(incoming, attempt / "preprocessed", existing_songs=downloads / "songs",
        ffmpeg_path=local["ffmpeg_path"], artifact_cache=cache.values(), on_diagnostic=callback, **policy)
    if incoming_report["valid"]:
        remember_background_pairs(settings["import"], incoming_report.get("learned_background_pairs", {}))
    candidate_settings = deepcopy(settings)
    candidate_settings["local"].update(assets_root=str(stage), server_project_root=str(attempt / "prepared"),
                                        database_relative_path="database")
    plan = build_plan(_config_for_plan(candidate_settings, incoming_report["normalized_root"], attempt / "plan-config.json"))
    plan["diagnostics"] = incoming_report["diagnostics"] + plan["diagnostics"]
    summarize(plan)
    write_json(attempt / "plan.json", plan)
    if plan["errors"]:
        raise PlanError("追加未完成，原暂存资源保留；请修复后重试：\n" + "\n".join(plan["errors"]))
    protected = retain_review_edits(old_plan["merged_songlist"], reviewed, plan["merged_songlist"],
                                   record.get("review_protected_paths", {}), record["ids"])
    merge_songlist(plan["merged_songlist"], [])
    ids = list(dict.fromkeys(record["ids"] + [song["id"] for song in plan["songs"]]))
    for row in plan["merged_songlist"]["songs"]:
        if row["id"] in ids:
            order_song_fields(row)
    _log(callback, "info", f"保留人工审核字段 {sum(map(len, protected.values()))} 处（含主动删除的字段）。")
    protected_assets = set(record.get("manual_asset_paths", [])) | {
        item["path"] for item in asset_changes(record["prepared_assets"], reviewed_assets)}
    apply_prepared_files(plan, stage, downloads, attempt, preserve_assets=protected_assets, callback=callback)
    write_json(stage / "songs/songlist", plan["merged_songlist"])
    updated = deepcopy(record)
    updated.update(ids=ids, review_protected_paths=protected, manual_asset_paths=sorted(protected_assets),
                   actual={sid: sorted(actual_chart_classes(downloads / "songs" / sid)) for sid in ids})
    _render_previews(attempt, updated, plan["merged_songlist"], local["ffmpeg_path"], callback, preserve_manual=True)
    canonical = attempt / "songs"
    clone_candidate(batch / "songs", canonical)
    normalized = Path(incoming_report["normalized_root"])
    for package in incoming_report["packages"]:
        if package["status"] != "normalized":
            continue
        destination = canonical / package["id"]
        destination.mkdir(exist_ok=True)
        for source in Path(package["output"]).iterdir():
            if source.name != "songlist":
                replace_copy(source, destination / source.name)
    rows = {row["id"]: row for row in plan["merged_songlist"]["songs"]}
    for sid in ids:
        write_json(canonical / sid / "songlist", {"songs": [rows[sid]]})
    for entry in _cache({}, incoming_report, normalized.parent).values():
        # _cache expects <root>/songs; preprocessing stores precisely that layout.
        entry["output"] = str(batch / "songs" / Path(entry["output"]).relative_to(normalized))
        cache[(entry["kind"], entry["source_sha256"], entry["target"])] = entry
    original_live = {row["id"] for row in read_json(assets / "songs/songlist")["songs"]}
    def relocate(entry):
        entry = deepcopy(entry)
        source = Path(entry["source"])
        if source.is_relative_to(normalized):
            entry["source"] = str(batch / "songs" / source.relative_to(normalized))
        return entry
    files = {(f["area"], f["path"]): deepcopy(f) for f in old_plan["files"]}
    files.update({(f["area"], f["path"]): relocate(f) for f in plan["files"]})
    combined = deepcopy(plan)
    combined.update(mode="prepare", batch_root=str(batch), preprocessing_report=str(batch / "report.json"),
        review_songlist=str(batch / "prepared/active/songs/songlist"), prepared_assets=str(batch / "prepared/active"),
        prepared_database=str(batch / "prepared/database"), files=list(files.values()), actual=updated["actual"],
        songs=[{"id": sid, "idx": rows[sid]["idx"], "kind": "incremental" if sid in original_live else "new"} for sid in ids],
        diagnostics=old_plan["diagnostics"] + plan["diagnostics"], valid=True, publishable=False)
    combined["learned_background_pairs"] = {**old_plan.get("learned_background_pairs", {}),
                                           **incoming_report.get("learned_background_pairs", {})}
    emit(combined["diagnostics"], "info", "publication.accumulated",
         f"本次追加 {len(plan['songs'])} 首，批次累计 {len(ids)} 首；未打包 bundle。")
    summarize(combined)
    report = _combined_report(old_report, incoming_report, combined, batch, canonical)
    sources = {s["path"]: deepcopy(s) for s in source_roots(record)}
    sources[str(incoming)] = {"path": str(incoming), "sha256": input_hashes}
    for source in sources.values():
        path = Path(source["path"])
        if path.is_relative_to(incoming) or incoming.is_relative_to(path):
            source["sha256"] = tree_hashes(path)
    updated.update(state="review", binding=_binding(settings), requested_import=requested_import,
        input_sha256=input_hashes, source_roots=list(sources.values()), normalized_sha256=tree_hashes(canonical),
        prepared_assets=tree_hashes(stage), prepared_database=tree_hashes(downloads),
        database_baseline={**record["database_baseline"], **extra_baseline}, normalization_cache=list(cache.values()),
        updated_at=timestamp(), additions=record.get("additions", 0) + 1)
    updated.pop("pending_append", None)
    _log(callback, "info", "增量资源准备完成；复核来源与审核文档后保存工作区…")
    _check_hashes(incoming, input_hashes)
    _check_hashes(assets, record["baseline"], except_paths={"songs/packlist", "songs/unlocks"})
    _check_hashes(batch / "prepared/database", record["prepared_database"])
    _check_hashes(batch / "songs", record["normalized_sha256"])
    if _download_baseline(database, ids) != updated["database_baseline"]:
        raise PlanError("追加期间本地下载资源基线发生变化。")
    for name, expected in review_hashes.items():
        if digest(batch / "prepared/active/songs" / name) != expected:
            raise PlanError("追加期间审核文档再次保存，未覆盖该改动；请关闭编辑器后重试追加。")
    _check_hashes(batch / "prepared/active", reviewed_assets)
    _commit(batch, attempt, updated, combined, report)
    snapshot_workspace_settings(batch, settings)
    sync_workspace_task(Path(local["release_root"]), batch, combined["merged_songlist"], updated["actual"])
    _log(callback, "info", f"01 累积完成：{len(ids)} 首；审核总表路径保持不变。")
    return combined
