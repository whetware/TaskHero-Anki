"""Exercise package identity and upgrades through Anki's real add-on manager."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import build
from taskhero_anki.config import Settings, SettingsStore
from taskhero_anki.credentials import CredentialStore
from taskhero_anki.storage import StateStore


ADDON_ID = "1715570135"
HAS_ANKI = importlib.util.find_spec("aqt") is not None
if HAS_ANKI:
    from aqt.addons import AddonManager, DownloadOk, InstallOk, download_and_install_addon
    from aqt.qt import QAction, QApplication, QWidget


@unittest.skipUnless(HAS_ANKI, "requires Anki's bundled Python/Qt")
class InstallationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["taskhero-anki-tests"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.addons = root / "addons21"
        self.addons.mkdir()
        self.profile = root / "profile"
        self.profile.mkdir()
        self.main = QWidget()
        self.main.pm = SimpleNamespace(addonFolder=lambda: str(self.addons))
        self.main.form = SimpleNamespace(actionAdd_ons=QAction(self.main))
        self.addCleanup(self.main.close)
        self.addCleanup(setattr, sys, "path", sys.path[:])
        self.manager = AddonManager(self.main)
        self.package = build.build(root / "candidate.ankiaddon")

    def _install(self, method):
        if method == "file":
            return self.manager.install(str(self.package), force_enable=True)
        downloaded = DownloadOk(
            data=self.package.read_bytes(), filename="TaskHero_for_Anki.ankiaddon",
            mod_time=1_791_403_515, min_point_version=260801,
            max_point_version=260903, branch_index=0,
        )
        client = Mock()
        with patch("aqt.addons.download_addon", return_value=downloaded) as download:
            addon_id, result = download_and_install_addon(
                self.manager, client, int(ADDON_ID), force_enable=True,
            )
        download.assert_called_once_with(client, int(ADDON_ID))
        self.assertEqual(addon_id, int(ADDON_ID))
        return result

    def _install_earlier_manual_copy(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(self.package) as source, zipfile.ZipFile(archive, "w") as target:
            for name in source.namelist():
                content = source.read(name)
                if name == "manifest.json":
                    manifest = json.loads(content)
                    manifest.update(package="taskhero_anki", conflicts=[])
                    content = json.dumps(manifest).encode()
                target.writestr(name, content)
        archive.seek(0)
        self.assertIsInstance(self.manager.install(archive), InstallOk)

    def test_both_install_methods_share_identity_and_update_in_place(self):
        for method in ("file", "ankiweb", "file"):
            with self.subTest(method=method):
                result = self._install(method)
                self.assertIsInstance(result, InstallOk)
                self.assertTrue(result.compatible)
                self.assertEqual(result.conflicts, set())
                self.assertEqual(self.manager.allAddons(), [ADDON_ID])
                self.assertEqual(self.manager.ankiweb_addons(), [int(ADDON_ID)])
                self.assertTrue(self.manager.isEnabled(ADDON_ID))
                self.assertEqual(self.manager.addonConflicts(ADDON_ID), ["taskhero_anki"])
                self.assertEqual(self.manager.addon_meta(ADDON_ID).page(),
                                 f"https://ankiweb.net/shared/info/{ADDON_ID}")
                with zipfile.ZipFile(self.package) as archive:
                    for name in archive.namelist():
                        self.assertEqual((self.addons / ADDON_ID / name).read_bytes(), archive.read(name))

    def test_upgrade_disables_earlier_copy_without_touching_profile_state(self):
        self._install_earlier_manual_copy()
        data = self.profile / "taskhero_anki"
        settings = Settings(habit_id="synthetic-habit", habit_title="Study Anki")
        SettingsStore(data / "settings.json").save(settings)
        CredentialStore(data / "credentials.json").save("th-int-v1-anki-" + "A" * 43)
        store = StateStore(data / "state.sqlite3")
        now_ms = 1_791_403_515_000
        review_ids = set(range(now_ms - 17, now_ms))
        store.record_reviews([(rid, -1) for rid in review_ids], "desktop", now_ms, now_ms, settings.day_boundary)
        store.finalize_reviews(store.due_pending_reviews(now_ms), review_ids)
        store.materialize_events(settings.day_boundary.day(now_ms), settings, now_ms)
        sent = store.claim_next_event(now_ms)
        store.mark_sent(sent, {"trackerPoint": {"id": "synthetic-receipt"}}, now_ms)
        original = {path.name: path.read_bytes() for path in data.iterdir()}

        for method in ("file", "ankiweb"):
            with self.subTest(method=method):
                # Start each path with an enabled earlier copy and no new copy.
                if (self.addons / ADDON_ID).exists():
                    self.manager.deleteAddon(ADDON_ID)
                    self.manager.toggleEnabled("taskhero_anki", enable=True)
                result = self._install(method)
                self.assertIsInstance(result, InstallOk)
                self.assertEqual(result.conflicts, {"taskhero_anki"})
                self.assertEqual(self.manager.allAddons(), [ADDON_ID, "taskhero_anki"])
                self.assertEqual([addon.dir_name for addon in self.manager.all_addon_meta() if addon.enabled],
                                 [ADDON_ID])
                self.assertEqual({path.name: path.read_bytes() for path in data.iterdir()}, original)


if __name__ == "__main__":
    unittest.main()
