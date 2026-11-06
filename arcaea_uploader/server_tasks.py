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
    return fingerprint({key: task.get(key) for key in
        ("songs", "actual", "constants", "options", "source", "publication", "endpoint")})


def finish_task(task, *, skipped=False):
    if skipped and any(task["options"].values()):
        raise PlanError("仍有启用的 03 操作，不能标记全部跳过。")
    task["state"] = "skipped" if skipped else "complete"
    task["completion_signature"] = completion_signature(task)


def task_complete(task):
    return (task.get("state") in {"complete", "skipped"}
            and task.get("completion_signature") == completion_signature(task)
            and (task["state"] != "skipped" or not any(task["options"].values())))


def english_name(row):
    localized = row.get("title_localized")
    value = localized.get("en") if isinstance(localized, dict) else None
    return (value if isinstance(value, str) and value.strip() else row.get("title")) or ""


def parse_constant(value):
    if value is None or isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, bool) or not re.fullmatch(r"\d+(?:\.\d)?", str(value).strip()):
        raise PlanError("定数须为整数或一位小数，未知请留空。")
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise PlanError("定数格式无效。") from None
    if not number.is_finite() or number <= 0 or not math.isfinite(float(number)):
        raise PlanError("定数须为正数，未知请留空；无实际谱面的难度由程序写 -1。")
    return float(number)


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
            "state": "draft", "source": None, "publication": None, "revision": 0, "input_errors": []}
    if previous:
        if previous.get("id") != task_id:
            raise PlanError("03 任务标识不匹配。")
        for key in ("created_at", "constants", "options", "progress", "managed_purchases", "endpoint", "revision", "input_errors", "completion_signature"):
            if key in previous:
                task[key] = deepcopy(previous[key])
        if selected == previous["songs"] and task["actual"] == previous["actual"]:
            for key in ("source", "publication", "state"):
                task[key] = deepcopy(previous.get(key))
    # A removed difficulty must not leave a writable constant behind.
    task["constants"] = {sid: {str(c): task["constants"].get(sid, {}).get(str(c)) for c in classes}
                         for sid, classes in task["actual"].items()}
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
    return validated


def read_reviewed(task, source):
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


def purchase_specs(task):
    specs = {}
    for row in task["songs"]:
        name, sid = row.get("purchase", ""), row["id"]
        if name in ("", None):
            continue
        if not portable_id(name):
            raise PlanError(f"{sid}: purchase ID 不合法。")
        if name == sid and name == row.get("set"):
            raise PlanError(f"{sid}: purchase 同时匹配 id 和 set，无法自动决定单曲还是曲包。")
        if name == sid:
            kind, price = "single", 100
        elif name == row.get("set"):
            kind, price = "pack", 500
        else:
            raise PlanError(f"{sid}: purchase 必须等于曲目 ID 或 set。")
        spec = {"kind": kind, "name": name, "price": price, "orig_price": price,
                "discount_from": PAST_FROM, "discount_to": PAST_TO, "discount_reason": ""}
        if name in specs and specs[name] != spec:
            raise PlanError(f"购买项 {name} 在本批同时被解释为不同类别。")
        specs[name] = spec
    return list(specs.values())


def update_purchase_candidates(task):
    try:
        task["purchase_candidates"] = purchase_specs(task)
        task["purchase_errors"] = []
    except PlanError as error:
        task["purchase_candidates"], task["purchase_errors"] = [], [str(error)]


def _chart_map(data):
    values = {i: -1 for i in range(5)}
    for chart in (data or {}).get("charts", []):
        difficulty, value = chart.get("difficulty"), chart.get("chart_const")
        if (type(difficulty) is not int or difficulty not in values or type(value) not in {int, float}
                or not math.isfinite(value)):
            raise PlanError("远端歌曲定数结构无效。")
        values[difficulty] = value
    return values


def _available(item):
    value = item.get("is_available") if item else None
    return type(value) in {bool, int} and value == 1


def run_task(task, api, *, persist=lambda task: None, on_log=lambda level, message: None,
             allow_missing=False):
    """No automatic HTTP retries. Resume reconciles resource state before writes."""
    if task.get("input_errors"):
        raise PlanError("原批次仍有 ERROR，不能只执行成功部分的 03 操作。")
    publication = task.get("publication") or {}
    if (not task.get("source") or publication.get("songlist_sha256") != task["source"].get("sha256")
            or publication.get("kind") not in {"verified", "manual_confirmed"}):
        raise PlanError("尚未确认本批最终 songlist 与已上传资源一致。")
    source = Path(task["source"]["path"])
    if source.exists() and digest(source) != task["source"]["sha256"]:
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
    specs = purchase_specs(task) if task["options"]["purchases"] else []
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
    if task["options"]["constants"]:
        for row in task["songs"]:
            on_log("info", f"正在核验歌曲定数 {row['id']}。")
            _chart_map(api.get_optional("/songs/" + row["id"]))
    task["endpoint"], task["state"] = api.base_url, "running"
    persist(task)

    def step(key, inputs, action):
        signature = fingerprint(inputs)
        old = task["progress"].get(key, {})
        if old.get("state") == "done" and old.get("sha256") == signature:
            on_log("info", f"{key} 已完成，跳过重复执行。")
            return
        task["progress"][key] = {"state": "running", "sha256": signature}
        persist(task)
        try:
            action()
        except Exception as error:
            state = "uncertain" if isinstance(error, ApiError) and error.uncertain else "failed"
            task["progress"][key]["state"] = state
            task["state"] = state
            persist(task)
            on_log("error", f"{key} 未完成，已保存进度：{error}")
            raise
        task["progress"][key]["state"] = "done"
        persist(task)
        on_log("info", f"{key} 完成。")

    if missing:
        on_log("warning", "未填定数：" + ", ".join(missing) + "；保留已有值，新增记录暂用 -1。")
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
        existing = api.get_optional("/songs/" + sid)
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
            api.request("POST", "/songs", desired)
        else:
            patch = {}
            if existing.get("name") != desired["name"]:
                patch["name"] = desired["name"]
            prior = _chart_map(existing)
            charts = [c for c in desired["charts"] if c["chart_const"] != prior[c["difficulty"]]]
            if charts:
                patch["charts"] = charts
            if patch:
                api.request("PUT", "/songs/" + sid, patch)
        verified = api.get_optional("/songs/" + sid)
        if not verified or verified.get("name") != desired["name"] or _chart_map(verified) != values:
            raise PlanError(f"{sid}: 曲目定数写入后核验不一致。")

    if task["options"]["constants"]:
        for row in task["songs"]:
            sid = row["id"]
            inputs = [row, task["actual"][sid], task["constants"].get(sid, {})]
            step("constants:" + sid, inputs, lambda row=row: constants(row))
    if task["options"]["refresh_rating"]:
        step("refresh:" + RATING_OPERATION, [task["songs"], task["actual"], task["constants"]],
             lambda: api.request("POST", "/system/operations/" + RATING_OPERATION, {}))
    finish_task(task)
    persist(task)
    return task
