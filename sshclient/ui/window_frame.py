"""窗口外框：给无边框窗口画一圈 1px 描边，并把四角修成 5px 圆角。

**为什么不能用 QSS 的 `border`。** 实测（400x300 无边框窗口，1/2/3px 三档，
`border_strong` 取样）：给 `QMainWindow` 加 `QMainWindow { border: Npx solid X }`
之后，**四条边的像素全是底色，命中 0 px**。机制是 `setCentralWidget` 把 central
widget 满铺在 `(0, 0, W, H)`，而应用级那条 `QWidget { background-color: root_bg }`
让 central 及其全部子控件都不透明 —— **边框被自己的 central 盖光**。
把 central 背景改透明后 3px 红边立刻出现，证明规则本身没写错，是被盖的。
换挂到 `QWidget#root`、挂到标题栏，都被各自的子控件再盖一层。
所以这里自绘。

**圆角同理，QSS 的 `border-radius` 也画不出来** —— 而且比描边更隐蔽：
窗口半透明之后它**仍然**是 0 px。原因是边缘那几个控件各自不透明地铺到
窗口边：标题栏浮层满宽（`setGeometry(0, 0, W, 38)`）、侧栏满高、状态栏在
central 之外。它们把 central 画出来的圆角四个角整个盖死。
实测四组（判据钉在四角像素的 alpha）：

    只加 QSS border-radius          四角 alpha=255（没圆）
    translucent + QSS radius       四角 alpha=255（还是没圆）
    translucent + 本文件的挖角浮层  四角 alpha=0，  边缘/中心 alpha=255

所以圆角也只能自绘：**窗口开 `WA_TranslucentBackground`，再由这个顶层浮层
把圆角之外挖成透明**（`CompositionMode_Clear` + 裁剪到"窗口矩形减去圆角矩形"）。

**为什么不用系统原生圆角。** Win11 的 `DWMWA_WINDOW_CORNER_PREFERENCE`
实测在本项目这种 `FramelessWindowHint` 上确实生效（角像素已是桌面色），
而且抗锯齿、保留投影 —— 但它**只提供 4px(ROUNDSMALL) 和 8px(ROUND) 两档，
没有 5px**。用户要的是 5px，所以走自绘。代价是窗口变 layered，
**丢掉 DWM 原生投影** —— 这是这条路的已知取舍。

**为什么不用 accent 描边。** `accent` 是**用户可自定义**的，而 `_CLAMP["light"]`
的上限 0.97 明确放行近白：实测把 accent 设成 `#ffffff` 时它与 `bg_base` 的
对比度塌到 1.083 —— 边框直接消失，且没有任何测试会红。`border_strong`
（浅 3.167 / 深 3.398）是唯一在两套主题、任意强调色下都稳的。

**不影响贴边分屏与边缘缩放。** `titlebar.hit_test` 是纯几何计算
（`RESIZE_MARGIN` + 窗口宽高 + 光标坐标），与绘制无关；实测加边框前后
11 个采样点的判定完全一致。

**最大化时圆角自动关闭。** 最大化窗口盖满屏幕，四角挖空只会露出壁纸 ——
Windows / macOS 两边都是这个惯例（最大化 = 方角）。判定放在绘制时读
`window().isMaximized()`，不用额外的事件管道。
"""
from __future__ import annotations

from PyQt6 import QtCore, QtGui, QtWidgets

from .design.v1.palette import Palette

#: 圆角半径，单位**物理像素**（故意不进 `_SCALE_PROPS`：跟着界面字号放大
#: 在 200% dpi 下会变成 10 物理像素的大圆）。
CORNER_RADIUS = 5


class WindowFrame(QtWidgets.QWidget):
    """浮在窗口最上层的圆角 + 1px 描边环。

    **只画四条边和四个角，不填中间。** 整块 `fillRect` 会把下面的界面全盖住 ——
    这是本实现唯一会翻车的地方，写错了就是「窗口里什么都没有」。

    1px 描边是**物理尺寸**：`_scale_metrics` 只缩放 `font-size` / `padding` /
    `margin` / `border-radius` / `min-*` 这几类，1px 描边**故意不进**
    `_SCALE_PROPS` —— 跟着字号放大的边框在 200% dpi 下会变成一条 4 物理像素的
    黑带。要「更醒目」应该加对比度，不是加粗。
    """

    def __init__(self, parent: QtWidgets.QWidget, palette: Palette,
                 *, border: bool = True):
        super().__init__(parent)
        self._color = palette.border_strong
        self._border = border
        # **必须不吃鼠标事件。** 否则这条浮层会把窗口最外一圈的点击、拖拽、
        # 贴边分屏手势全吃掉 —— 而那一圈正是 `RESIZE_MARGIN` 缩放带所在。
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        # **不进任何布局**（它是 central 的游离子控件，布局不认它，所以不占位）。
        # 注意别用 `setFixedWidth(0)` 之类去「让它不占位」——那会把
        # `setGeometry` 给的宽度夹成 0，整条环直接消失且不报错。

    # ------------------------------------------------------------------ 对外

    def apply_palette(self, palette: Palette) -> None:
        self._color = palette.border_strong
        self.update()

    def raise_over(self) -> None:
        """重新抬到最上层。**resize / 切页之后必须再调一次**，
        否则子控件一旦盖上来这条环就消失，而且没有任何报错。"""
        self.raise_()

    # ------------------------------------------------------------------ 绘制

    def _radius(self) -> int:
        """最大化时返回 0（方角）。在绘制时读，不额外接事件管道。"""
        win = self.window()
        if win is not None and win.isMaximized():
            return 0
        return CORNER_RADIUS

    def paintEvent(self, _event) -> None:  # noqa: N802
        w, h = self.width(), self.height()
        r = self._radius()
        p = QtGui.QPainter(self)
        if r > 0 and w > 2 * r and h > 2 * r:
            path = QtGui.QPainterPath()
            path.addRoundedRect(QtCore.QRectF(0.5, 0.5, w - 1, h - 1), r, r)
            # 「窗口矩形减去圆角矩形」= 四个角。挖掉它们，底下桌面就透出来了。
            outside = (QtGui.QRegion(self.rect())
                       - QtGui.QRegion(path.toFillPolygon().toPolygon()))
            if not outside.isEmpty():
                p.save()
                p.setClipRegion(outside)
                p.setCompositionMode(
                    QtGui.QPainter.CompositionMode.CompositionMode_Clear)
                p.fillRect(self.rect(), QtCore.Qt.GlobalColor.transparent)
                p.restore()
        if self._border:
            color = QtGui.QColor(self._color)
            # 圆角那一段要抗锯齿，否则像拿剪刀剪的。
            p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
            pen = QtGui.QPen(color)
            pen.setWidthF(1.0)
            p.setPen(pen)
            p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QtCore.QRectF(0.5, 0.5, w - 1, h - 1), r, r)
            # **四条直边再补一遍，关掉抗锯齿。** 1px 的线开了抗锯齿会跟底下
            # 混色：实测窗口下边读出 `#4a5368` 而不是 `#5d6781`（底下是更暗的
            # 状态栏），描边绕一圈会深一截，肉眼可见。所以直边用实心矩形
            # 覆盖 —— 圆角弧仍然是上面那一遍的抗锯齿结果，不受影响。
            p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(color)
            if 0 < r:
                p.drawRect(r, 0, w - 2 * r, 1)
                p.drawRect(r, h - 1, w - 2 * r, 1)
                p.drawRect(0, r, 1, h - 2 * r)
                p.drawRect(w - 1, r, 1, h - 2 * r)
            else:
                p.drawRect(0, 0, w, 1)
                p.drawRect(0, h - 1, w, 1)
                p.drawRect(0, 0, 1, h)
                p.drawRect(w - 1, 0, 1, h)
        p.end()


class _Backer(QtWidgets.QWidget):
    """铺在窗口**最底下**的不透明底色板。

    **为什么必须有它。** `WA_TranslucentBackground` 让 Qt 不再替顶层窗口
    自动铺底，于是「本来没被任何控件画过」的区域会**真的透出桌面**。
    实测（判据：弹框内部每一行的 alpha，QColorDialog 74x129 / 500x666 两例）：

        不垫底板   内部透明行 = 38..126（标题栏以下整片）
        垫了底板   内部透明行 = 无，四角仍是 0

    `setAutoFillBackground` / 改调色板 Window+Base / 追加一条更具体的
    `QDialog { background-color }` —— **四条全试过，一条都不管用**：
    半透明顶层窗口上 `QWidget { background-color }` 不负责铺自己那层底。
    38 那个起点正是 `TITLEBAR_H`，说明只有标题栏浮层画了底色。

    **必须待在最底层**（`lower()`），否则会盖住正文；又必须不吃鼠标事件，
    否则整个弹框点不动。
    """

    def __init__(self, parent: QtWidgets.QWidget, color: str):
        super().__init__(parent)
        self.setAttribute(
            QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._color = color

    def apply_palette(self, color: str) -> None:
        if color != self._color:
            self._color = color
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        """**必须自绘，不能靠 `autoFillBackground` / 调色板。**

        实测 `setAutoFillBackground(True)` + 调色板 `Window` 角色在
        半透明窗口的子控件上**根本不参与渲染**：`b.grab()` 单独抓这块底板
        是 `#1e2431`，可它合成进弹框之后那一块依旧是透明的 —— 实测把
        底板 `hide()` 掉和留着，弹框的透明行数**一模一样**（38..497，257 行）。
        也就是说那块控件是纯装饰品。自绘的 `fillRect` 没有这个中间层。
        """
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor(self._color))
        p.end()


class _FrameSync(QtCore.QObject):
    """把浮层的尺寸和层级钉在窗口上。

    主窗口自己有一条 `resizeEvent` 会同步 `WindowFrame`，但**弹窗没有**——
    而弹窗恰恰是这次要一起圆角的。放在事件过滤器里，主窗口和 6 个弹窗
    就共用同一条同步逻辑，漏一处的表现是「窗口一改大小圆角就错位」。
    """

    def __init__(self, window: QtWidgets.QWidget, frame: "WindowFrame",
                 backer: "_Backer"):
        super().__init__(window)
        self._window = window
        self._frame = frame
        self._back = backer
        window.installEventFilter(self)
        self.sync()

    def sync(self) -> None:
        w, f, b = self._window, self._frame, self._back
        geo = QtCore.QRect(0, 0, w.width(), w.height())
        if b.geometry() != geo:
            b.setGeometry(geo)
        if f.geometry() != geo:
            f.setGeometry(geo)
        # 底板永远在最底、角浮层永远在最上。顺序反了就是「正文被盖死」
        # 或者「圆角又被标题栏盖掉上面两个角」，而且都不报错。
        b.lower()
        b.update()
        f.raise_over()
        f.update()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self._window and event.type() in (
                QtCore.QEvent.Type.Resize,
                QtCore.QEvent.Type.WindowStateChange,
                QtCore.QEvent.Type.Show,
                QtCore.QEvent.Type.ChildAdded):
            self.sync()
        return False


def enable_rounded_corners(window: QtWidgets.QWidget, palette: Palette, *,
                           border: bool = True,
                           backer: str = "") -> "WindowFrame":
    """给一个 frameless 顶层窗口装上 5px 圆角，返回浮层（供换主题重绘用）。

    **必须在 `show()` 之前调。** `WA_TranslucentBackground` 是窗口创建期属性，
    show 之后再设会让原生窗口重建，肉眼看得见一次闪烁 —— 同
    `titlebar.frameless_enabled()` 那边的规矩。

    ``backer`` 是垫在窗口最底下的底色（见 :class:`_Backer` 的必要性）。
    传空则按「主窗口用 `bg_base`、弹窗用 `bg_dialog`」取。
    """
    window.setAttribute(
        QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
    color = backer or (palette.bg_base if border else palette.bg_dialog)
    backer_widget = _Backer(window, color)
    frame = WindowFrame(window, palette, border=border)
    # 事件过滤器挂在 window 上，局部变量由 Qt 的父子关系保活；显式存一份属性
    # 是为了让「已经装过了」这件事可查（弹窗可能被重复安装）。
    sync = _FrameSync(window, frame, backer_widget)
    window._mavisFrameSync = sync          # noqa: SLF001
    window._mavisFrame = frame             # noqa: SLF001
    window._mavisBacker = backer_widget    # noqa: SLF001
    return frame
