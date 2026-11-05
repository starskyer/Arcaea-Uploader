"""Read-only version selection shared by the desktop preview and bundling."""

from dataclasses import dataclass
import json
from pathlib import Path
import re


def validate_version(value, name="应用版本"):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value):
        raise ValueError(f"{name}必须由数字和点组成，例如 7.0.257。")
    return value


def latest_record(records):
    if not isinstance(records, list):
        raise ValueError("metadata.oldjson 必须是数组。")
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("metadata.oldjson 含无效记录。")
        for key in ("applicationVersionNumber", "versionNumber"):
            validate_version(record.get(key), key)
    return max(records, key=lambda record: tuple(map(int, record["versionNumber"].split("."))), default=None)


def read_history(assets_root):
    path = Path(assets_root) / "metadata.oldjson"
    records = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else []
    latest_record(records)
    return records


@dataclass(frozen=True)
class BundleVersionPlan:
    app_version: str
    bundle_version: str
    previous_version: str | None
    filename_prefix: str
    transition: bool


def resolve_bundle_versions(records, *, follow_app_version=True, app_version=""):
    if type(follow_app_version) is not bool:
        raise ValueError("follow_app_version 必须是布尔值。")
    latest = latest_record(records)
    previous = latest["versionNumber"] if latest else None
    if follow_app_version:
        if latest is None:
            raise ValueError("没有应用版本历史；首次打包前请取消勾选并填写应用版本。")
        numbers = tuple(map(int, previous.split(".")))
        version = (".".join(map(str, numbers)) + ".1" if len(numbers) <= 3 else
                   ".".join(map(str, numbers[:-1])) + "." + str(numbers[-1] + 1))
        return BundleVersionPlan(latest["applicationVersionNumber"], version, previous, version, False)
    app_version = validate_version(app_version.strip() if isinstance(app_version, str) else app_version)
    if latest and app_version == latest["applicationVersionNumber"]:
        raise ValueError("应用版本未改变，请沿用当前应用版本，或填写新的应用版本。")
    return BundleVersionPlan(app_version, app_version, previous, app_version + ".0", True)
