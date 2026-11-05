"""Application adapter for the unmodified, MIT-licensed Arcaea-Bundler core.

Builds are intended for a release workspace: successful builds update its
metadata.oldjson. The GUI publish workflow is not connected yet.
"""

from __future__ import annotations

from base64 import b64decode
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from .bundle_versions import latest_record, read_history, resolve_bundle_versions


@dataclass(frozen=True)
class BundleResult:
    bundle: Path
    metadata: Path
    app_version: str
    version: str


def _version(value: str | None, name: str):
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value)):
        raise ValueError(f"Invalid {name}: {value!r}")


def validate_bundle(bundle: Path, metadata: Path) -> dict:
    """Verify offsets and SHA-256 against the actual generated CB bytes."""
    data = json.loads(metadata.read_text(encoding="utf-8"))
    for key in ("applicationVersionNumber", "versionNumber"):
        value = data.get(key)
        if value is None:
            raise ValueError(f"Bundle metadata is missing {key}")
        _version(value, key)
    size = bundle.stat().st_size
    offset = 0
    with bundle.open("rb") as stream:
        for entry in data["added"]:
            length = entry["length"]
            if (type(length) is not int or length < 0 or entry["byteOffset"] != offset
                    or offset + length > size):
                raise ValueError("Invalid bundle byte range")
            value = sha256()
            remaining = length
            while remaining:
                block = stream.read(min(remaining, 1024 * 1024))
                if not block:
                    raise ValueError("Unexpected end of bundle")
                value.update(block)
                remaining -= len(block)
            if value.digest() != b64decode(entry["sha256HashBase64Encoded"], validate=True):
                raise ValueError(f"Bundle hash mismatch: {entry['path']}")
            offset += length
    if offset != size:
        raise ValueError("Unindexed bytes in bundle")
    return data


def build_bundle(assets_root: Path, output_directory: Path, *, app_version: str | None = None,
                 bundle_version: str | None = None, previous_version: str | None = None) -> BundleResult:
    """Build a normal update or an application transition in the output directory.

    Resolve the version through the upstream core, check destination collisions
    before parsing advances history, then validate staged bytes before exposure.
    """
    assets_root = Path(assets_root).resolve(strict=True)
    output_directory = Path(output_directory).resolve()
    if output_directory.is_relative_to(assets_root):
        raise ValueError("Bundle output must be outside the input assets tree")
    for name in ("songlist", "packlist", "unlocks"):
        if not (assets_root / "songs" / name).is_file():
            raise FileNotFoundError(assets_root / "songs" / name)
    records = read_history(assets_root)
    latest = latest_record(records)
    application_changed = app_version is not None and (latest is None or app_version != latest["applicationVersionNumber"])
    if application_changed and bundle_version is None:
        bundle_version = app_version
    if not records and (app_version is None or bundle_version is None):
        raise ValueError("First build requires app_version and bundle_version")
    for name, value in (("app_version", app_version), ("bundle_version", bundle_version), ("previous_version", previous_version)):
        _version(value, name)
    output_directory.mkdir(parents=True, exist_ok=True)
    # Lazy import: upstream configures logging when imported. Window startup and
    # read-only preflight do not need to load it.
    from ._vendor.arcaea_bundler.main import Bundler
    with TemporaryDirectory(prefix=".bundle-", dir=output_directory) as staging:
        staged_bundle = Path(staging) / "bundle.cb"
        staged_metadata = Path(staging) / "bundle.json"
        core = Bundler(str(assets_root), str(staged_bundle), str(staged_metadata), "metadata")
        try:
            core.set_version(app_version, bundle_version, previous_version)
            resolved_version = core.bundle_version
            if resolved_version is None:
                raise ValueError("Bundle core did not resolve a versionNumber")
            _version(resolved_version, "versionNumber")
            prefix = core.app_version + ".0" if application_changed and resolved_version == core.app_version else resolved_version
            output = output_directory / f"{prefix}.cb"
            metadata = output_directory / f"{prefix}.json"
            for path in (output, metadata):
                if path.exists():
                    raise FileExistsError(path)
            core.parse()
        finally:
            # Close handles before validating or moving files on Windows.
            for name in ("file_handler", "metadata_handler"):
                handler = getattr(core, name, None)
                if handler is not None:
                    handler.close()
        data = validate_bundle(staged_bundle, staged_metadata)
        if app_version is not None and data["applicationVersionNumber"] != app_version:
            raise ValueError("Unexpected application version")
        if data["versionNumber"] != resolved_version:
            raise ValueError("Unexpected bundle version")
        if previous_version is not None and data["previousVersionNumber"] != previous_version:
            raise ValueError("Unexpected previous bundle version")
        # CB first, paired metadata last. Never replace an existing version.
        staged_bundle.rename(output)
        staged_metadata.rename(metadata)
    return BundleResult(output, metadata, data["applicationVersionNumber"], data["versionNumber"])


def build_configured_bundle(assets_root: Path, output_directory: Path, *, follow_app_version=True,
                            app_version="") -> BundleResult:
    """Apply the same checkbox policy and version preview used by the desktop."""
    plan = resolve_bundle_versions(read_history(assets_root), follow_app_version=follow_app_version,
                                   app_version=app_version)
    return build_bundle(assets_root, output_directory, app_version=plan.app_version,
                        bundle_version=plan.bundle_version, previous_version=plan.previous_version)
