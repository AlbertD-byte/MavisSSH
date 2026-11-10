"""MavisSSH entry point.

Run with::

    .venv\\Scripts\\python.exe main.py

Copyright (C) 2026  Albert D

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

from PyQt6.QtCore import QCoreApplication, QLibraryInfo, Qt, QTranslator
from PyQt6.QtWidgets import QApplication, QMessageBox

from sshclient import __version__  # 版本号的唯一真相在包上
from sshclient.core import logs
from sshclient.core.fonts import ui_font
from sshclient.core.settings import AppSettings, SessionStore
from sshclient.ui import logo, msgbox
from sshclient.ui.design.v1.palette import get_palette
from sshclient.ui.main_window import MainWindow

log = logs.get_logger("mavisssh.main")


def palette_of_startup():
    """启动时按已存的主题取配色。

    读设置失败一律回退深色——图标取不到不该让整个程序起不来。
    """
    try:
        return get_palette(AppSettings.load().theme)
    except Exception:                              # noqa: BLE001
        return get_palette("dark")


def install_crash_guard() -> None:
    """Keep the app alive when an exception escapes a Qt slot.

    By default PyQt6 calls ``qFatal()`` for an unhandled exception raised in a
    slot or callback: the traceback is printed and the process aborts with
    STATUS_STACK_BUFFER_OVERRUN. There is no dialog and no chance to recover,
    so a single typo anywhere in a click handler makes the whole window
    vanish with no explanation.

    Installing ``sys.excepthook`` intercepts that path (verified in
    tests/_excepthook_probe.py: without it the process dies, with it the
    process survives and the hook is called). We log the full traceback and
    show a short message, and the guard around the handler keeps a failure
    while reporting from becoming a second, worse crash.
    """
    previous = sys.excepthook

    def hook(exc_type, exc, tb) -> None:
        try:
            text = "".join(traceback.format_exception(exc_type, exc, tb))
        except Exception:  # noqa: BLE001
            text = f"{exc_type.__name__}: {exc}"
        log.critical("未捕获异常\n%s", text, exc_info=False)
        try:
            # 走 msgbox 拿自绘标题栏。这里是崩溃兜底，**必须整段包在
            # try 里**（下面就是）：万一它自己再抛，不能递归回自己。
            # 配色取不到就回退到深色，不依赖主窗口——崩溃时主窗口可能
            # 早没了。
            box = msgbox._build(None, QMessageBox.Icon.Critical,
                                "发生错误", f"{exc_type.__name__}: {exc}",
                                informative=f"完整信息已写入 "
                                           f"{logs.LOG_DIRNAME}/{logs.LOG_FILENAME}",
                                detailed=text)
            box.exec()
        except Exception:  # noqa: BLE001
            try:
                previous(exc_type, exc, tb)
            except Exception:  # noqa: BLE001
                pass

    sys.excepthook = hook


def main() -> int:
    QCoreApplication.setOrganizationName("MavisSSH")
    QCoreApplication.setApplicationName("MavisSSH")
    QCoreApplication.setApplicationVersion(__version__)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, False)

    app = QApplication(sys.argv)
    # Qt 自带对话框（取色器、文件框）默认是**全英文**的。这里装一次中文翻译。
    #
    # **QTranslator 是 app 级的**：装上之后 QMessageBox 的标准按钮也跟着变
    # （&Yes/&No -> 是(&Y)/否(&N)），波及五个文件二十多处。装在这里而不是
    # 装在取色器上，作用域才是对的——我们要的是全应用中文，不是取色器中文。
    # 找不到翻译文件不算失败，只是界面保持英文。
    _tr = QTranslator(app)
    for _base in (QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath),
                  str(Path(__file__).resolve().parent)):
        if _tr.load("qtbase_zh_CN", _base):
            break
    else:
        _tr = None
    if _tr is not None:
        app.installTranslator(_tr)
    # 窗口 / 任务栏 / Alt-Tab 的图标。**必须在建任何窗口之前设**，否则已经建
    # 出来的窗口拿不到（Qt 只在建的时候取一次）。深浅两套主题的底色差一档，
    # 这里先按当前设置给一版，换主题时 MainWindow.apply_theme 会再刷一次。
    app.setWindowIcon(logo.window_icon(palette_of_startup()))
    # 日志跟着当前工作目录走，所以第一行就把实际路径写清楚——从别的目录
    # 启动会写到那个目录下面。
    log_dir = logs.setup()
    install_crash_guard()

    log.info("MavisSSH 启动  cwd=%s  日志目录=%s",
             Path.cwd(), log_dir)

    # UI font is proportional; the terminal picks its own monospace face via
    # fonts.mono_font() using AppSettings.term_font_*. Using Consolas app-wide
    # made every label and button look like a code editor.
    settings = AppSettings.load()
    app.setFont(ui_font(settings.ui_font_size, settings.ui_font_family))
    # Deliberately no settings.save() here. It looked harmless -- rewriting the
    # file with the values just read from it -- but load() answers "all
    # defaults" for anything it cannot parse, so this line turned a read
    # failure into permanent data loss. Nothing in main() mutates the
    # settings, so there is nothing to write back.

    store = SessionStore()
    log.info("已载入 %d 条会话", len(store.all))
    win = MainWindow(settings=settings, store=store)
    win.show()
    code = app.exec()
    log.info("MavisSSH 退出  code=%d", code)
    return code


if __name__ == "__main__":
    sys.exit(main())
