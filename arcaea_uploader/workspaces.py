"""Owned temporary batches, process locks and verified publication cleanup."""

from contextlib import contextmanager
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
            row["state"] = ("中断 / 可继续" if not report.get("complete") else
                            "需处理 / 可继续" if not report.get("valid") else "预处理完成")
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
    shutil.rmtree(batch)


def delete_workspace(root, batch):
    """Explicit manual deletion of one owned batch; never delete releases itself."""
    with workspace_lock(root, batch):
        _delete(root, batch)


def cleanup_published_workspace(root, batch, receipt):
    """Stage 02 cleans verified uploads; optional stage 03 keeps a small task.

    The publishing executor is not wired yet. It must supply verified facts;
    neither a successful preflight nor a UI click constitutes this receipt.
    """
    root, batch = _locations(root, batch)
    if (not isinstance(receipt, dict) or receipt.get("batch_id") != batch.name or receipt.get("local_verified") is not True
            or receipt.get("remote_verified") is not True):
        raise PlanError("尚未确认本地和远端资源，保留临时批次供重试。")
    post_file = receipt.get("server_task_file")
    post_relative = None
    if post_file:
        from .server_tasks import load_task
        post_path = Path(post_file).resolve(strict=True)
        expected = root / ".server-tasks" / (batch.name + ".json")
        if post_path != expected or linked(Path(post_file)) or linked(post_path.parent):
            raise PlanError("03 任务必须位于本工作区独立的 .server-tasks 目录。")
        post = load_task(post_path)
        if (post["id"] != batch.name or (post.get("publication") or {}).get("kind") != "verified"
                or not post.get("source")
                or post["publication"].get("songlist_sha256") != post["source"].get("sha256")
                or receipt.get("songlist_sha256") != post["source"].get("sha256")):
            raise PlanError("尚未保存与 02 上传结果绑定的 03 任务。")
        reviewed = Path(post["source"]["path"])
        if reviewed.is_file():
            from .planner import digest
            if digest(reviewed) != post["source"]["sha256"]:
                raise PlanError("02 核验后审核总表发生变化，保留工作区。")
        post_relative = str(post_path.relative_to(root))
    elif (receipt.get("server_operations") != "skipped"
          and receipt.get("refresh") not in {"succeeded", "server_stopped_confirmed"}):
        # Accept old completed receipts, but never lose newly deferred work.
        raise PlanError("清理前须保存独立 03 任务，或明确跳过 03。")
    with workspace_lock(root, batch):
        report = load_workspace(root, batch)
        if report.get("complete") is not True or report.get("valid") is not True:
            raise PlanError("批次尚未完成资源处理，保留工作区。")
        history = root / ".history"
        if os.path.lexists(history) and linked(history):
            raise PlanError("发布记录目录不能是链接。")
        history.mkdir(exist_ok=True)
        path = history / (batch.name + ".json")
        record = {"batch_id": batch.name, "completed_at": timestamp(),
                  "local_verified": True, "remote_verified": True,
                  "server_operations": "pending" if post_file else "skipped",
                  "server_task_file": post_relative, "cleanup_complete": False}
        write_json(path, record)
        _delete(root, batch)
        record["cleanup_complete"] = True
        write_json(path, record)
        return path
