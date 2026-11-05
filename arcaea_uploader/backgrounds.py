"""Background routing, inverse pairs and reference diagnostics."""

from pathlib import Path

from PIL import Image


def _pair_entries(pairs):
    if not isinstance(pairs, dict):
        raise ValueError("背景对应表必须是 JSON 对象。")
    for left, right in pairs.items():
        if not all(isinstance(v, str) and v and v == v.strip() and not any(c in v for c in '/\\\x00') for v in (left, right)):
            raise ValueError("背景对应表的键和值必须是无路径分隔符的背景 ID。")
    return pairs.items()


def _resolve_entries(entries):
    """Keep isolated undirected pairs; report every ID in ambiguous components."""
    graph, edges, seen = {}, [], set()
    for left, right in entries:
        graph.setdefault(left, set()).add(right)
        graph.setdefault(right, set()).add(left)
        edge = frozenset((left, right))
        if edge not in seen:
            edges.append((left, right))
            seen.add(edge)
    blocked = {bg for bg, neighbors in graph.items() if len(neighbors) > 1}
    pending = list(blocked)
    while pending:
        for neighbor in graph[pending.pop()]:
            if neighbor not in blocked:
                blocked.add(neighbor)
                pending.append(neighbor)
    pairs = {left: right for left, right in edges if left not in blocked}
    conflicts = {bg: sorted(graph[bg]) for bg in sorted(blocked)}
    return pairs, conflicts


def split_pairs(pairs):
    """Migrate old directed dictionaries without choosing conflicting partners."""
    return _resolve_entries(_pair_entries(pairs))


def normalize_pairs(pairs):
    pairs, conflicts = split_pairs(pairs)
    if conflicts:
        hubs = {bg: values for bg, values in conflicts.items() if len(values) > 1}
        raise ValueError(f"无向背景对应必须一一配对，同一背景不能对应多个背景：{hubs}")
    return pairs


def pair_lookup(pairs):
    result = {}
    for left, right in normalize_pairs(pairs).items():
        result[left], result[right] = right, left
    return result


def merge_pairs(learned, overrides):
    """A confirmed override replaces learned pairs at either endpoint."""
    overrides = normalize_pairs(overrides)
    endpoints = pair_lookup(overrides)
    retained = {a: b for a, b in normalize_pairs(learned).items() if a not in endpoints and b not in endpoints}
    return normalize_pairs({**retained, **overrides})


def learn_pairs(master):
    """Learn each explicit relationship once, for lookup in both directions."""
    entries = []
    for song in master.get("songs", []):
        for scope in (song, *song.get("difficulties", [])):
            bg, inverse = scope.get("bg", song.get("bg")), scope.get("bg_inverse")
            if isinstance(bg, str) and bg and isinstance(inverse, str) and inverse:
                _pair_entries({bg: inverse})
                entries.append((bg, inverse))
    return _resolve_entries(entries)


class BackgroundIndex:
    def __init__(self, assets_root=None):
        self.existing_names = set()
        self.existing_ids = set()
        self.planned = {}
        if assets_root:
            for relative in ("img/bg", "img/bg/1080"):
                folder = Path(assets_root) / relative
                if folder.is_dir():
                    for path in folder.iterdir():
                        if path.is_file():
                            self.existing_names.add(path.name.casefold())
                            self.existing_ids.add(path.stem.casefold())

    def route(self, path, log):
        """Check both existing directories before decoding size."""
        path = Path(path)
        if path.name.casefold() in self.existing_names:
            log("info", "background.skip_existing", f"背景 {path.name} 已存在于 img/bg 或 img/bg/1080，跳过导入。")
            return None
        with Image.open(path) as image:
            size = image.size
        if size == (1920, 1440):
            target = "img/bg/1080/" + path.name
        elif size in {(1440, 1080), (1280, 960)}:
            target = "img/bg/" + path.name
        else:
            raise ValueError(f"背景 {path.name} 出现新尺寸 {size[0]}×{size[1]}，需人工决定分流。")
        log("info", "background.route", f"背景 {path.name} 按 {size[0]}×{size[1]} 分流至 {target}。")
        return target


def references(value, prefix=""):
    result = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else key
            if key in {"bg", "bg_inverse"} and isinstance(item, str) and item:
                result.append((path, item))
            elif key == "bg_daynight" and isinstance(item, dict):
                result.extend((f"{path}.{k}", v) for k, v in item.items() if isinstance(v, str) and v)
            result.extend(references(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            result.extend(references(item, f"{prefix}[{index}]"))
    return result


def complete_inverse(row, pairs, log, ambiguous_ids=()):
    pairs = pair_lookup(pairs)
    scopes = [row, *row.get("difficulties", [])]
    for scope in scopes:
        bg = scope.get("bg")
        if not isinstance(bg, str) or not bg or scope.get("bg_inverse"):
            continue
        inverse = pairs.get(bg)
        via = "对应表"
        if inverse is None and bg in ambiguous_ids:
            log("warning", "background.inverse_ambiguous", f"总表中背景 {bg} 有多个不同翻转对应，需人工指定 bg_inverse。")
            continue
        if inverse is None:
            via = "后缀规则"
            if bg.endswith("_light"):
                inverse = bg[:-6] + "_conflict"
            elif bg.endswith("_conflict"):
                inverse = bg[:-9] + "_light"
        if inverse:
            scope["bg_inverse"] = inverse
            log("info", "background.inverse", f"根据{via}补充 bg_inverse：{bg} → {inverse}。")
        else:
            log("warning", "background.inverse_unknown", f"背景 {bg} 无法确定 bg_inverse，请在对应表或 songlist 中人工指定。")


def check_references(row, available_ids, log):
    seen = set()
    for field, bg in references(row):
        if bg.casefold() not in available_ids and bg.casefold() not in seen:
            log("warning", "background.missing", f"{field} 引用背景 {bg}，本批新背景及两个已有 bg 目录中均未找到，请人工确认。")
            seen.add(bg.casefold())
