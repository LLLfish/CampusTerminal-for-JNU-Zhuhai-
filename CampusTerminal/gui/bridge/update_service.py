# SPDX-License-Identifier: GPL-3.0-or-later
"""Background update work and Qt-thread presentation, independent of authentication."""
import threading

from PyQt5.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

from gui import theme
from gui.bridge import edition, trace
from gui.bridge.paths import frozen
from gui.shell.notifications import Notice

# One wording for every failure path so the user reads the same sentence shape.
CHECK_FAILED = "更新失败：无法获取版本信息，请检查网络后重试。"
PREPARE_FAILED = "更新失败：下载或校验未完成，当前版本未更改，请重试。"
INSTALL_FAILED = "更新失败：无法启动更新，当前版本未更改，请重试。"
SHUTDOWN_FAILED = "更新失败：后台未能正常退出，本次更新已取消，当前版本未更改。"
RESULT_FAILED = "更新失败：{reason}，当前版本未更改。"

STATUS_CHECKING = "正在检查…"
STATUS_LATEST = "当前已是最新版本"
STATUS_FAILED = "更新失败，点击重试"
STATUS_READY = "发现新版本 "
STATUS_PREPARING = "正在下载并校验…"
STATUS_WAITING = "正在等待终端退出…"


class UpdateService(QObject):
    checked = pyqtSignal(bool, object, str)
    prepared = pyqtSignal(object, str)
    notes_ready = pyqtSignal(str, str)
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
                error = CHECK_FAILED if kind == "check" else PREPARE_FAILED
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

    def notes(self, version):
        """Fetch one version's release notes without claiming the busy flag."""
        from gui.bridge import update

        def run():
            try:
                value = update.release_notes(version)
            except Exception:
                value = ""
            if not self._closed.is_set():
                try:
                    self.notes_ready.emit(str(version), value or "")
                except RuntimeError:
                    pass  # The owning Qt window may already have been destroyed.

        threading.Thread(target=run, name="release-notes", daemon=True).start()
        return True

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
        self.auto_install = False
        self.ignored_version = ""
        self._log_sections = []
        self._log_pending = None
        self._log_notes = {}
        window.settings.check_update.connect(self.check)
        window.settings.log_requested.connect(self.open_log)
        self.service.checked.connect(self._checked)
        self.service.prepared.connect(self._prepared)
        self.service.notes_ready.connect(self._notes_ready)

    def schedule_startup(self, check_enabled, auto_install=False, ignored_version="", preview=False):
        self.auto_install = bool(auto_install)
        self.ignored_version = str(ignored_version or "")
        if check_enabled and not preview:
            QTimer.singleShot(0, lambda: self.check(False))

    def check(self, manual=True):
        if self.closed or self.installing:
            return
        if self.service.check(manual):
            self.window.settings.updates.set_status(STATUS_CHECKING, True)

    def _visible(self, release):
        from gui.bridge import update
        if not release:
            return None
        remote = update.parse_version(release.get("version"))
        if remote is None:
            return None
        ignored = update.parse_version(self.ignored_version)
        if ignored is not None and remote <= ignored:
            return None
        return release

    def _checked(self, manual, release, error):
        if self.closed:
            return
        if error:
            self.window.settings.updates.set_status(STATUS_FAILED)
            if manual:
                self.alerts.warn("检查更新", error)
            return
        release = self._visible(release)
        if not release:
            self.release = None
            self.window.settings.updates.set_status(STATUS_LATEST)
            if manual:
                self.alerts.warn("检查更新", STATUS_LATEST, "information")
            return
        self.release = release
        self.window.settings.updates.set_status(STATUS_READY + release["version"])
        message = ("发现主仓库新版本 " + release["version"] + "。下载完成后将停止校园网认证，更新到当前目录并重启。")
        if not frozen():
            self.alerts.warn("检查更新", "发现主仓库新版本 " + release["version"] + "。源码运行仅支持检查；请使用打包版本安装更新。", "information")
            return
        if self.auto_install:
            self.accept()
            return
        self.alerts.deliver(Notice("发现新版本", message, "information", "confirm", "install-update"))

    def accept(self):
        if self.closed or self.installing or not self.release or not frozen():
            return
        if self.service.prepare(self.release):
            self.window.settings.updates.set_status(STATUS_PREPARING, True)

    def _prepared(self, plan_path, error):
        if self.closed:
            return
        if error:
            self.window.settings.updates.set_status(STATUS_FAILED)
            self.alerts.warn("检查更新", error)
            return
        self.installing = True
        try:
            self.begin_install(plan_path)
        except Exception as exc:
            self.installing = False
            trace.emit("update_handoff_failed", reason=type(exc).__name__)
            self.window.settings.updates.set_status(STATUS_FAILED)
            self.alerts.warn("检查更新", INSTALL_FAILED)
        else:
            self.window.settings.updates.set_status(STATUS_WAITING, True)

    def open_log(self):
        """Show the newest offered version first, then the running version."""
        if self.closed:
            return
        sections, seen = [], set()
        if self.release:
            version = str(self.release.get("version") or "")
            if version:
                sections.append(("新版本 V" + version, version))
                seen.add(version)
        running = str(theme.APP_VERSION)
        if running not in seen:
            sections.append(("当前版本 V" + running, running))
        self._log_sections = sections
        self._log_notes = {}
        self._log_pending = {version for _heading, version in sections}
        self.window.settings.update_log.set_busy(True)
        if not self._log_pending:
            self._render_log()
            return
        for _heading, version in sections:
            self.service.notes(version)

    def _notes_ready(self, version, notes):
        if self.closed or not self._log_pending:
            return
        self._log_notes[version] = notes
        self._log_pending.discard(version)
        if not self._log_pending:
            self._render_log()

    def _render_log(self):
        sections = self._log_sections
        self._log_pending = None
        self.window.settings.update_log.set_busy(False)
        self.window.settings.show_update_log(
            [(heading, self._log_notes.get(version, "")) for heading, version in sections])

    def report_result(self, payload):
        """Surface a previous run's update result through the unified failure wording."""
        if self.closed or not isinstance(payload, dict) or payload.get("ok"):
            return
        reason = str(payload.get("error") or "更新没有完成")
        self.alerts.warn("更新失败", RESULT_FAILED.format(reason=reason))

    def close(self):
        self.closed = True
        self.service.close()

    def shutdown_failed(self):
        self.installing = False
        self.window.settings.updates.set_status(STATUS_FAILED)
        self.alerts.warn("检查更新", SHUTDOWN_FAILED)
