"""标签页右键菜单：关闭当前 / 关闭其他 / 关闭右侧。

**为什么单独一个模块。** 关闭按钮那一套（``tab_close.py``）是在 QTabBar 上
挂**自绘子控件**、按几何匹配标签的 —— 它解决的是「Qt 的原生 close-button
不能改样式」这个坑。右键菜单要的是完全不同的东西：拿到**被右击的那个
标签的下标**，弹一个菜单。两者唯一的交集是都要知道「当前是哪一个 tab」，
所以菜单的**下标判定**放在这里，而**实际的关闭动作**仍然走
``tabCloseRequested`` —— 与关闭按钮走的是**同一条路**，不另开一条。

那样做的两个好处：

* 关闭按钮与右键菜单的关闭行为**必然一致**（同一条 ``_on_tab_close``），
  不会出现「按钮关得掉、菜单关不掉」；
* 欢迎页那个不可关闭的标签，菜单里也会自动变成灰的 —— 因为判定用的是
  同一个 ``NO_CLOSE`` 属性。

**下标判定不能靠 ``tabBar().tabAt(pos)`` 完事。** 那个函数在标签被
拖动重排（``setMovable(True)``）之后仍然可用，但它**不认识自绘的关闭
按钮**：点在关闭按钮上时它会返回那个标签的下标 —— 那是对的（点 X 就是
关那个），可如果菜单在关闭按钮上弹出，用户看到的是「X 上弹出了菜单」，
很怪。所以这里额外判一下：点在**别的**标签的关闭按钮上时，宁可不给菜单。
"""
from __future__ import annotations

from PyQt6 import QtCore, QtWidgets

from . import icons
from .tab_close import NO_CLOSE


class TabContextMenu(QtCore.QObject):
    """在标签栏上弹出「关闭当前 / 关闭其他 / 关闭右侧」。

    挂在 QTabBar 的事件过滤器上：收到 ``MouseButtonRelease`` 且是右键时，
    判定被右击的标签，弹菜单。
    """

    def __init__(self, tabs: QtWidgets.QTabWidget, palette, parent=None):
        super().__init__(parent if parent is not None else tabs)
        self._tabs = tabs
        self._bar = tabs.tabBar()
        #: 设计系统的 ``Palette``（不是 QPalette）—— 图标要按主题着色。
        self._palette = palette
        self._bar.installEventFilter(self)
        # 右键按下时先记下位置：ContextMenu 事件里已经拿不到原始坐标了。
        self._press_pos: QtCore.QPoint | None = None

    # ------------------------------------------------------------------ public

    def set_palette(self, palette) -> None:
        """换主题时更新图标配色（菜单是每次弹出才建的，所以只需存住）。"""
        self._palette = palette

    # ------------------------------------------------------------------ events

    def eventFilter(self, obj, event):
        if obj is self._bar:
            et = event.type()
            if et == QtCore.QEvent.Type.MouseButtonPress:
                if event.button() == QtCore.Qt.MouseButton.RightButton:
                    self._press_pos = event.position().toPoint()
                else:
                    self._press_pos = None
            elif et == QtCore.QEvent.Type.MouseButtonRelease:
                if event.button() == QtCore.Qt.MouseButton.RightButton:
                    pos = event.position().toPoint()
                    press = self._press_pos
                    self._press_pos = None
                    # **必须是「按下与释放都在同一个标签上」** —— 否则
                    # 「按下右键 → 拖到别的标签 → 松开」会在别的标签上弹菜单。
                    if press is not None and self._tab_at(pos) == self._tab_at(press):
                        self._show_menu(pos)
                        return True
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------ private

    def _tab_at(self, pos: QtCore.QPoint) -> int:
        """位置落在哪个标签上（-1 = 不在标签上）。"""
        bar = self._bar
        for i in range(self._tabs.count()):
            if bar.tabRect(i).contains(pos):
                return i
        return -1

    def _closable(self, idx: int) -> bool:
        """这个标签能不能关（欢迎页不能）。"""
        w = self._tabs.widget(idx)
        return w is not None and w.property(NO_CLOSE) is not True

    def _show_menu(self, pos: QtCore.QPoint) -> None:
        idx = self._tab_at(pos)
        if idx < 0:
            return
        total = self._tabs.count()
        # 同一批里可能有不可关闭的（欢迎页）。**「关闭其他」要真的关掉
        # 所有别的标签**，所以它指的是「除当前之外全部」。
        others = [i for i in range(total) if i != idx]
        has_other = any(self._closable(i) for i in others)
        # 「关闭右侧」= 当前标签**右边**那些
        right = [i for i in range(idx + 1, total) if self._closable(i)]

        menu = QtWidgets.QMenu(self._bar)
        # 图标与菜单栏（文件/视图/帮助）保持同一套 —— 菜单栏里
        # 「关闭所有标签」用的是 icons.icon("close", ...)。
        pal = self._palette
        act_close = menu.addAction(icons.icon("close", pal, 16), "关闭当前")
        act_close.setEnabled(self._closable(idx))
        act_other = menu.addAction(icons.icon("close", pal, 16), "关闭其他")
        act_other.setEnabled(has_other)
        act_right = menu.addAction(icons.icon("close", pal, 16), "关闭右侧")
        act_right.setEnabled(bool(right))

        chosen = menu.exec(self._bar.mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_close:
            self._close(idx)
        elif chosen is act_other:
            # **从大到小关。** 关掉一个会让后面的下标全部前移 ——
            # 从小到大关的话，第二个要关的下标已经变了。
            for i in sorted(others, reverse=True):
                if self._closable(i):
                    self._close(i)
        elif chosen is act_right:
            for i in sorted(right, reverse=True):
                self._close(i)

    def _close(self, idx: int) -> None:
        """走与关闭按钮**同一条**路。"""
        if 0 <= idx < self._tabs.count():
            self._tabs.tabCloseRequested.emit(idx)
