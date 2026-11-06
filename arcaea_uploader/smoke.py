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
        import ssl
        from PIL import Image, __version__ as pillow_version
        import paramiko
        from .backgrounds import complete_inverse
        from .bundle_versions import resolve_bundle_versions
        from .bundler import build_configured_bundle
        from .gui import UploaderApp
        from .settings import default_settings, save_settings

        root = tk.Tk()
        root.withdraw()
        report["checks"]["tk"] = root.tk.call("info", "patchlevel")
        report["checks"]["pillow"] = pillow_version
        report["checks"]["ssh_client"] = paramiko.__version__
        # Exercise a packaged cryptographic backend, without contacting a host.
        report["checks"]["ssh_crypto"] = paramiko.RSAKey.generate(1024).get_bits() == 1024
        report["checks"]["tls_context"] = ssl.create_default_context().verify_mode == ssl.CERT_REQUIRED
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
            config_path = Path(temporary) / "config.json"
            settings = default_settings()
            settings["local"]["release_root"] = str(Path(temporary) / "releases")
            save_settings(config_path, settings)
            application = UploaderApp(root, config_path)
            root.update_idletasks()
            if len(application.stage_buttons) != 3 or not application.follow_app_version_var.get():
                raise ValueError("Desktop initialization failed")
            report["checks"]["desktop"] = True
            if any(str(button["state"]) != "normal" or not button["command"] for button in application.stage_buttons[:2]):
                raise ValueError("Stage 01/02 buttons are not connected")
            report["checks"]["stage01_stage02"] = True
            application.stage_buttons[2].invoke()
            if application.results.select() != str(application.server_ops.frame):
                raise ValueError("Stage 03 editor is not available")
            report["checks"]["stage03_editor"] = True
            if not application.server_ops.commit_button["command"]:
                raise ValueError("Final workspace commit is not available")
            report["checks"]["stage03_commit"] = True
            application.show_help(("api", "token_env"))
            if "ARCAEA_UPLOADER_API_TOKEN" not in application.help_text.get("1.0", "end"):
                raise ValueError("Field help was not packaged")
            report["checks"]["field_help"] = True
            application.manage_workspaces()
            if application.workspace_manager.tree.get_children():
                raise ValueError("Smoke test workspace was not isolated")
            report["checks"]["workspace_manager"] = True
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
