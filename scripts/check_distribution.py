"""Check release contents and generate SHA-256 checksums without extracting."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import tarfile
import zipfile


def check(path):
    if path.suffix in {".whl", ".zip"}:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            contents = {name: archive.read(name) for name in names
                        if name.endswith(("config.example.json", "UPSTREAM.json"))}
    else:
        with tarfile.open(path, "r:gz") as archive:
            names = archive.getnames()
            contents = {name: archive.extractfile(name).read() for name in names
                        if name.endswith(("config.example.json", "UPSTREAM.json"))}
    forbidden = {"config.local.json", "LOCAL.md", ".env", ".git", ".ssh", "state", "database"}
    for name in names:
        parts = Path(name).parts
        if any(part in forbidden or part.startswith(".venv") for part in parts):
            raise ValueError(f"Private/generated content in {path.name}: {name}")
        if name.endswith((".cb", ".oldjson", ".aff", ".ogg", ".mp4", ".lnk", ".pem", ".key")):
            raise ValueError(f"Resource or credential file in {path.name}: {name}")
    if not any(name.endswith("arcaea_bundler/LICENSE") for name in names):
        raise ValueError("Missing Bundler license in " + path.name)
    if not any(name.endswith("THIRD_PARTY_NOTICES.md") for name in names):
        raise ValueError("Missing third-party notices in " + path.name)
    if path.suffix == ".whl":
        own_license = any(name.endswith(".dist-info/licenses/LICENSE") for name in names)
    else:
        prefix = names[0].split("/")[0]
        own_license = prefix + "/LICENSE" in names
    if not own_license:
        raise ValueError("Missing project MIT license in " + path.name)
    examples = [json.loads(data) for name, data in contents.items() if name.endswith("config.example.json")]
    for example in examples:
        if len(example["import"]["background_pairs"]) < 36:
            raise ValueError("The example background table is incomplete")
    if path.suffix == ".zip":
        if "Arcaea-Uploader/Arcaea-Uploader.exe" not in names or not examples:
            raise ValueError("Portable archive is missing the executable or example config")
        if not any(name.endswith("THIRD_PARTY_LICENSES.json") for name in names):
            raise ValueError("Portable archive is missing runtime notices")
    print("Checked", path.name, "-", len(names), "files")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, nargs="?", default=Path("dist"))
    parser.add_argument("--write-checksums", action="store_true")
    args = parser.parse_args()
    paths = sorted(p for p in args.directory.iterdir() if p.suffix in {".whl", ".zip"} or p.name.endswith(".tar.gz"))
    if not paths:
        raise SystemExit("No release artifacts found")
    for path in paths:
        check(path)
    if args.write_checksums:
        sums = []
        for path in paths:
            digest = sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            sums.append(digest.hexdigest() + "  " + path.name)
        (args.directory / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
