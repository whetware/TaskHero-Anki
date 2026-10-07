#!/usr/bin/env python3
from __future__ import annotations

import json
import hashlib
import os
import time
import zipfile
from pathlib import Path
from typing import Iterator, Tuple


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "taskhero_anki"
OUTPUT = ROOT / "dist" / "taskhero-for-anki.ankiaddon"
RUNTIME_FILES = (
    "__init__.py", "addon.py", "api.py", "config.py", "credentials.py",
    "engine.py", "manifest.json", "storage.py", "ui.py",
)


def _entries() -> Iterator[Tuple[str, Path]]:
    for path in SOURCE.rglob("*"):
        name = path.relative_to(SOURCE).as_posix()
        if path.is_symlink():
            raise ValueError(f"Symlinks are not allowed in the runtime directory: {name}")
        if not path.is_file() or path.suffix == ".pyc":
            continue
        if name not in RUNTIME_FILES:
            raise ValueError(f"Unexpected runtime file (not packaged): {name}")
    for name in RUNTIME_FILES:
        path = SOURCE / name
        if not path.is_file():
            raise ValueError(f"Required runtime file is missing: {name}")
        yield name, path
    license_path = ROOT / "LICENSE"
    if license_path.is_symlink():
        raise ValueError("Symlinks are not allowed in package inputs: LICENSE")
    yield "LICENSE", license_path


def _archive_timestamp() -> Tuple[int, int, int, int, int, int]:
    manifest = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", manifest["mod"]))
    timestamp = time.gmtime(epoch)[:6]
    return timestamp if timestamp[0] >= 1980 else (1980, 1, 1, 0, 0, 0)


def build(output: Path = OUTPUT) -> Path:
    if not (SOURCE / "manifest.json").is_file():
        raise SystemExit("taskhero_anki/manifest.json is missing")
    if not (ROOT / "LICENSE").is_file():
        raise SystemExit("LICENSE is missing")

    # Validate everything before opening/truncating a previously built artifact.
    entries = list(_entries())
    timestamp = _archive_timestamp()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, path in entries:
            info = zipfile.ZipInfo(name, date_time=timestamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return output


def main() -> None:
    output = build()
    checksum = f"{hashlib.sha256(output.read_bytes()).hexdigest()}  {output.name}\n"
    output.with_suffix(output.suffix + ".sha256").write_text(checksum, encoding="utf-8")
    print(output)
    print(checksum, end="")


if __name__ == "__main__":
    main()
