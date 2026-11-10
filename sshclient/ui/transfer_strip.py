"""传输状态条：SFTP 上传 / 下载的进度与结果。

刻意**不用** `QProgressDialog`。那是个模态顶层窗：弹出来会抢焦点、盖住文件
列表、而一次后台传输本来就不该打断用户 —— 观感上像是从上个时代掉进来的。
改成贴在面板底部的一条状态卡：常驻可见、期间照样能操作，完成或失败在原地
反馈之后自己退场。

三处刻意的选择：

**自绘进度条而不是 `QProgressBar`。** 要的是 5px 的圆角胶囊 + 渐变填充 +
宽度过渡；QSS 只能给出方角块，而 `::chunk` 在深浅两套主题下还要各写一遍
圆角和渐变。自绘两个 `drawRoundedRect` 就够了，也不用去和 QSS 抢样式表。

**百分比用等宽数字。** 百分比每几百毫秒跳一次，比例字宽下整行会左右抖。
界面字体里数字本来就是等宽的（0-9 等宽），所以只要保证标签不换行、不被
拉伸即可，不需要额外换字体。

**首字节之前是不定态。** 点上传到 paramiko 第一次回调之间可能隔着好几秒
（连远端、开文件、握手），一个卡在 0% 的确定态进度条看起来像死了。所以
还没收到第一个真实进度时，条子走一条来回滑的亮带。
"""
from __future__ import annotations

from PyQt6 import QtCore, QtGui, QtWidgets

from . import icons
from .design.v1.palette import Palette, get_palette

#: 成功后退场前的停留时间。够看清"完成了"，又不至于一直占着底部。
SUCCESS_LINGER_MS = 1800
#: 不定态亮带走完一趟的时长。
INDETERMINATE_MS = 1100

_UNITS = ("B", "KB", "MB", "GB", "TB")


def format_bytes(n: float) -> str:
    """1234 -> "1.2 KB"；字节数为 0 时给 "0 B" 而不是 "0.0 B"。"""
    n = float(max(0.0, n))
    if n < 1024:
        return "%d B" % n
    for u in _UNITS[1:]:
        n /= 1024.0
        if n < 1024 or u == _UNITS[-1]:
            return "%.1f %s" % (n, u)
    return "%.1f %s" % (n, _UNITS[-1])


def format_speed(bytes_per_sec: float) -> str:
    if bytes_per_sec <= 0:
        return ""
    return format_bytes(bytes_per_sec) + "/s"


class SlimBar(QtWidgets.QWidget):
    """5px 圆角胶囊进度条，自绘。

    三种画法：**不定态**（亮带来回滑）、**确定态**（按比例填充）、**收尾**
    （整条铺满 success 色）。宽度变化走 180ms 过渡，否则一格一格地跳。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(5)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                           QtWidgets.QSizePolicy.Policy.Fixed)
        self._value = 0.0          # 0..1，确定态的目标值
        self._drawn = 0.0          # 0..1，当前画出来的值（过渡中）
        self._indeterminate = False
        self._phase = 0.0
        self._track = "#252a35"
        self._c0 = "#4c8dff"
        self._c1 = "#6ba1ff"
        self._solid = False        # True 时不画渐变，整条一个色（成功/失败）

        self._phase_timer = QtCore.QTimer(self)
        self._phase_timer.setInterval(16)
        self._phase_timer.timeout.connect(self._advance_phase)

        self._anim = QtCore.QVariantAnimation(self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)

    # ------------------------------------------------------------ 外观

    def apply_palette(self, p: Palette) -> None:
        # **原来这里是 `p.bg_base if p.name == "dark" else p.bg_elevated`。**
        # 那个按主题名分叉的写法有个硬伤：传输条底下压的就是 `bg_base`
        # （SftpPanel 是普通 QWidget，命中 qss.py 那条兜底
        # `QWidget { background-color: root_bg }`），于是深色下轨道色
        # 和底色**完全相同** —— 实测对比度 1.000，轨道根本画不出来，
        # 界面上只剩一条悬空的填充色。浅色分叉用的 `bg_elevated` 才是对的：
        # 轨道 vs 底 1.046、轨道 vs 填充 4.562，两头都分得开。
        # 按主题名分叉还意味着第三套主题会静默掉回深色那一支。
        self._track = p.bg_elevated
        self._c0 = p.accent
        self._c1 = p.accent_hover
        self.update()

    def set_tone(self, c0: str, c1: str, solid: bool = False) -> None:
        self._c0, self._c1, self._solid = c0, c1, solid
        self.update()

    # ------------------------------------------------------------ 状态

    def start(self) -> None:
        """进入传输态：清零并切到不定态，直到第一个真实进度到达。"""
        self._anim.stop()
        self._value = self._drawn = 0.0
        self._solid = False
        self._indeterminate = True
        self._phase = 0.0
        self._phase_timer.start()
        self.update()

    def set_value(self, v: float) -> None:
        """确定态进度（0..1）。第一次调用会自动关掉不定态。"""
        v = max(0.0, min(1.0, float(v)))
        self._value = v
        if self._indeterminate:
            self._indeterminate = False
            self._phase_timer.stop()
        self._anim.setStartValue(self._drawn)
        self._anim.setEndValue(v)
        self._anim.start()
        self.update()

    def finish(self, color: str) -> None:
        """铺满并换成结果色。"""
        self._anim.stop()
        self._indeterminate = False
        self._phase_timer.stop()
        self._value = self._drawn = 1.0
        self.set_tone(color, color, solid=True)
        self.update()

    @property
    def value(self) -> float:
        """当前**目标**值，不是动画中间值 —— 测试要断言的是它。"""
        return self._value

    @property
    def indeterminate(self) -> bool:
        return self._indeterminate

    # ------------------------------------------------------------ 内部

    def _on_anim(self, val) -> None:
        self._drawn = float(val)
        self.update()

    def _advance_phase(self) -> None:
        self._phase = (self._phase + 16.0 / INDETERMINATE_MS) % 1.0
        self.update()

    def paintEvent(self, _ev) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setPen(QtCore.Qt.PenStyle.NoPen)

        r = self.height() / 2.0
        p.setBrush(QtGui.QColor(self._track))
        # PyQt6 没有 (x, y, w, h, ...) 这条 float 重载，只能给 QRectF。
        p.drawRoundedRect(QtCore.QRectF(0.0, 0.0, float(self.width()),
                                        float(self.height())), r, r)

        if self._indeterminate:
            self._paint_band(p, r)
        else:
            self._paint_fill(p, r)
        p.end()

    def _paint_band(self, p: QtGui.QPainter, r: float) -> None:
        """来回滑的一条亮带。宽 32%，两头用渐变淡出。"""
        w = self.width()
        band = w * 0.32
        x = (w + band) * self._phase - band
        grad = QtGui.QLinearGradient(x, 0, x + band, 0)
        head = QtGui.QColor(self._c0)
        tail = QtGui.QColor(self._c0)
        head.setAlpha(0)
        tail.setAlpha(0)
        grad.setColorAt(0.0, tail)
        grad.setColorAt(0.5, QtGui.QColor(self._c1))
        grad.setColorAt(1.0, head)
        p.setBrush(QtGui.QBrush(grad))
        p.drawRoundedRect(QtCore.QRectF(x, 0.0, band, float(self.height())), r, r)

    def _paint_fill(self, p: QtGui.QPainter, r: float) -> None:
        w = self.width()
        fw = w * self._drawn
        if fw <= 0.5:
            return
        # 渐变要横跨整个控件，这样条子长到全满时颜色分布不会随进度漂移。
        if self._solid:
            p.setBrush(QtGui.QColor(self._c0))
        else:
            grad = QtGui.QLinearGradient(0, 0, w, 0)
            grad.setColorAt(0.0, QtGui.QColor(self._c0))
            grad.setColorAt(1.0, QtGui.QColor(self._c1))
            p.setBrush(QtGui.QBrush(grad))
        # 裁到实际进度：画一个整高的圆角矩形再裁掉右边的多余部分，
        # 低进度时就是一小段方头——诚实，而且比露一个圆点好看。
        p.save()
        p.setClipRect(QtCore.QRectF(0.0, 0.0, fw, float(self.height())))
        p.drawRoundedRect(QtCore.QRectF(0.0, 0.0, max(fw, r * 2.0),
                                        float(self.height())), r, r)
        p.restore()


class TransferStrip(QtWidgets.QFrame):
    """贴在 SFTP 面板底部的一条状态卡。

    空闲时整个隐藏，所以不占地方；传输时出现，界面上半部分照常可用。
    成功后短暂停留再自己退场；失败则**停在那里等人处理**——一次失败没人
    看见等于没报，而且不该用模态框把它变成一次打断。
    """

    cancelRequested = QtCore.pyqtSignal()
    dismissed = QtCore.pyqtSignal()

    IDLE, RUNNING, SUCCESS, ERROR, CANCELLED = (
        "idle", "running", "success", "error", "cancelled")

    def __init__(self, palette: Palette | None = None, parent=None):
        super().__init__(parent)
        self.setProperty("role", "transferStrip")
        # palette 允许为 None（独立构造/测试），于是每一处读 token 的地方都得
        # 兜住。__init__ 和 _recolor 都兜了，begin() 却直接摸
        # self._palette.accent —— 评审实跑 `TransferController(None).begin()`
        # 抛 AttributeError: 'NoneType' object has no attribute 'accent'。
        # 兜底统一放这里，别在每个调用点各写一遍。
        self._palette = palette or get_palette("dark")
        self._state = self.IDLE
        self._linger: QtCore.QTimer | None = None

        self.glyph = QtWidgets.QLabel()
        self.glyph.setFixedSize(20, 20)
        self.glyph.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        self.title = QtWidgets.QLabel()
        self.title.setProperty("role", "transferTitle")

        self.detail = QtWidgets.QLabel()
        self.detail.setProperty("role", "transferDetail")

        text_col = QtWidgets.QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(1)
        text_col.addWidget(self.title)
        text_col.addWidget(self.detail)

        self.action = QtWidgets.QPushButton("取消")
        self.action.setProperty("variant", "ghost")
        self.action.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.action.clicked.connect(self._on_action)

        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(14, 10, 10, 8)
        row.setSpacing(10)
        row.addWidget(self.glyph, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        row.addLayout(text_col, 1)
        row.addWidget(self.action, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)

        self.bar = SlimBar()
        bar_row = QtWidgets.QHBoxLayout()
        bar_row.setContentsMargins(14, 0, 14, 12)
        bar_row.addWidget(self.bar)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(row)
        lay.addLayout(bar_row)

        self.setVisible(False)
        if palette is not None:
            self.apply_palette(palette)

    # ------------------------------------------------------------ 外观

    def apply_palette(self, p: Palette) -> None:
        self._palette = p
        self.bar.apply_palette(p)
        self._recolor()

    def _recolor(self) -> None:
        p = self._palette
        if p is None:
            return
        tone = {
            self.RUNNING: p.accent,
            self.SUCCESS: p.success,
            self.ERROR: p.danger,
            self.CANCELLED: p.text_muted,
        }.get(self._state)
        if tone is None:
            self.glyph.clear()
            self.setProperty("tone", "")
            return
        if self._state == self.SUCCESS:
            name = "check"
        elif self._state == self.CANCELLED:
            # 「已取消」用圆圈斜杠（ban），不是 ✕：✕ 说的是"点我关掉"这个动作，
            # 而这里要说的是一个**已经发生的状态**。而且这条用的是灰不是红 ——
            # 取消是用户的正常选择，不是出错。
            name = "ban"
        else:
            name = getattr(self, "_direction", "upload")
        self.glyph.setPixmap(icons.icon_pixmap(name, tone, 16))
        self.setProperty("tone", self._state)
        self.style().unpolish(self)
        self.style().polish(self)

    # ------------------------------------------------------------ 状态

    @property
    def state(self) -> str:
        return self._state

    def begin(self, direction: str, title: str, detail: str = "") -> None:
        """开始一次传输。*direction* 是 ``upload`` 或 ``download``。"""
        self._cancel_linger()
        self._direction = direction
        self._state = self.RUNNING
        self.title.setText(title)
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        self.action.setText("取消")
        self.action.setToolTip("停止后续文件（已发出的那一个会传完）")
        self.action.setVisible(True)
        self.bar.set_tone(self._palette.accent, self._palette.accent_hover)
        self.bar.start()
        self.setVisible(True)
        self._recolor()

    def advance(self, pct: int | None, detail: str = "") -> None:
        """确定态进度。*detail* 是"1.2 MB / 4.0 MB · 3.4 MB/s"这类。

        ``pct=None`` 表示**总量未知**，此时退回不定态。不能用 0 顶替：那画出来
        是一个卡在 0% 的确定态条，看起来像死了；也不能拿上次那个百分比顶着，
        那是撒谎。
        """
        if self._state != self.RUNNING:
            return
        if pct is None:
            self.bar.start()
        else:
            self.bar.set_value(pct / 100.0)
        if detail:
            self.detail.setText(detail)
            self.detail.setVisible(True)

    def succeed(self, text: str = "完成", detail: str = "") -> None:
        p = self._palette
        self._state = self.SUCCESS
        self.title.setText(text)
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        self.action.setText("关闭")
        self.action.setVisible(True)
        self.bar.finish(p.success)
        self._recolor()
        # 完成是"已经发生过的事实"，停一下让人看见，然后自己走。
        self._linger = QtCore.QTimer(self)
        self._linger.setSingleShot(True)
        self._linger.timeout.connect(self.hide_and_notify)
        self._linger.start(SUCCESS_LINGER_MS)

    def fail(self, title: str, detail: str = "") -> None:
        """失败**不自动退场**：没人看见的报错等于没报。"""
        p = self._palette
        self._cancel_linger()
        self._state = self.ERROR
        self.title.setText(title)
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        self.action.setText("关闭")
        self.action.setVisible(True)
        self.bar.finish(p.danger)
        self._recolor()

    def cancel(self, title: str = "已取消", detail: str = "") -> None:
        """用户主动停的。

        和"完成"必须分开：没传完就是没传完，报"上传完成"是撒谎。也不做成
        失败那种红色——取消是用户的正常选择,不是错误,不该像出事了。
        """
        p = self._palette
        self._cancel_linger()
        self._state = self.CANCELLED
        self.title.setText(title)
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        self.action.setText("关闭")
        self.action.setVisible(True)
        self.bar.finish(p.text_muted)
        self._recolor()
        self._linger = QtCore.QTimer(self)
        self._linger.setSingleShot(True)
        self._linger.timeout.connect(self.hide_and_notify)
        self._linger.start(SUCCESS_LINGER_MS)

    def hide_and_notify(self) -> None:
        self._cancel_linger()
        self.setVisible(False)
        self._state = self.IDLE
        self.dismissed.emit()

    def _cancel_linger(self) -> None:
        if self._linger is not None:
            self._linger.stop()
            self._linger = None

    def _on_action(self) -> None:
        if self._state == self.RUNNING:
            self.cancelRequested.emit()
        else:
            self.hide_and_notify()

    def setTextColor(self, color: str) -> None:  # pragma: no cover - Qt 兼容
        self.title.setStyleSheet(f"color: {color};")
