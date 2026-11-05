"""Serializable compiler-style diagnostics used by both desktop and CLI."""

from collections import Counter


def emit(logs, level, code, message, *, song_id=None, source=None, callback=None):
    if level not in {"info", "warning", "error"}:
        raise ValueError(level)
    entry = {"level": level, "code": code, "message": message, "song_id": song_id,
             "source": str(source) if source else None}
    logs.append(entry)
    if callback:
        callback(entry.copy())
    return entry


def summarize(report):
    logs = report.setdefault("diagnostics", [])
    counts = Counter(d["level"] for d in logs)
    report["diagnostic_counts"] = {level: counts[level] for level in ("info", "warning", "error")}
    for level, key in (("error", "errors"), ("warning", "warnings"), ("info", "infos")):
        report[key] = [d["message"] for d in logs if d["level"] == level]
    report["requires_review"] = bool(counts["warning"])


def legacy_diagnostics(report):
    """Backfill diagnostics for planner errors that originate in older checks."""
    logs = report.setdefault("diagnostics", [])
    messages = {d["message"] for d in logs}
    for level, key in (("error", "errors"), ("warning", "warnings"), ("info", "infos")):
        for message in report.get(key, []):
            if message not in messages:
                emit(logs, level, "plan." + level, message)
                messages.add(message)
    summarize(report)
