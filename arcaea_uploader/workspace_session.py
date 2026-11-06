"""Remember workspace roots and the last active workspace for one GUI profile."""

from pathlib import Path

from .planner import read_json
from .workspaces import list_workspaces, write_json


def load_session(path):
    try:
        data = read_json(Path(path))
        roots = data.get("roots", [])
        if not isinstance(roots, list) or not all(isinstance(p, str) and Path(p).is_absolute() for p in roots):
            raise ValueError("Invalid workspace roots")
        return {"roots": roots, "active": data.get("active") if isinstance(data.get("active"), str) else None}
    except (OSError, ValueError, AttributeError):
        return {"roots": [], "active": None}


def remember(path, root, active=None):
    path = Path(path)
    data = load_session(path)
    absolute = str(Path(root).resolve())
    data["roots"] = list(dict.fromkeys([*data["roots"], absolute]))
    data["active"] = str(Path(active).resolve()) if active else None
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, data)
    return data


def entries(path, current_root=None):
    roots = load_session(path)["roots"]
    if current_root:
        roots = [*roots, str(Path(current_root).resolve())]
    found = {}
    for root in dict.fromkeys(roots):
        try:
            for row in list_workspaces(root):
                row["release_root"] = root
                found[row["path"]] = row
        except (OSError, ValueError):
            continue
    return list(found.values())
