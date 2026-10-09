# SPDX-License-Identifier: GPL-3.0-or-later
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import QThread
from PyQt5.QtTest import QSignalSpy, QTest
from PyQt5.QtWidgets import QApplication
from gui import theme
from gui.bridge.update_service import UpdateController, UpdateService
from gui.shell.window import MainWindow

APP = QApplication.instance() or QApplication([])


def until(predicate):
    deadline = time.monotonic() + 2
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(10)
    assert predicate(), "Update result did not reach the Qt thread"


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = UpdateService(Path("unused"))
        self.addCleanup(self.service.close)

    def test_background_check_uses_running_version_and_qt_delivery(self):
        worker_threads, receiver_threads = [], []
        def check(version, edition):
            worker_threads.append(threading.get_ident())
            self.assertEqual(version, "9.12.34")
            self.assertEqual(edition, "portable")
            return {"version": "9.12.35"}
        self.service.checked.connect(lambda *_: receiver_threads.append(QThread.currentThread()))
        spy = QSignalSpy(self.service.checked)
        with patch("gui.bridge.update.check_release", side_effect=check), \
                patch("gui.bridge.update_service.theme.APP_VERSION", "9.12.34"), \
                patch("gui.bridge.update_service.edition.current", return_value="portable"):
            self.assertTrue(self.service.check())
            until(lambda: len(spy) == 1)
        self.assertNotEqual(worker_threads[0], threading.get_ident())
        self.assertEqual(receiver_threads, [APP.thread()])
        self.assertEqual(list(spy[0]), [True, {"version": "9.12.35"}, ""])
        self.assertFalse(self.service.busy)

    def test_duplicate_check_is_ignored_and_close_drops_late_result(self):
        entered, finish = threading.Event(), threading.Event()
        def check(*_):
            entered.set()
            finish.wait(2)
        spy = QSignalSpy(self.service.checked)
        with patch("gui.bridge.update.check_release", side_effect=check), \
                patch("gui.bridge.update_service.edition.current", return_value="installed"):
            self.assertTrue(self.service.check())
            self.assertTrue(entered.wait(1))
            self.assertFalse(self.service.check())
            self.service.close()
            finish.set()
            QTest.qWait(50)
        self.assertEqual(len(spy), 0)

    def test_failure_releases_busy_and_allows_retry(self):
        spy = QSignalSpy(self.service.checked)
        with patch("gui.bridge.update.check_release", side_effect=OSError("network")), \
                patch("gui.bridge.update_service.edition.current", return_value="installed"), \
                patch("gui.bridge.update_service.trace.emit"):
            self.service.check(False)
            until(lambda: len(spy) == 1)
        self.assertFalse(spy[0][0])
        self.assertTrue(spy[0][2])
        self.assertFalse(self.service.busy)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.window = MainWindow(.2, theme.pick_font_family())
        self.alerts, self.install = Mock(), Mock()
        self.controller = UpdateController(self.window, self.alerts, Path("unused"), self.install)
        self.real_service = self.controller.service
        self.controller.service = Mock()
        self.controller.service.check.return_value = True
        self.controller.service.prepare.return_value = True
        self.addCleanup(self.window.close)
        self.addCleanup(self.real_service.close)
        self.addCleanup(self.controller.close)

    def test_startup_is_opt_in_and_preview_never_checks(self):
        self.controller.schedule_startup(False)
        self.controller.schedule_startup(True, preview=True)
        QTest.qWait(10)
        self.controller.service.check.assert_not_called()
        self.controller.schedule_startup(True)
        until(lambda: self.controller.service.check.called)
        self.controller.service.check.assert_called_once_with(False)

    def test_manual_action_works_when_automatic_checks_disabled(self):
        self.window.settings.updates.button.click()
        self.controller.service.check.assert_called_once_with(True)
        self.assertFalse(self.window.settings.updates.button.isEnabled())
        self.controller._checked(True, None, "")
        self.assertTrue(self.window.settings.updates.button.isEnabled())
        self.assertEqual(self.window.settings.updates.button.text(), "当前已是最新版本")
        self.alerts.warn.assert_called_once_with("检查更新", "当前已是最新版本", "information")

    def test_startup_failure_and_latest_are_quiet(self):
        self.controller._checked(False, None, "network error")
        self.controller._checked(False, None, "")
        self.alerts.warn.assert_not_called()
        self.alerts.deliver.assert_not_called()
        self.install.assert_not_called()

    def test_offer_does_not_install_until_accepted_and_prepared(self):
        release = {"version": "999.1.0"}
        with patch("gui.bridge.update_service.frozen", return_value=True):
            self.controller._checked(False, release, "")
            self.install.assert_not_called()
            self.controller.service.prepare.assert_not_called()
            self.assertEqual(self.alerts.deliver.call_args.args[0].action, "install-update")
            self.controller.accept()
        self.controller.service.prepare.assert_called_once_with(release)
        self.install.assert_not_called()
        self.controller._prepared(Path("plan.json"), "")
        self.install.assert_called_once_with(Path("plan.json"))

    def test_source_checkout_offers_no_install_and_prepare_failure_keeps_running(self):
        with patch("gui.bridge.update_service.frozen", return_value=False):
            self.controller._checked(True, {"version": "999.1.0"}, "")
            self.controller.accept()
        self.controller.service.prepare.assert_not_called()
        self.alerts.deliver.assert_not_called()
        self.controller._prepared(None, "download failed")
        self.install.assert_not_called()
        self.assertFalse(self.controller.installing)

    def test_settings_geometry_order_and_opt_in_survive_resize(self):
        settings = self.window.settings
        settings.autos.set_options({"auto_check_update": True})
        for width, height in ((300, 560), (600, 700), (350, 750)):
            settings.resize(width, height)
            APP.processEvents()
            blocks = (settings.nic, settings.ip, settings.types, settings.updates, settings.autos)
            for above, below in zip(blocks, blocks[1:]):
                self.assertLess(above.geometry().bottom(), below.geometry().top())
            for block in blocks:
                self.assertTrue(settings.content.rect().contains(block.geometry()))
            self.assertTrue(settings.autos.toggles["auto_check_update"].isChecked())
        self.assertEqual(list(settings.autos.toggles)[-1], "auto_check_update")


if __name__ == "__main__":
    unittest.main()
