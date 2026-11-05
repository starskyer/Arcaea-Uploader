"""Distribution smoke checks; no game files or network services are touched."""

from hashlib import sha256
from importlib.resources import files
from io import BytesIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from . import __version__


def run_smoke_test(output: Path) -> int:
    report = {"version": __version__, "frozen": bool(getattr(sys, "frozen", False)), "ok": False, "checks": {}}
    root = None
    try:
        import tkinter as tk
        from PIL import Image, __version__ as pillow_version
        from .backgrounds import complete_inverse
        from .bundle_versions import resolve_bundle_versions
        from .bundler import build_configured_bundle
        from .gui import UploaderApp

        root = tk.Tk()
        root.withdraw()
        report["checks"]["tk"] = root.tk.call("info", "patchlevel")
        report["checks"]["pillow"] = pillow_version
        data = BytesIO()
        Image.new("RGB", (2, 2)).save(data, format="PNG")
        data.seek(0)
        with Image.open(data) as image:
            image.verify()
        vendor = files("arcaea_uploader._vendor.arcaea_bundler")
        provenance = json.loads(vendor.joinpath("UPSTREAM.json").read_text(encoding="utf-8"))
        for name, expected in provenance["files"].items():
            if sha256(vendor.joinpath(name).read_bytes()).hexdigest() != expected:
                raise ValueError("Bundler provenance mismatch: " + name)
        report["checks"]["bundler_provenance"] = True
        row = {"bg": "night"}
        complete_inverse(row, {"day": "night"}, lambda *_: None)
        if row["bg_inverse"] != "day":
            raise ValueError("Background pair resolution failed")
        plan = resolve_bundle_versions([], follow_app_version=False, app_version="7.0.257")
        if plan.filename_prefix != "7.0.257.0":
            raise ValueError("Application transition preview failed")
        report["checks"]["application_transition"] = True
        report["checks"]["bundler_adapter"] = callable(build_configured_bundle)
        with TemporaryDirectory(prefix="arcaea-smoke-") as temporary:
            application = UploaderApp(root, Path(temporary) / "config.json")
            root.update_idletasks()
            if len(application.stage_buttons) != 2 or not application.follow_app_version_var.get():
                raise ValueError("Desktop initialization failed")
            report["checks"]["desktop"] = True
            application.close()
            root = None
        report["ok"] = True
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        if root is not None:
            root.destroy()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1
