# PyInstaller onedir build. Only named resources are included; no local config.
from pathlib import Path

root = Path(SPECPATH)
vendor = root / "arcaea_uploader" / "_vendor" / "arcaea_bundler"
datas = [(str(vendor / name), "arcaea_uploader/_vendor/arcaea_bundler")
         for name in ("LICENSE", "UPSTREAM.json", "main.py")]

a = Analysis([str(root / "Arcaea-Uploader.pyw")], pathex=[str(root)], datas=datas,
             hiddenimports=["arcaea_uploader.bundler"], hookspath=[str(root / "scripts/hooks")], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Arcaea-Uploader",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Arcaea-Uploader")
