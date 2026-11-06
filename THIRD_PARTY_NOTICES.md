# Third-party notices

## Arcaea-Bundler

- Author and copyright holder: **Lost-MSth**, Copyright (c) 2024 Lost-MSth.
- Upstream: [Lost-MSth/Arcaea-Bundler](https://github.com/Lost-MSth/Arcaea-Bundler).
- License: **MIT**, reproduced in full in [LICENSE](arcaea_uploader/_vendor/arcaea_bundler/LICENSE).
- Pinned revision: [`2b659e230f9d03520e4df0642a0d3cf6a7e78395`](https://github.com/Lost-MSth/Arcaea-Bundler/tree/2b659e230f9d03520e4df0642a0d3cf6a7e78395).
- Included source: `arcaea_bundler/main.py`, preserved byte-for-byte at `arcaea_uploader/_vendor/arcaea_bundler/main.py`.
- Provenance, SHA-256 checksums and selection rationale: `arcaea_uploader/_vendor/arcaea_bundler/UPSTREAM.json`.
- Application integration: `arcaea_uploader/bundler.py` is maintained by Arcaea-Uploader; it is separate from the original upstream source.

The MIT copyright and permission notice accompany the vendored source and Python distributions. Keep them with copies or substantial portions of this component. Future standalone application builds must also retain this notice and the full license; the desktop application's “关于与授权” view reads the bundled license.

The chosen revision matches the maintainer's existing deployment. This is a pinned integration, not a claim to use the latest upstream version or to be endorsed by its author.

## Maintainer-provided media script

The original `resize_and_preview.py` from the maintainer's manual update workflow is preserved at `arcaea_uploader/_legacy/resize_and_preview.py`, with its checksum and integration changes in `ORIGIN.json`. Runtime helpers are maintained in `arcaea_uploader/media.py`. The historical snapshot is not used to execute media jobs.

## Runtime dependencies and external tools

Python/Tkinter and Pillow retain their own licenses. Python package installations obtain these dependencies separately. Windows portable releases include the actual runtime license texts under `licenses/` and a versioned index in `THIRD_PARTY_LICENSES.json`; Pillow's wheel license also contains its bundled codec notices. The build copies Python's full license, the exact Tcl/Tk runtime notices, Pillow's distribution license, and the Bundler license.

SSH/SFTP uses [Paramiko](https://www.paramiko.org/) (LGPL-2.1-or-later), installed as an unmodified Python dependency. Its source is available from [upstream](https://github.com/paramiko/paramiko). Portable builds include Paramiko and its cryptographic/runtime dependencies, retaining the full license and notice files from their installed distributions, with package versions in `THIRD_PARTY_LICENSES.json`. Paramiko is collected as replaceable `.py` files in `_internal/paramiko`, outside the executable's module archive. The exact version's complete source distribution is included under `sources/`, with its PyPI SHA-256 checked during packaging and recorded in the license index. This component is not incorporated into this project's MIT license; users may modify or replace it under its own license, including debugging those modifications.

FFmpeg is invoked as an external program, discovered through PATH or an explicit executable path; no FFmpeg binary is included in this repository, Python packages, or Windows portable releases.

Arcaea-Uploader's own code is licensed under the MIT License, Copyright (c) 2026 StarSkyer; see the root LICENSE. The vendored component retains its original MIT license and copyright. No game assets are included or licensed by these notices.
