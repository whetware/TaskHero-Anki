"""Publication checks must protect tracked files as well as untracked source."""
from __future__ import annotations

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import check as publication


class PublicationCheckTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.root_patch = patch.object(publication, "ROOT", self.root)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.temporary.cleanup()

    def _write(self, relative, content="synthetic example\n"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _check_error(self):
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as failure:
            publication.check()
        return str(failure.exception)

    def test_tracked_private_paths_are_rejected_even_when_gitignored(self):
        self._write(".gitignore", "*\n!.gitignore\n")
        private_paths = (
            ".env.production", ".credentials.json.crash-leftover", ".settings.json.crash-leftover",
            ".history/old-note.md", "scratch/session.md", "__pycache__/module.pyc", ".venv/config.txt",
            ".pytest_cache/state", "profile/collection.media/image.png", "profile/collection.anki21-wal",
            "profile/state.sqlite-wal", "profile/state.db-journal", "study.apkg", "study.colpkg",
            "backup.ankiaddon", "debug.log", "private.pem", "private.key", "credentials.json", "settings.json",
            "credentials.json.bak", "credentials.json~", "settings.json.bak", "settings.json~", "local.env",
        )
        for relative in private_paths:
            self._write(relative)
        subprocess.run(["git", "add", "-f", "--", *private_paths], cwd=self.root, check=True)
        error = self._check_error()
        for relative in private_paths:
            self.assertIn(f"{relative}: private/generated data", error)

    def test_untracked_private_files_are_rejected_without_reading_contents(self):
        self._write(".env.local")
        with patch.object(Path, "read_bytes", side_effect=AssertionError("must not read private data")):
            self.assertIn(".env.local: private/generated data", self._check_error())

    def test_repository_ignore_rules_cover_private_backups_but_keep_source(self):
        self._write(".gitignore", (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8"))
        private_paths = (
            "credentials.json.bak", "credentials.json~", "settings.json.bak", "settings.json~", "local.env",
            "profile/credentials.json.backup", "profile/settings.json.old", "nested/local.env",
            ".credentials.json.crash-leftover", ".settings.json.crash-leftover", ".env.production",
        )
        source_paths = ("taskhero_anki/credentials.py", "taskhero_anki/config.py", "docs/images/settings.png")
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--stdin"], cwd=self.root,
            input="\n".join((*private_paths, *source_paths)) + "\n", text=True, capture_output=True, check=True,
        )
        self.assertEqual(set(result.stdout.splitlines()), set(private_paths))

    def test_broken_symlinks_are_rejected(self):
        link = self.root / "broken-link"
        try:
            link.symlink_to(self.root / "missing-target")
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")
        self.assertIn("broken-link: publication files must not be symlinks", self._check_error())

    def test_valid_source_passes_and_ignored_local_files_stay_out_of_scope(self):
        self._write(".gitignore", ".env.*\nscratch/\n")
        self._write(".env.local")
        self._write("scratch/session.md")
        self._write("module.py", "answer = 42\n")
        self._write("README.md", "# Example\n")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            publication.check()
        self.assertIn("passed for 3 files", output.getvalue())

    def test_credentials_are_reported_without_printing_their_values(self):
        token = f"th-int-v1-anki-{'A' * 43}"
        self._write("accidental.txt", token)
        error = self._check_error()
        self.assertIn("potential credential", error)
        self.assertNotIn(token, error)
