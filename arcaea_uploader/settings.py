"""GUI configuration persistence; independent from Tk and the planner."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile

from .planner import PlanError, read_json
from .backgrounds import split_pairs, learn_pairs, merge_pairs, new_pairs


PATH_KEYS = {
    "local": ("input_root", "assets_root",
              "server_project_root", "release_root", "placeholder_aff"),
    "ssh": ("private_key_path", "known_hosts_path"),
}


def user_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "Arcaea-Uploader"


def default_config_path() -> Path:
    # Source checkouts can keep machine-specific paths in this ignored file.
    local = Path(__file__).resolve().parent.parent / "config.local.json"
    return local if local.is_file() else user_data_dir() / "config.json"


def default_settings() -> dict:
    return {
        "import": {"version": "", "background_pairs": {}, "background_pairs_initialized": False,
                   "background_pair_conflicts": {}},
        "bundle": {"follow_app_version": True, "app_version": ""},
        "local": {
            "input_root": "", "assets_root": "",
            "server_project_root": "", "database_relative_path": "latest version/database",
            "release_root": str(user_data_dir() / "releases"), "placeholder_aff": "",
            "ffmpeg_path": "",
        },
        "ssh": {
            "host": "", "port": 22, "username": "", "server_project_root": "",
            "database_relative_path": "latest version/database",
            "private_key_path": "~/.ssh/id_ed25519", "known_hosts_path": "~/.ssh/known_hosts",
        },
        "api": {"base_url": "", "token_env": "ARCAEA_UPLOADER_API_TOKEN", "use_system_proxy": True},
    }


def normalize_settings(data: dict, origin: Path) -> dict:
    """Merge defaults, retain extension fields, anchor local paths on import."""
    if not isinstance(data, dict):
        raise PlanError("配置文件必须是 JSON 对象。")
    result = deepcopy(data)
    for section, defaults in default_settings().items():
        values = data.get(section, {})
        if not isinstance(values, dict):
            raise PlanError(f"配置项 {section} 必须是对象。")
        result[section] = {**defaults, **deepcopy(values)}
        if section == "local":
            # Old configuration files remain loadable; external tool overrides
            # are retired rather than silently retained and used later.
            for retired in ("bundler_entry", "bundler_root", "media_script"):
                result[section].pop(retired, None)
        for key, default in defaults.items():
            value = result[section][key]
            if section == "import" and key == "background_pairs":
                confirmed, conflicts = split_pairs(value)
                result[section][key] = confirmed
                reported = result[section]["background_pair_conflicts"]
                if not isinstance(reported, dict):
                    raise PlanError("背景冲突记录必须是对象。")
                result[section]["background_pair_conflicts"] = {**reported, **conflicts}
            elif section == "import" and key == "background_pair_conflicts":
                if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, list)
                    and all(isinstance(s, str) for s in v) for k, v in value.items()):
                    raise PlanError("背景冲突记录必须是 ID 到候选 ID 数组的对象。")
            elif isinstance(default, bool):
                if type(value) is not bool:
                    raise PlanError(f"配置项 {section}.{key} 必须是布尔值。")
            elif key == "port":
                if isinstance(value, bool):
                    raise PlanError("SSH 端口必须是 1–65535 的整数。")
                try:
                    port = int(value)
                except (TypeError, ValueError) as exc:
                    raise PlanError("SSH 端口必须是 1–65535 的整数。") from exc
                if not 1 <= port <= 65535 or str(value).strip() != str(port):
                    raise PlanError("SSH 端口必须是 1–65535 的整数。")
                result[section][key] = port
            elif not isinstance(value, str):
                raise PlanError(f"配置项 {section}.{key} 必须是文本。")
            else:
                result[section][key] = value.strip()
    for section, keys in PATH_KEYS.items():
        for key in keys:
            value = result[section][key]
            if value:
                path = Path(value).expanduser()
                result[section][key] = str((origin / path).resolve() if not path.is_absolute() else path.resolve())
    ffmpeg = result["local"]["ffmpeg_path"]
    if ffmpeg and (Path(ffmpeg).is_absolute() or "/" in ffmpeg or "\\" in ffmpeg):
        path = Path(ffmpeg).expanduser()
        result["local"]["ffmpeg_path"] = str((origin / path).resolve() if not path.is_absolute() else path.resolve())
    return result


def load_settings(path: Path) -> dict:
    return normalize_settings(read_json(path), path.resolve().parent)


def effective_settings(data: dict, origin: Path) -> dict:
    """Derive the default placeholder; Bundler and media code are built in."""
    result = normalize_settings(data, origin)
    local = result["local"]
    policy = result["import"]
    master_path = Path(local["assets_root"]) / "songs" / "songlist" if local["assets_root"] else None
    if not policy["background_pairs_initialized"] and master_path and master_path.is_file():
        learned, conflicts = learn_pairs(read_json(master_path))
        policy["background_pairs"] = merge_pairs(learned, policy["background_pairs"])
        policy["background_pair_conflicts"] = {**policy["background_pair_conflicts"], **conflicts}
        policy["background_pairs_initialized"] = True
    if not local["placeholder_aff"] and local["server_project_root"]:
        local["placeholder_aff"] = str(Path(local["server_project_root"])
                                       / local["database_relative_path"] / "songs" / "sayonarahatsukoi" / "0.aff")
    return result


def remember_background_pairs(policy, additions):
    """Add verified input pairs without replacing an existing decision."""
    accepted, conflicts = new_pairs(policy["background_pairs"], additions.items(),
                                    policy["background_pair_conflicts"])
    policy["background_pairs"].update(accepted)
    return accepted, conflicts


def persist_background_pairs(path, additions):
    """Only persist the table; retain all other values from the saved profile."""
    path = Path(path)
    saved = load_settings(path) if path.is_file() else default_settings()
    accepted, conflicts = remember_background_pairs(saved["import"], additions)
    if accepted:
        save_settings(path, saved)
    return accepted, conflicts


def save_settings(path: Path, data: dict) -> dict:
    """Replace one config atomically; incomplete paths may be saved for later."""
    normalized = normalize_settings(data, path.resolve().parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".config-", suffix=".tmp", delete=False) as stream:
            temp_path = Path(stream.name)
            json.dump(normalized, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        temp_path.replace(path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()
    return normalized
