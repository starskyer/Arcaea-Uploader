"""Read one author entry, recovering only unambiguous complete JSON objects."""

from copy import deepcopy
import json
import re

from .import_rules import validate_rating_values


def order_song_fields(row):
    """Arrange generated metadata without adding fields or changing values."""
    anchors = {"bg": ("bg_inverse",), "date": ("version", "remote_dl")}
    adjacent = {field for anchor, fields in anchors.items() if anchor in row for field in fields}
    ordered = {"idx": row["idx"]} if "idx" in row else {}
    for key, value in row.items():
        if key == "idx" or key in adjacent:
            continue
        ordered[key] = value
        for field in anchors.get(key, ()):
            if field in row:
                ordered[field] = row[field]
    row.clear()
    row.update(ordered)
    for difficulty in row.get("difficulties", []):
        order_song_fields(difficulty)


def strip_trailing_commas(text):
    result = []
    quoted = escaped = False
    for index, char in enumerate(text):
        if quoted:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            result.append(char)
        elif char == "," and (not text[index + 1:].strip() or text[index + 1:].lstrip().startswith(("}", "]"))):
            continue
        else:
            result.append(char)
    return "".join(result)


def read_author_metadata(path):
    raw = path.read_bytes()
    notes = []
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
        notes.append("converted_metadata_encoding:utf-16")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeError:
            text = raw.decode("gb18030")
            notes.append("converted_metadata_encoding:gb18030")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"JSON 重复键 {key}，无法自动选择。")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f"非法 JSON 数值 {value}。")

    decoder = json.JSONDecoder(object_pairs_hook=unique, parse_constant=invalid_constant)
    try:
        parsed = decoder.decode(text)
    except json.JSONDecodeError:
        repaired = strip_trailing_commas(text)
        try:
            parsed = decoder.decode(repaired)
            notes.append("removed_trailing_commas")
        except json.JSONDecodeError:
            parsed, end = decoder.raw_decode(repaired.lstrip())
            tail = repaired.lstrip()[end:]
            if not isinstance(parsed, dict) or not re.fullmatch(r"[\s\]},]*", tail):
                raise ValueError("完整 JSON 对象之后有无法自动决策的额外内容。")
            notes.append("recovered_complete_object")
    if isinstance(parsed, dict):
        rows = parsed.get("songs", [parsed])
    else:
        rows = parsed
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("每个曲目包必须只描述一首曲目，不能自动选择多个条目。")
    row = deepcopy(rows[0])
    validate_rating_values(row)
    return row, notes
