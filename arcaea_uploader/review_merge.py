"""Remember reviewed fields, including intentional removals, across imports."""

from copy import deepcopy


def _changes(before, current, path=()):
    if isinstance(before, dict) and isinstance(current, dict):
        for key in before.keys() | current.keys():
            if key not in before or key not in current:
                yield (*path, key)
            else:
                yield from _changes(before[key], current[key], (*path, key))
    elif path == ("difficulties",) and isinstance(before, list) and isinstance(current, list):
        old = {d["ratingClass"]: d for d in before}
        new = {d["ratingClass"]: d for d in current}
        yield from _changes(old, new, path)
    elif before != current:
        yield path


def _restore(target, current, path):
    key, *tail = path
    if isinstance(current, list):
        reviewed = next((d for d in current if d["ratingClass"] == key), None)
        existing = next((d for d in target if d["ratingClass"] == key), None)
        if not tail:
            target[:] = [d for d in target if d["ratingClass"] != key]
            if reviewed is not None:
                target.append(deepcopy(reviewed))
            target.sort(key=lambda d: d["ratingClass"])
        elif reviewed is not None:
            if existing is None:
                existing = {"ratingClass": key}
                target.append(existing)
            _restore(existing, reviewed, tail)
    elif not tail:
        if key in current:
            target[key] = deepcopy(current[key])
        else:
            target.pop(key, None)
    elif key in current:
        if key not in target or type(target[key]) is not type(current[key]):
            target[key] = [] if isinstance(current[key], list) else {}
        _restore(target[key], current[key], tail)


def retain_review_edits(draft, reviewed, merged, previous, ids):
    """Update protected paths and restore their current reviewed values in-place."""
    old = {row["id"]: row for row in draft["songs"]}
    current = {row["id"]: row for row in reviewed["songs"]}
    output = {row["id"]: row for row in merged["songs"]}
    protected = deepcopy(previous)
    for sid in ids:
        paths = {tuple(path) for path in protected.get(sid, [])}
        paths.update(_changes(old[sid], current[sid]))
        # A protected parent wins over all of its children, including deletions.
        minimal = [path for path in paths if not any(path[:len(parent)] == parent for parent in paths if len(parent) < len(path))]
        for path in minimal:
            _restore(output[sid], current[sid], path)
        protected[sid] = [list(path) for path in sorted(minimal, key=repr)]
    return protected
