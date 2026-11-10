"""Build, smoke-test and zip a Windows portable application."""

import argparse
from importlib import metadata
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import urllib.request
import zipfile
from hashlib import sha256


ROOT = Path(__file__).resolve().parents[1]


def collect_notices(application):
    target = application / "licenses"
    target.mkdir()
    notices = []

    def copy(source, name, version, filename):
        destination = target / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        notices.append({"name": name, "version": version, "license_file": "licenses/" + filename})

    copy(Path(sys.base_prefix) / "LICENSE.txt", "Python", platform.python_version(), "Python.txt")
    for package in ("Pillow", "PyInstaller", "paramiko", "bcrypt", "cryptography", "cffi", "pycparser", "pynacl", "invoke", "typing_extensions"):
        try:
            distribution = metadata.distribution(package)
        except metadata.PackageNotFoundError:
            # Recent Python/dependency combinations no longer need this
            # compatibility package. Only collect it when it is installed.
            if package == "typing_extensions":
                continue
            raise
        matches = [f for f in distribution.files or []
                   if f.name.lower().startswith(("license", "copying", "notice"))]
        if not matches:
            raise RuntimeError(package + " license files were not found")
        for index, item in enumerate(matches):
            copy(distribution.locate_file(item), package, distribution.version,
                 f"{package}/{index}-{item.name}")

    # Distribute the exact unmodified LGPL component's corresponding source.
    # Paramiko is collected as replaceable .py files, outside the PYZ archive.
    version = metadata.version("paramiko")
    with urllib.request.urlopen(f"https://pypi.org/pypi/paramiko/{version}/json", timeout=45) as response:
        release = json.load(response)
    source = next(item for item in release["urls"] if item["packagetype"] == "sdist")
    with urllib.request.urlopen(source["url"], timeout=90) as response:
        archive = response.read()
    if sha256(archive).hexdigest() != source["digests"]["sha256"]:
        raise RuntimeError("Paramiko source archive hash mismatch")
    sources = application / "sources"
    sources.mkdir()
    (sources / source["filename"]).write_bytes(archive)
    notices.append({"name": "paramiko corresponding source", "version": version,
                    "source_file": "sources/" + source["filename"], "url": source["url"],
                    "sha256": source["digests"]["sha256"]})

    import tkinter as tk
    window = tk.Tk()
    window.withdraw()
    try:
        for name in ("tcl", "tk"):
            version = str(window.tk.getvar(name + "_patchLevel"))
            source = Path(str(window.tk.getvar(name + "_library"))) / "license.terms"
            filename = name + "-license.terms"
            if source.is_file():
                copy(source, name, version, filename)
            else:
                # Some CPython Windows installers omit Tcl's separate notice.
                # Fetch the license of the exact runtime release from upstream.
                url = f"https://raw.githubusercontent.com/tcltk/{name}/core-{version.replace('.', '-')}/license.terms"
                request = urllib.request.Request(url, headers={"User-Agent": "Arcaea-Uploader-build"})
                with urllib.request.urlopen(request, timeout=45) as response:
                    (target / filename).write_bytes(response.read())
                notices.append({"name": name, "version": version, "license_file": "licenses/" + filename,
                                "source": url})
    finally:
        window.destroy()
    vendor = ROOT / "arcaea_uploader/_vendor/arcaea_bundler"
    copy(vendor / "LICENSE", "Arcaea-Bundler", "pinned upstream revision", "Arcaea-Bundler.txt")
    (application / "THIRD_PARTY_LICENSES.json").write_text(
        json.dumps(notices, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    if sys.platform != "win32" or platform.architecture()[0] != "64bit":
        raise SystemExit("Build the Windows x64 distribution using 64-bit Python on Windows.")
    sys.path.insert(0, str(ROOT))
    from arcaea_uploader import __version__

    output = args.output_directory.resolve()
    output.mkdir(parents=True, exist_ok=True)
    work_root = ROOT / "state/package-build"
    work_root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="windows-", dir=work_root) as temporary:
        stage = Path(temporary)
        assert stage.resolve().is_relative_to(work_root.resolve())
        subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                        "--distpath", str(stage / "dist"), "--workpath", str(stage / "work"),
                        str(ROOT / "Arcaea-Uploader.spec")], cwd=ROOT, check=True)
        application = stage / "dist/Arcaea-Uploader"
        for name in ("README.md", "CHANGELOG.md", "THIRD_PARTY_NOTICES.md", "config.example.json"):
            shutil.copyfile(ROOT / name, application / name)
        if (ROOT / "LICENSE").is_file():
            shutil.copyfile(ROOT / "LICENSE", application / "LICENSE")
        shutil.copytree(ROOT / "docs", application / "docs")
        collect_notices(application)
        report = stage / "smoke.json"
        subprocess.run([str(application / "Arcaea-Uploader.exe"), "--smoke-test", str(report)],
                       cwd=application, check=True, timeout=60)
        result = json.loads(report.read_text(encoding="utf-8"))
        if not result.get("ok") or not result.get("frozen") or result["version"] != __version__:
            raise RuntimeError("Frozen application smoke check failed: " + str(result))
        archive = output / f"Arcaea-Uploader-{__version__}-Windows-x64.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(application.rglob("*")):
                if path.is_file():
                    bundle.write(path, path.relative_to(application.parent).as_posix())
        print("Portable application:", archive)
        print("Smoke checks:", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
