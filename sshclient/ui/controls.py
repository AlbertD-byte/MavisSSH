"""跨模块共用的小控件工厂：状态标签、图标按钮、竖分隔线。

原先都散在 main_window.py 里，但侧边栏行、欢迎页、会话工具条、主窗口 chrome
都要用。单独放一个模块，是为了让 `sidebar` 和 `main_window` 引到同一份，
而不必互相 import——那会成环。
"""
from __future__ import annotations

from PyQt6 import QtCore, QtWidgets

from . import icons
from .design.v1.palette import Palette


def _label(text: str, role: str = "") -> QtWidgets.QLabel:
    lab = QtWidgets.QLabel(text)
    if role:
        lab.setProperty("role", role)
    return lab


def _icon_button(name: str, tooltip: str, palette: Palette, size: int = 18) -> QtWidgets.QToolButton:
    btn = QtWidgets.QToolButton()
    btn.setProperty("variant", "icon")
    # 把图标名挂在按钮上。换主题时靠它重画——**不要**在 apply_palette 里再手抄
    # 一份图标名列表，抄漏一处，Ctrl+T 就会把那个按钮悄悄打回旧图标，
    # 而界面上看不出任何异常（这个坑真的踩过一次）。
    btn._icon_name = name
    btn._icon_size = size
    btn.setIcon(icons.icon(name, palette, size))
    btn.setIconSize(QtCore.QSize(size, size))
    btn.setToolTip(tooltip)
    btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
    btn.setFixedSize(30, 30)
    return btn


def _retheme_icon_action(action, palette, color: str = "") -> None:
    """按新配色重画 QAction 的图标。

    QAction 的图标是构造时定下的，``_retheme_icon_button`` 够不到它（它只处理
    QAbstractButton）。图标名由 action 自己用 ``_icon_name`` 记住，**这里不
    手抄一份名表**——手抄过一次，换主题就会有一排图标悄悄变回旧色。

    它和 ``_retheme_icon_button`` 住在一起是有道理的：重染色的事只在这一处
    做，换主题的路径里就不该再出现 ``setIcon(icons.icon(...))``。
    """
    name = getattr(action, "_icon_name", None)
    if not name:
        return
    from . import icons
    action.setIcon(icons.icon(name, palette, 16, color or palette.text_dim))


def _retheme_icon_button(btn: QtWidgets.QToolButton, palette: Palette,
                         color: str | None = None) -> None:
    """按按钮自己记住的图标名与尺寸重新染色。

    任何要换主题的图标按钮都该走这里，**不要**在 apply_palette 里手抄一份
    名字表 —— 抄漏一处，Ctrl+T 就会把那个按钮悄悄打回旧图标，界面上看不出
    任何异常。这个坑真踩过一次（sftp_panel 漏了 mkdir 和 up 两处）。
    """
    name = getattr(btn, "_icon_name", None)
    if not name:
        return
    size = getattr(btn, "_icon_size", 18)
    btn._icon_color = color          # 存住，供下一次换主题复现
    btn.setIcon(icons.icon(name, palette, size, color) if color
                else icons.icon(name, palette, size))
    btn.setIconSize(QtCore.QSize(size, size))


def _set_icon(btn: QtWidgets.QToolButton, name: str, palette: Palette,
              size: int = 18, color: str | None = None) -> None:
    """给按钮装图标，并记住名字/尺寸/颜色，供之后换主题复用。"""
    btn._icon_name = name
    btn._icon_size = size
    _retheme_icon_button(btn, palette, color)


# **原 `_sep()` 已删。** 它是一条原生 `QFrame` VLine，**没有设任何颜色**，
# 于是被应用级 `QWidget { color: text }` 兜住 —— 主窗口实测像素是
# `#787b82`（`text` 与底色各半的混色，(0xe4+0x0d)/2=0x78），对比度 4.52，
# 是全界面**最亮**的线条，而它两侧都是同一个 `#0d0f14`，**零信息量**。
#
# 关键在于它**不吃 `border` token**：改 palette 对它完全无效。所以「统一把
# 边框调淡」这条路根本碰不到用户眼里最扎眼的那几条。
#
# 删掉之后那一格改用布局间距，分组照样读得出来（现代 UI 的做法：靠留白
# 分组，不靠画线）。**没有留一个「不再画线的 `_sep`」** —— 同名函数
# 不再干它名字说的那件事，等于给后来人装了一个静默失效的开关。
