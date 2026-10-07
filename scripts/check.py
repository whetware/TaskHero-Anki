#!/usr/bin/env python3
"""Dependency-free publication checks; never print matched secret values."""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import tabnanny
import tokenize
from fnmatch import fnmatchcase
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERNS = (
    re.compile(rb"th-int-v1-[a-z]+-[A-Za-z0-9_-]{43}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}"),
    re.compile(rb"(?:ghp_|github_pat_)[A-Za-z0-9_]{30,}"),
)
PRIVATE_DIRECTORIES = {".history", "scratch", "__pycache__", ".venv", ".pytest_cache", "collection.media"}
PRIVATE_NAMES = (
    ".env", ".env.*", "*.env", "credentials.json*", "settings.json*", ".credentials.json.*", ".settings.json.*",
    "*.anki2*", "*.apkg", "*.colpkg", "*.ankiaddon", "*.db*", "*.sqlite*", "*.log", "*.pem", "*.key",
    "*.pyc", "*.pyo",
)


def check() -> None:
    if "--require-anki" in sys.argv and importlib.util.find_spec("aqt") is None:
        raise SystemExit("Release validation requires Anki's bundled Python and aqt packages.")
    listing = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT,
    )
    paths = sorted({Path(name.decode()) for name in listing.split(b"\0") if name})
    problems = []
    for relative in paths:
        path = ROOT / relative
        if path.is_symlink():
            problems.append(f"{relative}: publication files must not be symlinks")
            continue
        if (any(part.lower() in PRIVATE_DIRECTORIES for part in relative.parts[:-1])
                or any(fnmatchcase(relative.name.lower(), pattern) for pattern in PRIVATE_NAMES)):
            problems.append(f"{relative}: private/generated data must not be published")
            continue
        if not path.exists():
            continue
        content = path.read_bytes()
        if any(pattern.search(content) for pattern in SECRET_PATTERNS):
            problems.append(f"{relative}: potential credential; inspect privately")
        if relative.suffix == ".py":
            try:
                compile(content, str(relative), "exec")
                with tokenize.open(path) as source:
                    tabnanny.process_tokens(tokenize.generate_tokens(source.readline))
            except (SyntaxError, tabnanny.NannyNag, tokenize.TokenError) as error:
                problems.append(f"{relative}: Python syntax/indentation check failed ({type(error).__name__})")
        if relative.suffix in {".py", ".md", ".yml", ".json"}:
            for number, line in enumerate(content.splitlines(), 1):
                if line.rstrip() != line:
                    problems.append(f"{relative}:{number}: trailing whitespace")
    if problems:
        raise SystemExit("\n".join(problems))
    print(f"Publication checks passed for {len(paths)} files (syntax, indentation, credential patterns, private data).")


if __name__ == "__main__":
    check()
