from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import build


class BuildTest(unittest.TestCase):
    def test_archive_is_minimal_complete_and_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = build.build(Path(directory) / "first.ankiaddon")
            second = build.build(Path(directory) / "second.ankiaddon")

            self.assertEqual(_sha256(first), _sha256(second))
            with zipfile.ZipFile(first) as archive:
                names = archive.namelist()
                expected = [
                    "__init__.py", "addon.py", "api.py", "config.py", "credentials.py",
                    "engine.py", "manifest.json", "storage.py", "ui.py", "LICENSE",
                ]
                self.assertEqual(names, expected)
                for name, source in build._entries():
                    self.assertEqual(archive.read(name), source.read_bytes())

                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(manifest["package"], "taskhero_anki")
                self.assertEqual(manifest["name"], "TaskHero for Anki")

    def test_unexpected_private_file_cannot_enter_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "runtime"
            shutil.copytree(build.SOURCE, source, ignore=shutil.ignore_patterns("__pycache__"))
            (source / "credentials.json").write_text("{}", encoding="utf-8")
            output = Path(directory) / "candidate.ankiaddon"
            output.write_bytes(b"previous candidate")
            with patch.object(build, "SOURCE", source), self.assertRaisesRegex(ValueError, "Unexpected runtime file"):
                build.build(output)
            self.assertEqual(output.read_bytes(), b"previous candidate")

    def test_missing_required_file_fails_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "runtime"
            shutil.copytree(build.SOURCE, source, ignore=shutil.ignore_patterns("__pycache__"))
            (source / "api.py").unlink()
            with patch.object(build, "SOURCE", source), self.assertRaisesRegex(ValueError, "Required runtime file"):
                build.build(Path(directory) / "candidate.ankiaddon")

    def test_symlink_cannot_import_external_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "runtime"
            shutil.copytree(build.SOURCE, source, ignore=shutil.ignore_patterns("__pycache__"))
            self._symlink(source / "external.txt", Path(directory) / "private.txt")
            with patch.object(build, "SOURCE", source), self.assertRaisesRegex(ValueError, "Symlinks"):
                build.build(Path(directory) / "candidate.ankiaddon")

    def test_license_symlink_is_rejected_without_overwriting_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            external = root / "private.txt"
            external.write_text("synthetic private data", encoding="utf-8")
            self._symlink(root / "LICENSE", external)
            output = root / "candidate.ankiaddon"
            output.write_bytes(b"previous candidate")
            with patch.object(build, "ROOT", root), self.assertRaisesRegex(ValueError, "Symlinks.*LICENSE"):
                build.build(output)
            self.assertEqual(output.read_bytes(), b"previous candidate")

    def _symlink(self, link: Path, target: Path) -> None:
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


if __name__ == "__main__":
    unittest.main()
