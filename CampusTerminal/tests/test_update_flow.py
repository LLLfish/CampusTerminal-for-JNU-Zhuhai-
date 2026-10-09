# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the actual app event loop using fake release/backend boundaries."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def scenario(mode):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PyQt5.QtCore import QTimer
    from PyQt5.QtWidgets import QApplication
    from gui import app as gui
    from gui.bridge import update
    events, controllers = [], []

    class Backend:
        def __init__(self, _root):
            pass
        def status(self):
            return {"ok": True, "active": True, "phase": "online"}
        ensure_started = status
        def configure(self, options):
            return {**self.status(), "options": options}
        def shutdown(self, handoff=False, adapter="", graceful=False):
            events.append(("shutdown", handoff, graceful))
            if mode == "failed":
                return {"ok": False, "error": "BackendStopTimeout"}
            return {"ok": True, "pending": False, "active": False}

    real_controller = gui.UpdateController
    def controller(*args):
        instance = real_controller(*args)
        controllers.append(instance)
        def checked(*_):
            events.append(("checked",))
            if mode != "decline":
                instance.accept()
        instance.service.checked.connect(checked)
        QTimer.singleShot(30, instance.check)
        QTimer.singleShot(650, QApplication.instance().quit)
        return instance

    data = {**gui.store.DEFAULTS, "auto_connect": False, "auto_start": False, "inode_fallback": True}
    nic = {"id": "nic", "name": "test", "label": "test", "up": True}
    with tempfile.TemporaryDirectory() as temp, \
            patch.object(gui, "UpdateController", side_effect=controller), \
            patch.object(gui, "BackendClient", Backend), \
            patch.object(gui, "SingleInstance", return_value=SimpleNamespace(acquire=lambda: True)), \
            patch.object(gui, "list_adapters", return_value=[nic]), \
            patch.object(gui, "original_present", return_value=True), \
            patch.object(gui.store, "load", return_value=data), \
            patch.object(gui.store, "save", side_effect=lambda _: events.append(("save",))), \
            patch.object(gui.store, "APP_DIR", Path(temp)), \
            patch.object(gui.store, "clear_password"), \
            patch.object(gui.store, "startup_enabled", return_value=False), \
            patch.object(gui.QSystemTrayIcon, "isSystemTrayAvailable", return_value=False), \
            patch("gui.bridge.update_service.frozen", return_value=True), \
            patch.object(update, "check_release", return_value={"version": "999.1.0"}), \
            patch.object(update, "prepare_update", return_value="test-plan.json"), \
            patch.object(update, "launch_update", side_effect=lambda _: events.append(("launch",))), \
            patch.object(update, "cancel_update", side_effect=lambda _: events.append(("cancel",))):
        assert gui.main() == 0
    assert ("checked",) in events, events
    if mode == "decline":
        assert not any(e[0] in ("shutdown", "launch") for e in events), events
    else:
        assert ("shutdown", False, True) in events, events
        assert events.index(("save",)) < events.index(("launch",)) < events.index(("shutdown", False, True)), events
        if mode == "failed":
            assert ("cancel",) in events and not controllers[0].installing, events


class UpdateFlowTests(unittest.TestCase):
    def test_accept_decline_and_failed_shutdown_in_real_app_loop(self):
        for mode in ("accept", "decline", "failed"):
            with self.subTest(mode=mode):
                result = subprocess.run([sys.executable, __file__, "--mode", mode], capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))


if __name__ == "__main__":
    if "--mode" in sys.argv:
        scenario(sys.argv[-1])
    else:
        unittest.main()
