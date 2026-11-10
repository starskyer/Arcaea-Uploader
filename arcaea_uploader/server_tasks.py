"""Optional stage 03: durable metadata tasks and ordered API operations."""

from copy import deepcopy
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
from uuid import uuid4

from .api_client import ApiError
from .import_rules import portable_id
from .planner import PlanError, digest, read_json, song_rows
from .workspaces import linked, timestamp, write_json


OPTIONS = {"refresh_resources": True, "purchases": True, "constants": True, "refresh_rating": True}
RESOURCE_OPERATIONS = ("refresh_content_bundle_cache", "refresh_song_file_cache", "refresh_world_map_cache")
RATING_OPERATION = "refresh_all_score_rating"
PAST_FROM, PAST_TO = 946684800000, 946684860000  # 2000-01-01; milliseconds.


def fingerprint(data):
    return sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()


def completion_signature(task):
    values = {key: task.get(key) for key in
        ("songs", "actual", "constants", "options", "source", "publication", "endpoint")}
    for key in ("purchase_overrides", "purchase_skip_confirmation", "followup"):
        if task.get(key):
            values[key] = task[key]
    return fingerprint(values)


def finish_task(task, *, skipped=False):
    _editable(task)
    if skipped and any(task["options"].values()):
        raise PlanError("仍有启用的 03 操作，不能标记全部跳过。")
    task["state"] = "skipped" if skipped else "complete"
    task["completion_signature"] = completion_signature(task)


def task_complete(task):
    return (task.get("state") in {"complete", "skipped"}
            and task.get("completion_signature") == completion_signature(task)
            and (task["state"] != "skipped" or not any(task["options"].values())))


def archive_info(task, path=None):
    """Recognize explicit archives and legacy workspace commit records."""
    if task.get("archive"):
        return task["archive"]
    if path is not None:
        path = Path(path).resolve()
        if path.parent.name == ".server-tasks" and path.name == task.get("id", "") + ".json":
            history = path.parent.parent / ".history" / path.name
            if history.is_file():
                if linked(history):
                    raise PlanError("提交记录不能是链接。")
                record = read_json(history)
                if record.get("committed") is True and record.get("batch_id") == task["id"]:
                    return {"kind": "workspace_commit", "completed_at": record.get("completed_at"),
                            "history_file": str(history), "api_base_url": task.get("endpoint", ""),
                            "modified_after_commit": bool(record.get("completion_signature")
                                and record["completion_signature"] != completion_signature(task))}
    return None


def _editable(task, path=None):
    if archive_info(task, path):
        raise PlanError("已归档的 03 任务只读；请先创建后续任务，再修改或执行。")


def english_name(row):
    localized = row.get("title_localized")
    value = localized.get("en") if isinstance(localized, dict) else None
    return (value if isinstance(value, str) and value.strip() else row.get("title")) or ""


def parse_constant(value):
    if value is None or isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, bool) or not re.fullmatch(r"-?\d+(?:\.\d)?", str(value).strip()):
        raise PlanError("定数须为整数或一位小数；可填正数、0 或 -1，未知请留空。")
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise PlanError("定数格式无效。") from None
    if (not number.is_finite() or number < 0 and number != -1
            or not math.isfinite(float(number))):
        raise PlanError("定数可填正数、0 或 -1；0 / -1 不计评分，未知请留空。")
    return 0.0 if number == 0 else float(number)


def _rows(document):
    result = {}
    for row in song_rows(document):
        sid = row.get("id")
        if not portable_id(sid) or sid.casefold() in result:
            raise PlanError("审核 songlist 含无效或重复曲目 ID。")
        result[sid.casefold()] = row
    return result


def make_task(task_id, document, actual, previous=None):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
        raise PlanError("03 任务标识无效。")
    rows = _rows(document)
    selected = []
    for sid, classes in actual.items():
        if not portable_id(sid) or any(type(c) is not int or c not in range(5) for c in classes):
            raise PlanError("实际谱面难度清单无效。")
        row = rows.get(sid.casefold())
        if row is None or row["id"] != sid:
            raise PlanError(f"审核 songlist 缺少本批曲目 {sid}，或 ID 大小写不匹配。")
        selected.append({key: deepcopy(row[key]) for key in ("id", "set", "purchase", "title", "title_localized") if key in row})
    if not selected:
        raise PlanError("没有本批曲目，不能建立 03 任务。")
    task = {"owner": "Arcaea-Uploader/server-task", "schema_version": 1, "id": task_id,
            "created_at": timestamp(), "songs": selected,
            "actual": {sid: sorted(set(classes)) for sid, classes in actual.items()},
            "constants": {}, "options": OPTIONS.copy(), "progress": {}, "managed_purchases": {},
            "purchase_overrides": {}, "purchase_skip_confirmation": None,
            "state": "draft", "source": None, "publication": None, "revision": 0, "input_errors": []}
    if previous:
        if previous.get("id") != task_id:
            raise PlanError("03 任务标识不匹配。")
        for key in ("created_at", "constants", "options", "progress", "managed_purchases", "endpoint", "revision", "input_errors", "completion_signature", "purchase_overrides", "purchase_skip_confirmation", "archive", "followup"):
            if key in previous:
                task[key] = deepcopy(previous[key])
        if selected == previous["songs"] and task["actual"] == previous["actual"]:
            for key in ("source", "publication", "state"):
                task[key] = deepcopy(previous.get(key))
    # A removed difficulty must not leave a writable constant behind.
    task["constants"] = {sid: {str(c): task["constants"].get(sid, {}).get(str(c)) for c in classes}
                         for sid, classes in task["actual"].items()}
    task["purchase_overrides"] = {sid: value for sid, value in task["purchase_overrides"].items() if sid in actual}
    update_purchase_candidates(task)
    return task


def task_path(release_root, task_id, *, create=True):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
        raise PlanError("03 任务标识无效。")
    root = Path(release_root).resolve()
    workspace = root / task_id
    if workspace.is_dir() and (workspace / "publish.json").is_file():
        from .workspaces import load_workspace
        load_workspace(root, workspace)
        return workspace / "server-task.json"
    directory = root / ".server-tasks"
    if directory.exists() and linked(directory):
        raise PlanError("03 任务目录不能是链接。")
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory / (task_id + ".json")


def existing_task_path(release_root, task_id):
    path = task_path(release_root, task_id, create=False)
    legacy = Path(release_root).resolve() / ".server-tasks" / (task_id + ".json")
    return path if path.exists() or not legacy.exists() else legacy


@contextmanager
def task_lock(path):
    path = Path(path).with_suffix(".lock")
    if os.path.lexists(path) and linked(path):
        raise PlanError("03 任务锁不能是链接。")
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if not stream.tell():
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
        except OSError:
            raise PlanError("该 03 任务正在执行，请完成后再编辑或重试。") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def save_task(path, task, *, locked=False, workspace_locked=False):
    if task.get("owner") != "Arcaea-Uploader/server-task":
        raise PlanError("不是有效的 03 任务。")
    path = Path(path)
    _editable(task, path)
    if path.parent.exists() and linked(path.parent):
        raise PlanError("03 任务目录不能是链接或目录联接。")
    if not locked:
        if path.name == "server-task.json" and (path.parent / "publish.json").is_file() and not workspace_locked:
            from .workspaces import workspace_lock
            with workspace_lock(path.parent.parent, path.parent):
                return save_task(path, task, workspace_locked=True)
        with task_lock(path):
            return save_task(path, task, locked=True)
    update_purchase_candidates(task)
    if task.get("state") in {"complete", "skipped"} and not task_complete(task):
        task["state"] = "draft"
    if path.exists():
        current = read_json(path)
        _editable(current, path)
        if (current.get("owner") != task["owner"] or current.get("id") != task["id"]
                or current.get("revision", 0) != task.get("revision", 0)):
            raise PlanError("03 任务已在其他窗口更新，请重新载入。")
    revision, updated_at = task.get("revision", 0) + 1, timestamp()
    write_json(path, {**task, "revision": revision, "updated_at": updated_at})
    task.update(revision=revision, updated_at=updated_at)


def load_task(path):
    path = Path(path)
    if linked(path):
        raise PlanError("03 任务文件不能是链接。")
    task = read_json(path)
    if (not isinstance(task, dict) or task.get("owner") != "Arcaea-Uploader/server-task"
            or task.get("schema_version") != 1):
        raise PlanError("不是支持的 03 任务文件。")
    if (not isinstance(task.get("actual"), dict) or not isinstance(task.get("constants"), dict)
            or any(not isinstance(task.get(key), dict) for key in ("options", "progress", "managed_purchases"))
            or not isinstance(task.get("input_errors", []), list)
            or type(task.get("revision", 0)) is not int):
        raise PlanError("03 任务的数据结构无效。")
    if (not isinstance(task.get("purchase_overrides", {}), dict)
            or task.get("purchase_skip_confirmation") is not None and not re.fullmatch(
                r"[0-9a-f]{64}", str(task["purchase_skip_confirmation"]))):
        raise PlanError("购买项审核记录无效。")
    for key in ("archive", "followup"):
        if key in task and (not isinstance(task[key], dict) or not task[key]):
            raise PlanError("03 归档或后续任务记录无效。")
    followup = task.get("followup")
    if followup and (any(not isinstance(followup.get(key), str)
                        or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", followup[key])
                        for key in ("source_task_id", "origin_task_id"))
                     or not isinstance(followup.get("source_task_file"), str) or not followup["source_task_file"]
                     or not re.fullmatch(r"[0-9a-f]{64}", str(followup.get("source_task_sha256", "")))):
        raise PlanError("后续任务缺少有效的来源归档记录。")
    for override in task.get("purchase_overrides", {}).values():
        _validate_purchase_override(override)
    source = task.get("source")
    if source is not None and (not isinstance(source, dict) or not isinstance(source.get("path"), str)
                               or not re.fullmatch(r"[0-9a-f]{64}", str(source.get("sha256", "")))):
        raise PlanError("03 任务缺少有效的审核表快照。")
    validated = make_task(task["id"], {"songs": task["songs"]}, task["actual"], task)
    validated.update({key: task[key] for key in ("source", "publication", "state") if key in task})
    for sid, values in validated["constants"].items():
        for value in values.values():
            parse_constant(value)
    if (set(validated["options"]) != set(OPTIONS)
            or any(type(v) is not bool for v in validated["options"].values())):
        raise PlanError("03 操作选项无效。")
    archived = archive_info(validated, path)
    if archived:
        validated["archive"] = deepcopy(archived)
    return validated


def create_followup(source_path, release_root):
    """Create a separate metadata task; never mutate the source archive."""
    source_path = Path(source_path)
    if linked(source_path):
        raise PlanError("03 任务文件不能是链接。")
    source_path = source_path.resolve()
    with task_lock(source_path):
        original = load_task(source_path)
        if not archive_info(original, source_path):
            raise PlanError("请先完成并归档当前任务，再创建后续任务。")
        if not original.get("source") or not original.get("publication"):
            raise PlanError("归档缺少原上传确认，无法建立后续任务。")
        identifier = "followup-" + original["id"][:50] + "-" + uuid4().hex
        task = make_task(identifier, {"songs": original["songs"]}, original["actual"])
        for key in ("constants", "purchase_overrides", "source", "publication", "endpoint"):
            if key in original:
                task[key] = deepcopy(original[key])
        target = original.get("endpoint") or original["archive"].get("api_base_url")
        if target:
            task["endpoint"] = target
        task["options"] = {**original["options"], "refresh_resources": False}
        task["progress"] = {key: deepcopy(value) for key, value in original["progress"].items()
                            if key.startswith(("constants:", "purchase:")) or key == "refresh:" + RATING_OPERATION}
        task["followup"] = {"source_task_id": original["id"], "source_task_file": str(source_path),
                            "source_task_sha256": digest(source_path),
                            "origin_task_id": original.get("followup", {}).get("origin_task_id", original["id"])}
        path = task_path(release_root, identifier)
        save_task(path, task)
    return path, task


def archive_followup(path, task, *, locked=False):
    """Freeze a successful follow-up as its own immutable completion record."""
    path = Path(path)
    if not locked:
        with task_lock(path):
            return archive_followup(path, task, locked=True)
    _editable(task, path)
    if not task.get("followup") or not task_complete(task):
        raise PlanError("后续任务尚未完成，不能归档。")
    current = read_json(path)
    _editable(current, path)
    if current.get("id") != task["id"] or current.get("revision") != task["revision"]:
        raise PlanError("后续任务已在其他窗口改变，请重新载入。")
    archived = {**task, "archive": {"kind": "followup_complete", "completed_at": timestamp(),
                                   "api_base_url": task.get("endpoint", "")},
                "revision": task["revision"] + 1, "updated_at": timestamp()}
    write_json(path, archived)
    task.update(archived)
    return task


def read_reviewed(task, source):
    _editable(task)
    if task.get("followup"):
        raise PlanError("后续任务沿用原归档的曲目元数据，不能更换资源总表。")
    path = Path(source).resolve(strict=True)
    before = digest(path)
    updated = make_task(task["id"], read_json(path), task["actual"], task)
    if digest(path) != before:
        raise PlanError("读取审核 songlist 时文件发生变化，请重试。")
    updated["source"] = {"path": str(path), "sha256": before}
    if before != (task.get("source") or {}).get("sha256"):
        updated["publication"] = None
    return updated


def confirm_uploaded(task, receipt=None):
    _editable(task)
    if not task.get("source"):
        raise PlanError("请先载入已经审核并用于上传的最终 songlist。")
    if receipt is not None:
        if (receipt.get("batch_id") != task["id"] or receipt.get("local_verified") is not True
                or receipt.get("remote_verified") is not True
                or receipt.get("songlist_sha256") != task["source"]["sha256"]):
            raise PlanError("02 上传凭据与 03 审核表不匹配。")
    task["publication"] = {"kind": "verified" if receipt else "manual_confirmed",
                           "songlist_sha256": task["source"]["sha256"], "confirmed_at": timestamp(),
                           "confirmation_id": uuid4().hex}


def missing_constants(task):
    if not task["options"]["constants"]:
        return []
    return [f"{sid}/{c}" for sid, classes in task["actual"].items() for c in classes
            if parse_constant(task["constants"].get(sid, {}).get(str(c))) is None]


def _purchase_spec(row):
    name, sid = row.get("purchase", ""), row["id"]
    if name in ("", None):
        return None
    if not portable_id(name):
        raise PlanError(f"{sid}: purchase ID 不合法。")
    if name == sid and name == row.get("set"):
        raise PlanError(f"{sid}: purchase 同时匹配 id 和 set，无法自动决定单曲还是曲包。")
    if name == sid:
        kind, price = "single", 100
    elif name == row.get("set"):
        kind, price = "pack", 500
    else:
        raise PlanError(f"{sid}: purchase 与曲目 ID、set 均不相同，无法自动推断；可编辑本批安排或确认跳过。")
    return {"kind": kind, "name": name, "price": price, "orig_price": price,
            "discount_from": PAST_FROM, "discount_to": PAST_TO, "discount_reason": ""}


def _validate_purchase_override(value):
    if not isinstance(value, dict) or value.get("mode") not in {"skip", "custom"}:
        raise PlanError("购买项编辑模式无效。")
    if value["mode"] == "custom":
        if not portable_id(value.get("name")) or value.get("kind") not in {"single", "pack"}:
            raise PlanError("请填写有效的购买项 ID，并选择单曲或曲包。")
        price = value.get("price")
        if type(price) is not int or not 0 <= price <= 2147483647:
            raise PlanError("购买价格须为非负整数。")


def set_purchase_override(task, song_ids, mode, *, name="", kind="single", price=None):
    """Edit this task only; the reviewed songlist and remote data stay intact."""
    _editable(task)
    if mode not in {"auto", "skip", "custom"} or not song_ids:
        raise PlanError("请选择购买项编辑方式。")
    if any(sid not in task["actual"] for sid in song_ids):
        raise PlanError("购买项引用了本批以外的曲目。")
    value = {"mode": mode}
    if mode == "custom":
        if not isinstance(name, str):
            raise PlanError("购买项 ID 必须是文本。")
        if isinstance(price, str) and re.fullmatch(r"[0-9]+", price.strip()):
            price = int(price.strip())
        value.update(name=name.strip(), kind=kind, price=price)
    if mode != "auto":
        _validate_purchase_override(value)
    previous = deepcopy(task.get("purchase_overrides", {}))
    overrides = task.setdefault("purchase_overrides", {})
    for sid in song_ids:
        if mode == "auto":
            overrides.pop(sid, None)
        else:
            overrides[sid] = deepcopy(value)
    if overrides != previous:
        task["purchase_skip_confirmation"] = None
    update_purchase_candidates(task)


def analyze_purchases(task):
    """A joined catalog view of purchase, item and purchase_item, without I/O."""
    groups, free, invalid, skipped, errors, conflicts = {}, [], [], [], [], set()
    for row in task["songs"]:
        song = {"id": row["id"], "title": english_name(row), "set": row.get("set"),
                "purchase": row.get("purchase")}
        try:
            override = task.get("purchase_overrides", {}).get(row["id"])
            if override:
                _validate_purchase_override(override)
                if override["mode"] == "skip":
                    skipped.append({**song, "reason": "人工选择跳过购买项"})
                    continue
                spec = {key: override[key] for key in ("name", "kind", "price")}
                spec.update(orig_price=spec["price"], discount_from=PAST_FROM, discount_to=PAST_TO, discount_reason="")
            else:
                spec = _purchase_spec(row)
        except PlanError as error:
            invalid.append({**song, "error": str(error)})
            errors.append(str(error))
            continue
        if spec is None:
            free.append(song)
            continue
        name = spec["name"]
        if name not in groups:
            groups[name] = {"spec": spec, "songs": []}
        elif groups[name]["spec"] != spec:
            conflicts.add(name)
        groups[name]["songs"].append(song)
    purchases = []
    for name, group in groups.items():
        if name in conflicts:
            error = f"购买项 {name} 在本批同时被解释为不同类别。"
            errors.append(error)
            invalid.extend({**song, "error": error} for song in group["songs"])
            continue
        spec = group["spec"]
        purchases.append({"spec": spec, "songs": group["songs"],
            "item": {"item_id": name, "type": spec["kind"], "is_available": True},
            "purchase_item": {"purchase_name": name, "item_id": name, "type": spec["kind"], "amount": 1},
            "discount_active": False, "vouchers_allowed": False})
    return {"purchases": purchases, "free_songs": free, "invalid_songs": invalid,
            "skipped_songs": skipped, "errors": errors}


def purchase_skips(analysis):
    return [*analysis["skipped_songs"], *({**song, "reason": song["error"]} for song in analysis["invalid_songs"])]


def purchase_skip_signature(task):
    return fingerprint({"songs": task["songs"], "source": task.get("source"),
                        "overrides": task.get("purchase_overrides", {}),
                        "skips": purchase_skips(analyze_purchases(task))})


def confirm_purchase_skips(task):
    _editable(task)
    task["purchase_skip_confirmation"] = purchase_skip_signature(task)


def purchase_specs(task):
    analysis = analyze_purchases(task)
    if analysis["errors"]:
        raise PlanError("\n".join(analysis["errors"]))
    return [entry["spec"] for entry in analysis["purchases"]]


def update_purchase_candidates(task):
    analysis = analyze_purchases(task)
    task["purchase_candidates"] = [entry["spec"] for entry in analysis["purchases"]]
    task["purchase_errors"] = analysis["errors"]
    task["purchase_preview"] = analysis
    task["purchase_warnings"] = [song["reason"] for song in purchase_skips(analysis)]


def _chart_map(data):
    values = {i: -1 for i in range(5)}
    if data is not None and not isinstance(data, dict):
        raise PlanError("远端歌曲定数结构无效。")
    charts = (data or {}).get("charts", [])
    if not isinstance(charts, list) or not all(isinstance(chart, dict) for chart in charts):
        raise PlanError("远端歌曲定数结构无效。")
    seen = set()
    for chart in charts:
        difficulty, value = chart.get("difficulty"), chart.get("chart_const")
        if (type(difficulty) is not int or difficulty not in values or type(value) not in {int, float}
                or not math.isfinite(value) or difficulty in seen):
            raise PlanError("远端歌曲定数结构无效。")
        seen.add(difficulty)
        values[difficulty] = value
    return values


def _song_matches(data, desired, *, complete=False):
    if (not isinstance(data, dict) or data.get("name") != desired["name"]
            or data.get("song_id", desired["song_id"]) != desired["song_id"]):
        return False
    if complete and (data.get("song_id") != desired["song_id"]
                     or not isinstance(data.get("charts"), list) or len(data["charts"]) != 5):
        return False
    try:
        return _chart_map(data) == _chart_map(desired)
    except PlanError:
        return False


def _available(item):
    value = item.get("is_available") if item else None
    return type(value) in {bool, int} and value == 1


def run_task(task, api, *, persist=lambda task: None, on_log=lambda level, message: None,
             allow_missing=False):
    """Never automatically replay writes; resume reconciles current remote data."""
    _editable(task)
    if task.get("input_errors"):
        raise PlanError("原批次仍有 ERROR，不能只执行成功部分的 03 操作。")
    publication = task.get("publication") or {}
    if (not task.get("source") or publication.get("songlist_sha256") != task["source"].get("sha256")
            or publication.get("kind") not in {"verified", "manual_confirmed"}):
        raise PlanError("尚未确认本批最终 songlist 与已上传资源一致。")
    source = Path(task["source"]["path"])
    if not task.get("followup") and source.exists() and digest(source) != task["source"]["sha256"]:
        raise PlanError("确认后审核 songlist 已改变，请重新载入并确认上传状态。")
    if task.get("endpoint") and task["endpoint"] != api.base_url:
        raise PlanError("该 03 任务属于另一 API 地址，请为新服务器建立任务。")
    if not any(task["options"].values()):
        finish_task(task, skipped=True)
        persist(task)
        return task
    missing = missing_constants(task)
    if missing and not allow_missing:
        raise PlanError("真实谱面仍有未填定数，须先在界面确认后继续。")
    analysis = analyze_purchases(task) if task["options"]["purchases"] else None
    skipped = purchase_skips(analysis) if analysis else []
    if skipped and task.get("purchase_skip_confirmation") != purchase_skip_signature(task):
        raise PlanError("本批有待跳过的购买项，请先在执行 03 前的弹窗中确认，或编辑购买项安排。")
    specs = [entry["spec"] for entry in analysis["purchases"]] if analysis else []
    if task["options"]["constants"]:
        for row in task["songs"]:
            if not isinstance(english_name(row), str) or not english_name(row).strip():
                raise PlanError(f"{row['id']}: 缺少 title_localized.en 或 title，无法确定英文名。")
    task["state"] = "preflight"
    task.pop("completion_signature", None)
    persist(task)
    required = list(RESOURCE_OPERATIONS) if task["options"]["refresh_resources"] else []
    if task["options"]["refresh_rating"]:
        required.append(RATING_OPERATION)
    operations = api.request("GET", "/system/operations")
    if not isinstance(operations, list) or any(name not in operations for name in required):
        raise PlanError("服务器未提供本次需要的系统操作。")
    # Read all affected records before the first mutation; authentication or
    # structural errors must not produce a half-started business update.
    for spec in specs:
        on_log("info", f"正在核验购买项 {spec['name']}。")
        api.get_optional("/purchases/" + spec["name"])
        item = api.get_optional(f"/items/{spec['kind']}/{spec['name']}")
        if item and not _available(item):
            existing = api.get_optional("/purchases/" + spec["name"])
            if existing is None:
                raise PlanError(f"{spec['name']}: 已有物品不可用，需人工确认。")
    constant_inputs = {row["id"]: [row, task["actual"][row["id"]], task["constants"].get(row["id"], {})]
                       for row in task["songs"]}
    song_records = {}
    if task["options"]["constants"]:
        for row in task["songs"]:
            old = task["progress"].get("constants:" + row["id"], {})
            if old.get("state") == "done" and old.get("sha256") == fingerprint(constant_inputs[row["id"]]):
                continue
            on_log("info", f"正在核验歌曲定数 {row['id']}。")
            song_records[row["id"]] = api.get_optional("/songs/" + row["id"])
            _chart_map(song_records[row["id"]])
    task["endpoint"], task["state"] = api.base_url, "running"
    persist(task)

    def step(key, inputs, action):
        signature = fingerprint(inputs)
        old = task["progress"].get(key, {})
        if old.get("state") == "done" and old.get("sha256") == signature:
            on_log("info", f"{key} 已完成，跳过重复执行。")
            return
        if key.startswith("refresh:"):
            if old.get("state") in {"running", "uncertain"}:
                on_log("warning", f"{key} 上次结果未确认；本次将重新执行此刷新，不会直接标记成功。")
            on_log("info", f"正在执行 {key}；大型资源刷新可能需要数分钟，等待服务器返回。")
        task["progress"][key] = {"state": "running", "sha256": signature}
        persist(task)
        try:
            action()
        except Exception as error:
            state = "uncertain" if isinstance(error, ApiError) and error.uncertain else "failed"
            task["progress"][key]["state"] = state
            task["state"] = state
            persist(task)
            result = "结果未确认" if state == "uncertain" else "未完成"
            on_log("error", f"{key} {result}，已保存进度：{error}")
            raise
        task["progress"][key]["state"] = "done"
        persist(task)
        on_log("info", f"{key} 完成。")

    if missing:
        on_log("warning", "未填定数：" + ", ".join(missing) + "；保留已有值，新增记录暂用 -1。")
    for song in skipped:
        on_log("warning", f"已确认跳过 {song['id']} 的购买项：{song['reason']}；继续本批其他操作。")
    if task["options"]["refresh_resources"]:
        for operation in RESOURCE_OPERATIONS:
            step("refresh:" + operation, [task["source"]["sha256"], publication.get("confirmation_id")],
                 lambda operation=operation: api.request("POST", "/system/operations/" + operation, {}))

    def purchase(spec):
        name, kind = spec["name"], spec["kind"]
        existing = api.get_optional("/purchases/" + name)
        managed = task["managed_purchases"].get(name)
        if existing is not None and not managed:
            on_log("info", f"购买项 {name} 已存在，保留其价格与物品配置。")
            return
        policy = {key: value for key, value in spec.items() if key not in {"kind", "name"}}
        signature = fingerprint(spec)
        if managed and managed != signature:
            raise PlanError(f"{name}: 重试期间购买类别或策略改变，需人工检查。")
        item_path = f"/items/{kind}/{name}"
        item = api.get_optional(item_path)
        if item is None:
            try:
                api.request("POST", "/items", {"item_id": name, "type": kind, "is_available": True})
            except ApiError as error:
                if error.code != -122:
                    raise
            item = api.get_optional(item_path)
        if not _available(item):
            raise PlanError(f"物品 {kind}/{name} 未正确创建或不可用。")
        if existing is None:
            task["managed_purchases"][name] = signature  # Persist intent before POST.
            persist(task)
            api.request("POST", "/purchases", {"purchase_name": name, **policy})
            existing = api.get_optional("/purchases/" + name)
        if not existing or any(existing.get(key) != value for key, value in policy.items()):
            raise PlanError(f"购买项 {name} 的创建结果与预期不符，不覆盖远端数据。")
        items_path = "/purchases/" + name + "/items"
        items = api.request("GET", items_path)
        expected = {"id": name, "type": kind, "amount": 1}
        matching = [i for i in items if all(i.get(k) == v for k, v in expected.items())]
        if not matching:
            if items:
                raise PlanError(f"购买项 {name} 已关联其他物品，需人工检查。")
            api.request("PATCH", items_path, {"create": [expected]})
        items = api.request("GET", items_path)
        if not any(all(i.get(k) == v for k, v in expected.items()) for i in items):
            raise PlanError(f"购买项 {name} 的物品关联未核验成功。")

    for spec in specs:
        step("purchase:" + spec["name"], spec, lambda spec=spec: purchase(spec))

    def constants(row):
        sid = row["id"]
        existing = song_records[sid]
        values = _chart_map(existing)
        for difficulty in range(5):
            entered = parse_constant(task["constants"].get(sid, {}).get(str(difficulty)))
            if difficulty not in task["actual"][sid]:
                values[difficulty] = -1
            elif entered is not None:
                values[difficulty] = entered
        desired = {"song_id": sid, "name": english_name(row),
                   "charts": [{"difficulty": i, "chart_const": values[i]} for i in range(5)]}
        if existing is None:
            verified = api.request("POST", "/songs", desired)
        else:
            patch = {}
            if existing.get("name") != desired["name"]:
                patch["name"] = desired["name"]
            prior = _chart_map(existing)
            charts = [c for c in desired["charts"] if c["chart_const"] != prior[c["difficulty"]]]
            if charts:
                patch["charts"] = charts
            if patch:
                verified = api.request("PUT", "/songs/" + sid, patch)
            else:
                if not _song_matches(existing, desired):
                    raise PlanError(f"{sid}: 远端曲目记录与请求 ID 不一致。")
                return  # The preflight read already matches; no write to verify.
        if not _song_matches(verified, desired, complete=True):
            verified = api.get_optional("/songs/" + sid)
        if not _song_matches(verified, desired):
            if any(c["chart_const"] == 0 for c in desired["charts"]):
                legacy_zero_view = {**desired, "charts": [
                    {**c, "chart_const": -1 if c["chart_const"] == 0 else c["chart_const"]}
                    for c in desired["charts"]]}
                if _song_matches(verified, legacy_zero_view):
                    raise PlanError(f"{sid}: API 将请求的 0.0 回显为 -1，无法精确核验；"
                                    "请修复服务端 core/song.py 的零值序列化后重试，或明确改填 -1。")
            raise PlanError(f"{sid}: 曲目定数写入后核验不一致。")

    if task["options"]["constants"]:
        for row in task["songs"]:
            sid = row["id"]
            step("constants:" + sid, constant_inputs[sid], lambda row=row: constants(row))
    if task["options"]["refresh_rating"]:
        step("refresh:" + RATING_OPERATION, [task["songs"], task["actual"], task["constants"]],
             lambda: api.request("POST", "/system/operations/" + RATING_OPERATION, {}))
    finish_task(task)
    persist(task)
    return task
