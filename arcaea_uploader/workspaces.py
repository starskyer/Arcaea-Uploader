"""Owned temporary batches, process locks and verified publication cleanup."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import stat

from .planner import PlanError


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def linked(path):
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0)
                                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def write_json(path, data):
    path = Path(path)
    if os.path.lexists(path) and linked(path):
        raise PlanError("工作区记录不能是链接。")
    temporary = path.with_name(path.name + ".tmp")
    if os.path.lexists(temporary) and linked(temporary):
        raise PlanError("工作区临时记录不能是链接。")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _locations(root, batch):
    root = Path(root).expanduser().resolve()
    supplied = Path(batch).absolute()
    if linked(supplied):
        raise PlanError("工作区不能是符号链接或目录联接。")
    batch = supplied.resolve(strict=True)
    if batch.parent != root or not re.fullmatch(r"preprocessed-[\w-]+", batch.name, flags=re.ASCII):
        raise PlanError("只能管理指定 releases 目录下的直接批次，不能操作根目录或外部路径。")
    if not batch.is_dir():
        raise PlanError("批次路径必须是目录。")
    return root, batch


def _read(path):
    if linked(path):
        raise PlanError("工作区记录不能是链接。")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PlanError("工作区记录必须是 JSON 对象。")
    return data


def load_workspace(root, batch, *, require_report=True):
    root, batch = _locations(root, batch)
    marker = batch / "workspace.json"
    owner = _read(marker) if marker.exists() else None
    if owner and (owner.get("owner") != "Arcaea-Uploader" or owner.get("schema_version") != 1):
        raise PlanError("不支持的工作区标记。")
    report = None
    try:
        report = _read(batch / "report.json")
        if report.get("mode") != "preprocess" or not isinstance(report.get("packages"), list):
            raise PlanError("不是可识别的预处理报告。")
        if (not isinstance(report.get("counts"), dict) or not isinstance(report.get("errors"), list)
                or any(not isinstance(p, dict) or not {"id", "source", "status", "actions"} <= p.keys()
                       or not isinstance(p["actions"], list) for p in report["packages"])):
            raise PlanError("预处理报告的内容不完整。")
        for key, expected in (("batch_root", batch), ("normalized_root", batch / "songs"),
                              ("report_file", batch / "report.json")):
            if not isinstance(report.get(key), str) or not report[key] or Path(report[key]).resolve() != expected:
                raise PlanError("报告中的工作区路径不匹配。")
    except (OSError, ValueError):
        if require_report or not owner:
            raise PlanError("批次报告缺失或损坏，无法载入；有归属标记的批次仍可手动清理。")
        report = owner
    source = report.get("input_root")
    if not isinstance(source, str) or not source or not Path(source).is_absolute():
        raise PlanError("缺少原始目录，无法核验工作区归属。")
    source = Path(source).resolve()
    if root.is_relative_to(source) or source.is_relative_to(batch):
        raise PlanError("原始目录与临时目录重叠，拒绝操作。")
    return report


def initialize_workspace(batch, input_root, import_version):
    write_json(Path(batch) / "workspace.json", {
        "owner": "Arcaea-Uploader", "schema_version": 1,
        "created_at": timestamp(), "input_root": str(input_root), "import_version": import_version,
    })


def workspace_marker(root, batch):
    """Read a validated legacy workspace without requiring its newer marker."""
    report = load_workspace(root, batch)
    path = Path(batch) / "workspace.json"
    if path.exists():
        return _read(path)
    return {"owner": "Arcaea-Uploader", "schema_version": 1,
            "created_at": timestamp(), "input_root": report["input_root"],
            "import_version": report.get("import_version", "")}


def snapshot_workspace_settings(batch, settings):
    """Save public configuration fields; never copy credentials or env values."""
    from .settings import default_settings
    path = Path(batch) / "workspace.json"
    marker = workspace_marker(Path(batch).parent, batch)
    marker["settings"] = {section: {key: deepcopy(settings[section][key]) for key in defaults}
                          for section, defaults in default_settings().items()}
    marker["updated_at"] = timestamp()
    write_json(path, marker)


def workspace_settings(root, batch, current, origin):
    from .settings import effective_settings
    marker = workspace_marker(root, batch)
    values = deepcopy(marker.get("settings") or current)
    if not marker.get("settings"):
        values["local"]["input_root"] = marker["input_root"]
        if marker.get("import_version"):
            values["import"]["version"] = marker["import_version"]
    publication = Path(batch) / "publish.json"
    if publication.is_file():
        record = _read(publication)
        binding = record.get("binding", {})
        if not marker.get("settings"):
            for key in ("import", "bundle"):
                if key in binding:
                    values[key] = deepcopy(binding[key])
            values["local"].update(binding.get("local", {}))
        # The immutable import policy also includes automatically learned pairs.
        values["import"] = deepcopy(binding.get("import", values["import"]))
        pending = record.get("pending_append") or {}
        values["local"]["input_root"] = pending.get("input_root", binding.get("local", {}).get(
            "input_root", values["local"]["input_root"]))
    values["local"]["release_root"] = str(Path(root).resolve())
    return effective_settings(values, origin)


def sync_workspace_task(root, batch, document, actual):
    from .server_tasks import (existing_task_path, load_task, make_task, read_reviewed, save_task, task_path)
    batch = Path(batch)
    old_path = existing_task_path(root, batch.name)
    previous = load_task(old_path) if old_path.is_file() else None
    task = make_task(batch.name, document, actual, previous)
    task = read_reviewed(task, batch / "prepared/active/songs/songlist")
    task["input_errors"] = []
    path = task_path(root, batch.name)
    save_task(path, task, workspace_locked=True)
    return path, task


def list_workspaces(root):
    root = Path(root).expanduser().resolve()
    if not root.exists():
        return []
    entries = []
    for batch in sorted(root.glob("preprocessed-*"), key=lambda p: p.name):
        row = {"path": str(batch), "name": batch.name, "report": None,
               "managed": False, "state": "无法识别", "error": ""}
        try:
            report = load_workspace(root, batch)
            row.update(managed=True, report=report)
            saved = _read(batch / "workspace.json").get("settings", {}) if (batch / "workspace.json").is_file() else {}
            ssh = saved.get("ssh", {})
            row["server"] = ((ssh["host"] + ":" + str(ssh.get("port", 22)) + " " + ssh.get("server_project_root", ""))
                             if ssh.get("host") else saved.get("local", {}).get("server_project_root", "未绑定"))
            row["state"] = ("中断 / 可继续" if not report.get("complete") else
                            "需处理 / 可继续" if not report.get("valid") else "预处理完成")
            publication = batch / "publish.json"
            if publication.is_file():
                publication_data = _read(publication)
                state = publication_data.get("state")
                row["state"] = {"preparing": "01 中断 / 继续准备", "blocked": "01 待处理 / 可继续",
                    "review": "01 完成 / 待审核", "building": "构建中断 / 继续 02",
                    "built": "已打包 / 继续 02", "local_verified": "本地完成 / 继续上传",
                    "remote_verified": "已上传 / 继续 02 交接", "awaiting_stage03": "02 已上传 / 待 03",
                    "committed_cleanup_pending": "已提交 / 待清理"}.get(state, row["state"])
                if state == "awaiting_stage03" and (batch / "server-task.json").is_file():
                    from .server_tasks import load_task, task_complete
                    task = load_task(batch / "server-task.json")
                    if task_complete(task):
                        row["state"] = "03 完成 / 待确认提交"
                    elif task["state"] in {"failed", "uncertain", "preflight", "running"}:
                        row["state"] = "03 未完成 / 可继续"
                pending = publication_data.get("pending_append")
                if pending:
                    row["state"] = "追加待处理 / 继续 01"
                    row["pending_errors"] = len(pending.get("errors", []))
                if (batch / "append-transaction.json").exists():
                    row["state"] = "追加提交中断 / 继续恢复"
        except (ValueError, OSError) as exc:
            row["error"] = str(exc)
            try:
                load_workspace(root, batch, require_report=False)
                row.update(managed=True, state="报告损坏 / 可清理")
            except (ValueError, OSError):
                pass
        entries.append(row)
    return entries


@contextmanager
def workspace_lock(root, batch):
    root, batch = _locations(root, batch)
    locks = root / ".locks"
    if os.path.lexists(locks) and linked(locks):
        raise PlanError("工作区锁目录不能是链接。")
    locks.mkdir(exist_ok=True)
    path = locks / (batch.name + ".lock")
    if os.path.lexists(path) and linked(path):
        raise PlanError("工作区锁文件不能是链接。")
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise PlanError("该批次正在被另一任务使用，请结束后再继续或清理。") from exc
        try:
            yield batch
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def ensure_plain_tree(batch):
    """Never traverse a reparse point during moves, cleanup or hash validation."""
    batch = Path(batch)
    if linked(batch):
        raise PlanError("工作副本不能是链接。")
    for current, dirs, files in os.walk(batch, followlinks=False):
        for name in dirs + files:
            path = Path(current) / name
            if linked(path):
                raise PlanError(f"工作区包含链接，需人工处理后再继续或清理：{path.name}")


def _delete(root, batch):
    # Both paths have been resolved and the direct-child boundary verified.
    root, batch = _locations(root, batch)
    load_workspace(root, batch, require_report=False)
    ensure_plain_tree(batch)
    remove_plain_tree(batch)


def remove_plain_tree(path):
    """Remove a caller-validated private tree, including Windows read-only files."""
    ensure_plain_tree(path)
    def retry_readonly(function, target, error):
        if not isinstance(error[1], PermissionError) or not Path(target).is_file() or linked(Path(target)):
            raise error[1]
        os.chmod(target, Path(target).stat().st_mode | stat.S_IWUSR)
        function(target)
    shutil.rmtree(path, onerror=retry_readonly)


def delete_workspace(root, batch):
    """Explicit manual deletion of one owned batch; never delete releases itself."""
    with workspace_lock(root, batch):
        _delete(root, batch)


def cleanup_published_workspace(root, batch, receipt):
    """Compatibility entry point; cleanup now requires the final 03 commit."""
    actual = _read(Path(batch) / "receipt.json") if (Path(batch) / "receipt.json").is_file() else None
    if not actual or any(actual.get(k) != receipt.get(k) for k in
                         ("batch_id", "local_verified", "remote_verified", "songlist_sha256")):
        raise PlanError("02 上传凭据不足以销毁工作区，请完成 03 并确认提交。")
    return commit_workspace(root, batch)


def verify_workspace_publication(root, batch, task):
    from .planner import digest
    root, batch = _locations(root, batch)
    load_workspace(root, batch)
    record = _read(batch / "publish.json")
    receipt = _read(batch / "receipt.json") if (batch / "receipt.json").is_file() else {}
    publication, source = task.get("publication") or {}, task.get("source") or {}
    expected = batch / "prepared/active/songs/songlist"
    if (receipt.get("batch_id") != batch.name or receipt.get("local_verified") is not True
            or receipt.get("remote_verified") is not True or task.get("id") != batch.name
            or publication.get("kind") != "verified" or not record.get("build")
            or receipt.get("songlist_sha256") != record["build"].get("songlist_sha256")
            or receipt.get("songlist_sha256") != source.get("sha256")
            or publication.get("songlist_sha256") != source.get("sha256")
            or Path(source.get("path", "")).resolve() != expected.resolve()
            or not expected.is_file() or digest(expected) != source["sha256"]):
        raise PlanError("尚未完成 02，或审核总表与已上传内容不一致；保留工作区。")
    if record.get("pending_append") or task.get("input_errors"):
        raise PlanError("工作区仍有未解决的 ERROR。")
    return receipt, record


def verify_workspace_api(root, batch, settings):
    marker = workspace_marker(root, batch)
    snapshot = marker.get("settings", {})
    expected = snapshot.get("api", {}).get("base_url", "").rstrip("/")
    selected = settings["api"]["base_url"].rstrip("/")
    if expected and expected != selected:
        raise PlanError("管理 API 与本工作区保存的服务器不一致，请恢复工作区配置后再执行 03。")


def bind_workspace_api(root, batch, settings):
    with workspace_lock(root, batch):
        verify_workspace_api(root, batch, settings)
        path = Path(batch) / "workspace.json"
        marker = workspace_marker(root, batch)
        if not marker.get("settings"):
            snapshot_workspace_settings(batch, settings)
            marker = _read(path)
        marker["settings"]["api"] = {key: settings["api"][key] for key in ("base_url", "token_env")}
        marker["settings"]["api"]["use_system_proxy"] = settings["api"].get("use_system_proxy", True)
        write_json(path, marker)


def commit_workspace(root, batch, *, expected_revision=None):
    """Explicit final commit: verified 02 + current successful/skipped 03."""
    from .server_tasks import (archive_info, load_task, task_complete, task_lock)
    from .publishing import _check_hashes
    root, batch = _locations(root, batch)
    with workspace_lock(root, batch):
        report = load_workspace(root, batch)
        if report.get("complete") is not True or report.get("valid") is not True:
            raise PlanError("批次尚未完成资源处理，保留工作区。")
        ensure_plain_tree(batch)
        task_file = batch / "server-task.json"
        if not task_file.is_file():
            raise PlanError("工作区缺少 03 任务，不能提交清理。")
        with task_lock(task_file):
            task = load_task(task_file)
            if expected_revision is not None and task["revision"] != expected_revision:
                raise PlanError("确认期间定数或操作选项已改变，请重新检查后提交。")
            receipt, publication = verify_workspace_publication(root, batch, task)
            if not task_complete(task):
                raise PlanError("03 尚未成功完成，或定数 / 选项已改变，请先执行 03；保留工作区。")
            _check_hashes(batch / "prepared/active", publication["build"]["final_assets"])
            _check_hashes(batch / "prepared/database", publication["prepared_database"])
            _check_hashes(batch / "songs", publication["normalized_sha256"])
            snapshot = workspace_marker(root, batch).get("settings")
            if snapshot and any(task["options"].values()):
                verify_workspace_api(root, batch, {"api": {"base_url": task.get("endpoint", "")}})
            history = root / ".history"
            archived = root / ".server-tasks"
            for directory in (history, archived):
                if os.path.lexists(directory) and linked(directory):
                    raise PlanError("工作区归档目录不能是链接。")
                directory.mkdir(exist_ok=True)
            archive_task = archived / (batch.name + ".json")
            with task_lock(archive_task):
                prior = _read(archive_task) if archive_task.is_file() else None
                prior_archive = archive_info(prior, archive_task) if prior else None
                if prior_archive:
                    if prior.get("completion_signature") != task["completion_signature"]:
                        raise PlanError("已有提交归档与当前任务不一致，拒绝覆盖原记录。")
                    completed_at = prior_archive.get("completed_at") or timestamp()
                else:
                    completed_at = timestamp()
                    snapshot_task = deepcopy(task)
                    snapshot_task["archive"] = {"kind": "workspace_commit", "completed_at": completed_at,
                        "history_file": str(history / (batch.name + ".json")),
                        "api_base_url": task.get("endpoint") or (snapshot or {}).get("api", {}).get("base_url", "")}
                    write_json(archive_task, snapshot_task)
            path = history / (batch.name + ".json")
            record = {"batch_id": batch.name, "completed_at": completed_at, "committed": True,
                      "local_verified": True, "remote_verified": True, "bundle_version": publication["build"]["version"],
                      "songlist_sha256": receipt["songlist_sha256"], "server_operations": task["state"],
                      "completion_signature": task["completion_signature"], "options": task["options"],
                      "server_task_file": str(archive_task.relative_to(root)), "cleanup_complete": False}
            write_json(path, record)
            publication["state"] = "committed_cleanup_pending"
            write_json(batch / "publish.json", publication)
        # Close the task-file lock before deleting its directory on Windows.
        _delete(root, batch)
        record["cleanup_complete"] = True
        write_json(path, record)
        return path
