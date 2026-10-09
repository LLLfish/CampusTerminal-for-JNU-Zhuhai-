# SPDX-License-Identifier: GPL-3.0-or-later
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QPainter
from PyQt5.QtWidgets import QLabel, QPushButton, QWidget

from gui import theme as T
from gui.widgets.paint import prepare, stroke_round


class UpdateBlock(QWidget):
    check_requested = pyqtSignal()

    def __init__(self, scale, family, parent=None):
        super().__init__(parent)
        self.scale = scale
        x, y, w, h, _r = T.SET_UPDATE_BOX
        self.setGeometry(round(x * scale), round(y * scale), round(w * scale), round(h * scale))
        self.title = QLabel("检查更新", self)
        self.title.setGeometry(round(52 * scale), round(36 * scale), round((w - 104) * scale), round(88 * scale))
        self.title.setFont(T.ui_font(70.24, scale, family))
        self.title.setStyleSheet("color: #c7c7c7; background: transparent;")
        self.button = QPushButton("检查更新", self)
        self.button.setGeometry(round(52 * scale), round(157 * scale), round((w - 104) * scale), round(120 * scale))
        self.button.setFont(T.ui_font(60, scale, family))
        self.button.setCursor(Qt.PointingHandCursor)
        self.button.setAccessibleName("检查更新")
        self.button.setStyleSheet("QPushButton { color: #c7c7c7; background: #242424; border: 1px solid #444; border-radius: 5px; } QPushButton:hover { background: #333; } QPushButton:disabled { color: #818181; }")
        self.button.setToolTip("检查主仓库的最新正式版本；安装前会提示，更新到当前目录。")
        self.button.clicked.connect(self.check_requested)

    def set_status(self, text="检查更新", busy=False):
        self.button.setText(text)
        self.button.setToolTip(text)
        self.button.setEnabled(not busy)

    def paintEvent(self, _event):
        painter = QPainter(self)
        prepare(painter)
        stroke_round(painter, (0, 0, *T.SET_UPDATE_BOX[2:]), self.scale, T.C_LINE, 4.17)
