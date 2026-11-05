"""Conservative AFF template detection shared by preprocessing and planning."""

import re


_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
_TIMING = re.compile(rf"timing\(\s*{_NUMBER}\s*,\s*{_NUMBER}\s*,\s*{_NUMBER}\s*\)\s*;?")


def is_empty_aff(data: bytes) -> bool:
    """Only recognize known headers plus timing-only bodies, never file size.

    Unknown syntax, undecodable files and any playable/event statement are kept.
    Timing-only editor templates are not actual difficulty updates.
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        return False
    text = re.sub(r"//[^\r\n]*", "", text).strip()
    if not text:
        return True
    lines = text.splitlines()
    separators = [i for i, line in enumerate(lines) if line.strip() == "-"]
    if len(separators) != 1:
        return False
    index = separators[0]
    headers = [line.strip() for line in lines[:index] if line.strip()]
    if not headers or not all(re.fullmatch(rf"(?:AudioOffset|TimingPointDensityFactor)\s*:\s*{_NUMBER}", h) for h in headers):
        return False
    body = "\n".join(lines[index + 1:]).strip()
    return not _TIMING.sub("", body).strip()


def hitsound_files(data: bytes) -> set[str]:
    """AFF hitsound glass_wav refers to glass.wav; do not rewrite the chart."""
    text = re.sub(r"//[^\r\n]*", "", data.decode("utf-8-sig", errors="replace"))
    result = set()
    for event in re.finditer(r"\barc\(([^()\r\n]*)\)", text):
        args = [v.strip() for v in event.group(1).split(",")]
        if len(args) >= 10 and args[8].endswith("_wav") and args[8] != "none":
            result.add(args[8][:-4] + ".wav")
    return result
