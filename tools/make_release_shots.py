"""生成 GitHub README 用的发布截图。

产出（`docs/img/`）：

| 文件 | 内容 |
|---|---|
| `screenshot-main-dark.png`   | 主界面（深色主题），含会话列表与欢迎页 |
| `screenshot-main-light.png`  | 主界面（浅色主题） |
| `screenshot-terminal.png`    | 终端标签页（带一段模拟 shell 输出） |
| `screenshot-connect.png`     | 新建连接对话框 |
| `screenshot-settings.png`    | 设置对话框 |

**跑法**（Windows / PowerShell）::

    .venv\\Scripts\\python.exe tools\\make_release_shots.py

必须在真实的 `windows` 平台插件下跑 —— `offscreen` 插件不加载系统字体，
截出来是一片空白。脚本因此**显式拒绝**在 `offscreen` 下运行，而不是
悄悄产出一堆空白图。

脚本里的主机名 / IP 全部是 `example.com` 域与 RFC 5737 文档保留段，
不含任何真实环境信息 —— 截图是要公开挂到 GitHub 上的。
"""
from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# **必须在建 QApplication 之前定下来。** `tests/fonts.py` 的 `boot()` 会
# `setdefault` 成 offscreen —— 那个默认值在这里是错的（见模块 docstring）。
os.environ["QT_QPA_PLATFORM"] = "windows"

from tests.fonts import boot  # noqa: E402  (import 前必须先定平台插件)

app = boot([])

from PyQt6.QtWidgets import QVBoxLayout, QWidget  # noqa: E402

from sshclient.core import settings  # noqa: E402
from sshclient.core.settings import SavedSession  # noqa: E402
from sshclient.ui import connect_dialog, main_window, settings_dialog  # noqa: E402
from sshclient.ui.design.v1 import palette  # noqa: E402

OUT = ROOT / "docs" / "img"
OUT.mkdir(parents=True, exist_ok=True)

#: 演示用的会话。主机名走 RFC 2606 保留的 `example.com`，IP 走 RFC 5737
#: 文档保留段 —— 这两段都是"永远不会被解析到真实主机"的，公开截图安全。
_DEMO_SESSIONS = [
    SavedSession(name="生产 Web-01", host="web-01.example.com", port=22,
                 username="deploy", auth_method="key",
                 key_filename=r"C:\Users\deploy\.ssh\id_ed25519"),
    SavedSession(name="测试环境", host="203.0.113.20", port=22,
                 username="deploy", auth_method="password", password="x"),
    SavedSession(name="数据库", host="db.example.com", port=22,
                 username="ops", auth_method="key",
                 key_filename=r"C:\Users\deploy\.ssh\id_ed25519"),
]


def _shoot(widget, name: str, w: int, h: int, theme: str) -> None:
    """把 *widget* 渲染进一个真实宿主窗口后截图。

    直接 grab 一个从未 show 过的顶层 widget 会得到**空白图**（offscreen
    下 Qt 不给它跑真正的 backing store）—— 这会让"到底渲染出来没有"这类
    断言变成恒真。套一层已映射的宿主窗口才能逼出一次真实 paint。
    """
    p = palette.get_palette(theme)
    host = QWidget()
    host.setStyleSheet(f"background-color: {p.bg_base};")
    lay = QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(widget)
    host.resize(w, h)
    host.show()
    widget.show()
    for _ in range(6):
        app.processEvents()
    widget.repaint()
    host.repaint()
    app.processEvents()
    path = OUT / name
    host.grab().toImage().save(str(path))
    print(f"saved {path.relative_to(ROOT)}  ({path.stat().st_size} B)")
    _HOSTS.append(host)


_HOSTS: list = []


def _make_store(tmp: pathlib.Path, theme: str):
    store = settings.SessionStore(tmp / f"sessions_{theme}.json")
    for s in _DEMO_SESSIONS:
        store.upsert(s)
    return store


def _make_settings(store, theme: str):
    st = settings.AppSettings()
    st._path = store.path.parent / "settings.json"
    st.theme = theme
    return st


def main() -> int:
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp())

    # ---- 1. 主界面（深色 / 浅色） -------------------------------------
    for theme in ("dark", "light"):
        store = _make_store(tmp, theme)
        st = _make_settings(store, theme)
        win = main_window.MainWindow(st, store)
        _shoot(win, f"screenshot-main-{theme}.png", 1280, 820, theme)

    # ---- 2. 终端标签页 ------------------------------------------------
    # 用一段**手写的模拟输出**，不连任何真实服务器 —— 截图要公开。
    store = _make_store(tmp, "dark")
    st = _make_settings(store, "dark")
    win = main_window.MainWindow(st, store)
    tab = main_window.SessionTab(win)
    p = palette.get_palette("dark")
    tab.apply_palette(p)
    tab.terminal.resize(980, 460)
    tab.terminal.write(
        b"\x1b[1;32mdeploy@web-01\x1b[0m:\x1b[1;34m~/srv/app\x1b[0m$ ls -la\r\n"
        b"drwxr-xr-x  12 deploy deploy 4096 Aug 21 10:22 src\r\n"
        b"-rw-r--r--   1 deploy deploy  812 Aug 21 10:20 deploy.sh\r\n"
        b"-rw-r--r--   1 deploy deploy 2048 Aug 21 09:58 config.yml\r\n"
        b"\r\n\x1b[1;32mdeploy@web-01\x1b[0m:\x1b[1;34m~/srv/app\x1b[0m$ \x1b[36m"
        b"docker compose ps\x1b[0m\r\n"
        b"NAME            STATUS          PORTS\r\n"
        b"api             Up 3 hours      0.0.0.0:8080->8080/tcp\r\n"
        b"postgres        Up 3 hours      5432/tcp\r\n"
        b"\r\n\x1b[1;32mdeploy@web-01\x1b[0m:\x1b[1;34m~/srv/app\x1b[0m$ ")
    _shoot(tab, "screenshot-terminal.png", 1000, 560, "dark")

    # ---- 3. 新建连接对话框 --------------------------------------------
    store = _make_store(tmp, "light")
    st = _make_settings(store, "light")
    win = main_window.MainWindow(st, store)
    dlg = connect_dialog.ConnectDialog(win, palette=palette.get_palette("light"))
    dlg.name_edit.setText("生产 Web-01")
    dlg.host_edit.setText("web-01.example.com")
    dlg.user_edit.setText("deploy")
    dlg.auth_tabs.setCurrentIndex(1)
    dlg.key_path_edit.setText(r"C:\Users\deploy\.ssh\id_ed25519")
    _shoot(dlg, "screenshot-connect.png", 520, 640, "light")

    # ---- 4. 设置对话框 ------------------------------------------------
    store = _make_store(tmp, "dark")
    st = _make_settings(store, "dark")
    win = main_window.MainWindow(st, store)
    sdlg = settings_dialog.SettingsDialog(st, palette.get_palette("dark"), win)
    _shoot(sdlg, "screenshot-settings.png", 640, 640, "dark")

    print(f"\n共 {len(_HOSTS)} 张，已写入 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
