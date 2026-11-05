"""Rules shared by preprocessing and the final songlist merge."""

import math
from pathlib import Path
import re

from .charts import is_empty_aff


_ID = re.compile(r"[A-Za-z0-9_-]+")
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def portable_id(value):
    return isinstance(value, str) and bool(_ID.fullmatch(value)) and value.casefold() not in _RESERVED


def require_version(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("请填写本次导入 version；不会使用原始 songlist 中的版本。")
    if any(ord(char) < 32 for char in value):
        raise ValueError("导入 version 不能包含控制字符。")
    return value.strip()


def numeric_rating(value):
    return type(value) in {int, float} and math.isfinite(value)


def validate_rating_values(row):
    difficulties = row.get("difficulties", [])
    if not isinstance(difficulties, list):
        raise ValueError("difficulties 必须是数组。")
    for difficulty in difficulties:
        if not isinstance(difficulty, dict):
            raise ValueError("难度条目必须是对象。")
        if "rating" in difficulty and not numeric_rating(difficulty["rating"]):
            raise ValueError(f"ratingClass {difficulty.get('ratingClass')} 的 rating 不是合法数字。")


def actual_chart_classes(*folders):
    result = set()
    for folder in folders:
        if folder is None:
            continue
        for rating in range(5):
            path = Path(folder) / f"{rating}.aff"
            if path.is_file() and not is_empty_aff(path.read_bytes()):
                result.add(rating)
    return result


def normalize_import_row(row, version, actual, log):
    """Only imported songs are affected; idx remains the merge's responsibility."""
    version = require_version(version)
    validate_rating_values(row)
    row["version"] = version
    row["remote_dl"] = True
    log("info", "metadata.version", f"曲目及所有声明难度的 version 统一使用 {version}。")
    log("info", "metadata.remote_dl", "已设置 remote_dl=true。")
    for difficulty in row.get("difficulties", []):
        rating = difficulty["ratingClass"]
        difficulty["version"] = version
        if rating not in actual:
            old = difficulty.get("rating")
            difficulty["rating"] = 0
            if old != 0:
                log("info", "metadata.placeholder", f"ratingClass {rating} 没有实际谱面，rating 从 {old!r} 统一为 0。")
        elif "rating" not in difficulty or difficulty["rating"] < 0:
            raise ValueError(f"实际谱面 ratingClass {rating} 缺少合法的非负 rating。")
