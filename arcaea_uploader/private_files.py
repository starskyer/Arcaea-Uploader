"""Private staging copies; links are confined to one append transaction."""

import os
from pathlib import Path
import shutil
import stat
from tempfile import mkstemp


def copy_writable(source, target):
    result = shutil.copy2(source, target)
    os.chmod(result, Path(result).stat().st_mode | stat.S_IWUSR)
    return result


def replace_copy(source, target):
    """Never overwrite an inode shared with the preceding private candidate."""
    target = Path(target)
    descriptor, name = mkstemp(prefix=".copy-", dir=target.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        copy_writable(source, temporary)
        if target.exists() and not target.stat().st_mode & stat.S_IWUSR:
            os.chmod(target, target.stat().st_mode | stat.S_IWUSR)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def clone_candidate(source, target):
    """Share unchanged private files only until the append commits or rolls back.

    Editable JSONs always receive independent copies. Every candidate writer
    replaces files atomically. Live assets and author packages are never linked.
    Unsupported filesystems fall back to ordinary copies.
    """
    source = Path(source)
    editable = {"active/songs/songlist", "active/songs/packlist", "active/songs/unlocks"}
    def clone_file(original, destination):
        path = Path(original)
        if path.relative_to(source).as_posix() not in editable and path.stat().st_mode & stat.S_IWUSR:
            try:
                os.link(original, destination)
                return destination
            except OSError:
                pass
        return copy_writable(original, destination)
    return shutil.copytree(source, target, copy_function=clone_file)
