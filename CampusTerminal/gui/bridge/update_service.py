# SPDX-License-Identifier: GPL-3.0-or-later
"""Background update work and Qt-thread presentation, independent of authentication."""
import threading

from PyQt5.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

from gui import theme
from gui.bridge import edition, trace
from gui.bridge.paths import frozen
from gui.shell.notifications import Notice


class UpdateService(QObject):
    checked = pyqtSignal(bool, object, str)
    prepared = pyqtSignal(object, str)
    _finished = pyqtSignal(str, bool, object, str)

    def __init__(self, root, parent=None):
        super().__init__(parent)
        self.root = root
        self.busy = False
        self._closed = threading.Event()
        self._finished.connect(self._deliver)

    def _start(self, kind, manual, operation):
        if self.busy or self._closed.is_set():
            return False
        self.busy = True

        def run():
            result, error = None, ""
            try:
                result = operation()
            except Exception as exc:
                error = "检查更新失败，请检查网络连接后重试。" if kind == "check" else "下载或校验更新失败，当前版本未更改，请重试。"
                trace.emit("update_failed", reason=type(exc).__name__, outcome=kind)
            if not self._closed.is_set():
                try:
                    self._finished.emit(kind, manual, result, error)
                except RuntimeError:
                    pass  # The owning Qt window may already have been destroyed.

        threading.Thread(target=run, name="release-" + kind, daemon=True).start()
        return True

    def check(self, manual=True):
        from gui.bridge import update
        return self._start("check", manual, lambda: update.check_release(theme.APP_VERSION, edition.current()))

    def prepare(self, release):
        from gui.bridge import update
        return self._start("prepare", False, lambda: update.prepare_update(release, self.root))

    @pyqtSlot(str, bool, object, str)
    def _deliver(self, kind, manual, result, error):
        if self._closed.is_set():
            return
        self.busy = False
        if kind == "check":
            self.checked.emit(manual, result, error)
        else:
            self.prepared.emit(result, error)

    def close(self):
        self._closed.set()


class UpdateController(QObject):
    def __init__(self, window, alerts, root, begin_install, parent=None):
        super().__init__(parent or window)
        self.window, self.alerts = window, alerts
        self.begin_install = begin_install
        self.service = UpdateService(root, self)
        self.release = None
        self.installing = False
        self.closed = False
        window.settings.check_update.connect(self.check)
        self.service.checked.connect(self._checked)
        self.service.prepared.connect(self._prepared)

    def schedule_startup(self, enabled, preview=False):
        if enabled and not preview:
            QTimer.singleShot(0, lambda: self.check(False))

    def check(self, manual=True):
        if self.closed or self.installing:
            return
        if self.service.check(manual):
            self.window.settings.updates.set_status("正在检查…", True)

    def _checked(self, manual, release, error):
        if self.closed:
            return
        if error:
            self.window.settings.updates.set_status("检查失败，点击重试")
            if manual:
                self.alerts.warn("检查更新", error)
            return
        if not release:
            self.release = None
            self.window.settings.updates.set_status("当前已是最新版本")
            if manual:
                self.alerts.warn("检查更新", "当前已是最新版本", "information")
            return
        self.release = release
        self.window.settings.updates.set_status("发现新版本 " + release["version"])
        message = ("发现主仓库新版本 " + release["version"] + "。下载完成后将停止校园网认证，更新到当前目录并重启。")
        if not frozen():
            self.alerts.warn("检查更新", "发现主仓库新版本 " + release["version"] + "。源码运行仅支持检查；请使用打包版本安装更新。", "information")
            return
        self.alerts.deliver(Notice("发现新版本", message, "information", "confirm", "install-update"))

    def accept(self):
        if self.closed or self.installing or not self.release or not frozen():
            return
        if self.service.prepare(self.release):
            self.window.settings.updates.set_status("正在下载并校验…", True)

    def _prepared(self, plan_path, error):
        if self.closed:
            return
        if error:
            self.window.settings.updates.set_status("下载失败，点击重试")
            self.alerts.warn("检查更新", error)
            return
        self.installing = True
        try:
            self.begin_install(plan_path)
        except Exception as exc:
            self.installing = False
            trace.emit("update_handoff_failed", reason=type(exc).__name__)
            self.window.settings.updates.set_status("安装准备失败，点击重试")
            self.alerts.warn("检查更新", "无法启动更新或保存设置，当前版本未更改。请重试。")
        else:
            self.window.settings.updates.set_status("正在等待终端退出…", True)

    def close(self):
        self.closed = True
        self.service.close()

    def shutdown_failed(self):
        self.installing = False
        self.window.settings.updates.set_status("退出未完成，点击重试")
        self.alerts.warn("检查更新", "后台未能正常退出，本次更新已取消。当前版本未更改。")
