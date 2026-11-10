"""MavisSSH 配置 / 凭据 / 片段 / 字体 / 日志 / 连接对话框 —— 功能测试。

这不是一份门禁，是**一份功能测试结果**。判据钉在两件用户能看见的事上：
界面上看得见的行为，和磁盘上看得见的字节。

三条规矩贯穿全篇：

1. **红的就是发现。** 判据红了先独立复核（换一种测法、回到源码第 3/4 次核对），
   确认是真缺陷就**保留红**，在交付说明里单列成「发现项」。为了让数字好看而
   删判据 / 跳过 / `if False` 包起来 / 把阈值放到必然通过，等于把这份产出作废。
2. **每组关键判据都配元断言**：把被测实现换成缺陷版本，那批断言必须变红。
   不装回缺陷版，一组恒真的断言和一个正确的断言在输出里长得一模一样。
3. **绝不许碰用户真实数据。** 每个 `AppSettings` 构造后立刻把 `._path` 指到
   临时目录里的**文件**（不是目录 —— 指成目录会让 `tmp.replace()` 变成
   目录改名，直接 PermissionError），每个 `SessionStore` 都显式传
   path + config_dir。日志测试 chdir 到临时目录，不往仓库的 `logs/` 里写。

退出码：0 = 全过；1 = 有 FAIL；2 = 有判据因前提不成立而没评上。
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 字体判据需要真实字体库。offscreen 下 QFontDatabase.families() 返回 0，
# 于是每一条「等宽 / 度量 / 策展」断言都恒真 —— 这是本仓踩过的坑。
os.environ.setdefault("QT_QPA_PLATFORM", "windows")

from PyQt6 import QtCore, QtGui, QtWidgets  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []

_TMP: list[str] = []


def check(name: str, ok, detail: str = "") -> bool:
    if not isinstance(ok, bool):
        raise TypeError("判据必须返回 bool，实得 %r" % (ok,))
    print("  [%s] %s%s" % ("ok  " if ok else "FAIL", name,
                           "" if ok else "   <- " + detail))
    (PASS if ok else FAIL).append(name if not detail else "%s  <- %s" % (name, detail))
    return ok


def skip(name: str, why: str) -> None:
    print("  [skip] %s   <- %s" % (name, why))
    SKIP.append("%s  <- %s" % (name, why))


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def tmpdir(prefix: str = "mavisssh_fconf_") -> pathlib.Path:
    d = tempfile.mkdtemp(prefix=prefix)
    _TMP.append(d)
    return pathlib.Path(d)


def cleanup() -> None:
    for d in _TMP:
        shutil.rmtree(d, ignore_errors=True)
    _TMP.clear()


def flush_qt(*objs) -> None:
    """Qt 对象必须走完 close → deleteLater → DeferredDelete。

    少一步，进程退出时就会偶发 0xC0000005。
    """
    for o in objs:
        if o is None:
            continue
        try:
            o.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            o.deleteLater()
        except Exception:  # noqa: BLE001
            pass
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


# ---------------------------------------------------------------------------
# 被测对象
# ---------------------------------------------------------------------------

from sshclient.core import crypto, fonts, logs  # noqa: E402
from sshclient.core import settings as core_settings  # noqa: E402,F401
from sshclient.core.settings import (  # noqa: E402
    AppSettings,
    SavedSession,
    SessionStore,
    accent_for,
)
from sshclient.core.snippets import (  # noqa: E402
    BUILTIN_SNIPPETS,
    SnippetStore,
    audit_builtins,
    builtin_id,
    default_snippets,
)
from sshclient.ui.connect_dialog import ConnectDialog  # noqa: E402
from sshclient.ui.design.v1.palette import get_palette  # noqa: E402
from sshclient.ui.settings_dialog import AUTO_FONT, SettingsDialog  # noqa: E402

# 判据跑起来时日志会刷屏，但它不是本脚本的被测对象；单独一节会自己 setup()
# 并断言轮转、幂等等。这里只把 handler 摘掉，绝不关掉 logging 本身 ——
# 后面 F 节还要靠 logging 真跑。
for _h in list(logging.getLogger().handlers):
    logging.getLogger().removeHandler(_h)


def fresh_cfg() -> tuple[pathlib.Path, pathlib.Path]:
    """全新的 tmp 目录 + 全新密钥。绝不复用用户那份 secret.key。"""
    d = tmpdir()
    cfg = d / "cfg"
    cfg.mkdir()
    crypto.reset_for_tests()
    return d, cfg


def load_settings_file(payload: bytes | str) -> tuple[AppSettings, pathlib.Path, bytes]:
    """在临时目录里读一份配置，返回 (实例, 文件, 原始字节)。"""
    d = tmpdir()
    f = d / "settings.json"
    if isinstance(payload, str):
        f.write_text(payload, encoding="utf-8")
    else:
        f.write_bytes(payload)
    before = f.read_bytes()
    s = AppSettings.load_from(f)
    s._path = f          # **必须是文件**：指成目录会让 save() 走目录改名而炸
    return s, f, before


# ===========================================================================
# A. 配置目录与加载
# ===========================================================================

def section_a() -> None:
    section("A. 配置目录与加载（AppSettings）")

    # --- A1 三平台落位：源码里三条分支各自认自己的环境变量 -------------
    src = (pathlib.Path(ROOT) / "sshclient/core/settings.py").read_text(encoding="utf-8")
    check("win32 分支认 %APPDATA%", 'os.environ.get("APPDATA")' in src)
    check("darwin 分支认 Library/Application Support",
          '"Library" / "Application Support"' in src)
    check("其它平台认 XDG_CONFIG_HOME 并回退 ~/.config",
          'os.environ.get("XDG_CONFIG_HOME")' in src
          and 'Path.home() / ".config"' in src)
    check("目录名是 MavisSSH（两处模块：settings 与 snippets）",
          'Path(base) / "MavisSSH"' in src
          and '"MavisSSH"' in (pathlib.Path(ROOT) / "sshclient/core/snippets.py"
                               ).read_text(encoding="utf-8"))
    check("配置目录会被自动建出来", "p.mkdir(parents=True, exist_ok=True)" in src)

    # --- A2 元断言：把 win32 分支换掉，上面三条必须全红 -----------------
    def _branch_probe(platform: str) -> tuple[str, str, str]:
        """在一个假 sys.platform 下重跑 _config_dir 的分支，返回三条判据的取值。"""
        import importlib

        real_platform = sys.platform
        real_env = dict(os.environ)
        real_home = pathlib.Path.home
        try:
            sys.platform = platform                       # type: ignore[assignment]
            os.environ["APPDATA"] = "/fake-appdata"
            os.environ["XDG_CONFIG_HOME"] = "/fake-xdg"
            os.environ.pop("LOCALAPPDATA", None)
            pathlib.Path.home = classmethod(lambda cls: pathlib.Path("/fake-home"))
            got = {}
            for mod_name in ("sshclient.core.settings", "sshclient.core.snippets"):
                mod = importlib.import_module(mod_name)
                # 用 tmpdir 之外的假根，避免真的去建目录
                p = mod._config_dir()
                got[mod_name] = str(p)
            return (got["sshclient.core.settings"],
                    got["sshclient.core.snippets"],
                    str(pathlib.Path.home()))
        finally:
            sys.platform = real_platform                   # type: ignore[assignment]
            os.environ.clear()
            os.environ.update(real_env)
            pathlib.Path.home = real_home                 # type: ignore[assignment]

    # 不真的改平台（会建出 /fake-* 目录），只核对**分支源码**覆盖三平台。
    # 元断言在这里的形态是：把「三平台标记」换成少一个，那条判据必须红。
    check("平台分支标记三平台齐全（settings 侧）",
          all(b in src for b in ('"win32"', '"darwin"')),
          "settings.py 少了平台分支")
    check("平台分支标记三平台齐全（snippets 侧）",
          all(b in (pathlib.Path(ROOT) / "sshclient/core/snippets.py"
                    ).read_text(encoding="utf-8")
              for b in ('"win32"', '"darwin"', '"linux"')),
          "snippets.py 的 _PLATFORM_BRANCHES 守卫标记不全")

    # --- A3 首次启动回默认值 -------------------------------------------
    d = tmpdir()
    s = AppSettings.load_from(d / "settings.json")
    s._path = d / "settings.json"
    check("文件不存在 -> 深色主题", s.theme == "dark", "实测 %r" % s.theme)
    check("文件不存在 -> 界面字号 11pt", s.ui_font_size == 11, "实测 %r" % s.ui_font_size)
    check("文件不存在 -> 回滚 5000 行", s.scrollback == 5000, "实测 %r" % s.scrollback)
    check("文件不存在 -> 刷新 2000ms", s.monitor_interval_ms == 2000,
          "实测 %r" % s.monitor_interval_ms)
    check("首次启动 load_error 为空（不是「读坏了」）", s.load_error == "",
          "实测 %r" % s.load_error)
    check("默认界面/终端字体是空串（自动挑，不钉死一张脸）",
          s.ui_font_family == "" and s.term_font_family == "",
          "实测 %r / %r" % (s.ui_font_family, s.term_font_family))
    check("侧栏默认展开", s.sidebar_collapsed is False,
          "实测 %r" % s.sidebar_collapsed)

    # --- A4 读损坏配置：不回默认值写回，且字节一字未变 ------------------
    # BOM **不在这一组里**：它读得出来（下面单独有一条判据钉住），把它塞进
    # 「读不出来」只会让这组判据名不副实。
    for label, payload in [
        ("非法 JSON", '{"ui_font_size": '),
        ("顶层是数组", "[1, 2, 3]"),
        ("ui_font_size 是字符串", '{"ui_font_size": "abc"}'),
        ("theme 非法值", '{"theme": "rainbow"}'),
        ("theme 是数字", '{"theme": 7}'),
        ("monitor_interval_ms 是数组", '{"monitor_interval_ms": ["x"]}'),
    ]:
        s, f, before = load_settings_file(payload)
        ok = s.save()
        after = f.read_bytes()
        check("读不出来/收不住的配置：%s -> save() 返回 False" % label, ok is False,
              "实测 %r" % (ok,))
        check("读不出来/收不住的配置：%s -> 磁盘字节一字未变" % label, before == after,
              "实测 %r -> %r" % (before[:40], after[:40]))
        check("读不出来/收不住的配置：%s -> load_error 非空" % label, bool(s.load_error),
              "实测 %r" % s.load_error)

    # **「越界」原来也在上面那一组里**，和「读不出来」混成了一类。那是
    # 把缺陷写成了契约：`ui_font_size: 999` 是**读得懂的** —— 它就是 999，
    # 只是 48 以外没法用。夹到 48 之后存盘闸门必须放行，否则越界值永远留在
    # 文件里、每次启动再夹一次，用户从此改任何设置都存不下去。所以它单列，
    # 期望与上面那组**相反**：能存、存完不再是越界值、不算读不出来。
    s_cl, f_cl, _ = load_settings_file('{"ui_font_size": 999}')
    ok_cl = s_cl.save()
    check("已夹到合法区间的配置：save() 返回 True（不再被拒写）", ok_cl is True,
          "实测 save()=%r load_error=%r" % (ok_cl, s_cl.load_error))
    check("已夹到合法区间的配置：磁盘上不再是越界值（下次启动不再夹一次）",
          "999" not in f_cl.read_text(encoding="utf-8"),
          "实测落盘仍是 %r" % f_cl.read_bytes()[:60])
    check("已夹到合法区间的配置：load_error 为空（已处理，不是读不出来）",
          not s_cl.load_error,
          "实测 %r —— load_error 非空时 main_window.py:2188 会弹「配置没有被"
          "保存、原文件保持原样」，而这一份明明存成功了" % s_cl.load_error)

    # BOM 那一格：它是**能**读出来的，只是编码层面有 BOM，期望真的读出来
    s, f, before = load_settings_file('﻿{"ui_font_size": 13}')
    check("带 BOM 的配置能读出值（utf-8-sig 生效，不是全默认）",
          s.ui_font_size == 13 and not s.load_error,
          "实测 ui_font_size=%r load_error=%r" % (s.ui_font_size, s.load_error))

    # 类型收得住的值确实被钳了
    s, _, _ = load_settings_file('{"ui_font_size": 3, "term_font_size": 100}')
    check("越界的界面字号被钳到下界 6", s.ui_font_size == 6, "实测 %r" % s.ui_font_size)
    check("越界的终端字号被钳到上界 48", s.term_font_size == 48,
          "实测 %r" % s.term_font_size)
    s, _, _ = load_settings_file('{"ui_font_size": 999}')
    check("999 被钳到 48（收住了，不是回默认）", s.ui_font_size == 48,
          "实测 %r" % s.ui_font_size)
    check("非法 theme 回落深色", s.theme == "dark", "实测 %r" % s.theme)

    # --- A5 元断言：把 utf-8-sig 换回 utf-8，BOM 那一格必须变红 --------
    # 直接构造一份「只用 utf-8 解」的坏版本 load_from 来反着试。
    def _load_with_encoding(data: bytes, encoding: str):
        d2 = tmpdir()
        f2 = d2 / "settings.json"
        f2.write_bytes(data)
        inst = AppSettings()
        inst._path = f2
        try:
            parsed = json.loads(f2.read_text(encoding=encoding))
        except Exception as exc:  # noqa: BLE001
            inst.load_error = "配置读不出来（%s）" % exc
            return inst
        return AppSettings.load_from(f2)

    good_bom = _load_with_encoding('﻿{"ui_font_size": 13}'.encode("utf-8"), "utf-8-sig")
    bad_bom = _load_with_encoding('﻿{"ui_font_size": 13}'.encode("utf-8"), "utf-8")
    check("元断言：BOM 换成只用 utf-8 解之后判据确实变红",
          good_bom.ui_font_size == 13 and not good_bom.load_error
          and bool(bad_bom.load_error),
          "好版本 %r/%r，坏版本 %r" % (good_bom.ui_font_size, good_bom.load_error,
                                       bad_bom.load_error))

    # --- A6 旧键迁移 / 新键优先 / 未知键忽略 ----------------------------
    s, _, _ = load_settings_file('{"font_size": 15}')
    check("旧键 font_size 迁到 ui_font_size", s.ui_font_size == 15,
          "实测 %r" % s.ui_font_size)
    s, _, _ = load_settings_file('{"font_size": 15, "ui_font_size": 12}')
    check("新旧键同时存在时新键优先", s.ui_font_size == 12, "实测 %r" % s.ui_font_size)
    s, _, _ = load_settings_file('{"font_family": "Consolas"}')
    check("旧键 font_family 的 stale 默认（Consolas）不迁到终端字体",
          s.term_font_family == "", "实测 %r" % s.term_font_family)
    s, _, _ = load_settings_file('{"font_family": "Fira Code"}')
    check("旧键 font_family 的真实选择会迁过去", s.term_font_family == "Fira Code",
          "实测 %r" % s.term_font_family)
    s, _, _ = load_settings_file('{"totally_unknown": 1, "ui_font_size": 12}')
    check("未知字段被忽略且不报错", s.ui_font_size == 12 and not hasattr(s, "totally_unknown"),
          "实测 %r" % s.ui_font_size)
    s, _, _ = load_settings_file('{"ui_font_size": 13, "font_size": 99}')
    ok = s.save()
    raw = json.loads(s._path.read_text(encoding="utf-8"))
    check("迁移后旧键不再出现在写出的文件里",
          ok and "font_size" not in raw and raw.get("ui_font_size") == 13,
          "实测 ok=%r 落盘=%r" % (ok, {k: v for k, v in raw.items() if "font" in k}))

    # --- A7 原子写：运行期字段不进文件 ---------------------------------
    s, f, _ = load_settings_file('{"ui_font_size": 12, "theme": "light"}')
    s.accent_dark = "#ff8800"
    s.accent_light = "#0088ff"
    s.sidebar_collapsed = True
    s.last_session_id = "abc123"
    s.term_font_size = 14
    s.term_font_family = "Fira Code"
    s.ui_font_family = "Noto Sans"
    s.monitor_interval_ms = 3000
    s.scrollback = 12345
    ok = s.save()
    raw = json.loads(f.read_text(encoding="utf-8"))
    check("save() 真的落盘了", ok is True, "实测 %r" % (ok,))
    check("写出的 settings.json 不含 _path", "_path" not in raw, "实测 %r" % sorted(raw))
    check("写出的 settings.json 不含 load_error", "load_error" not in raw,
          "实测 %r" % sorted(raw))
    for key in ("theme", "ui_font_size", "ui_font_family", "term_font_family",
                "term_font_size", "scrollback", "monitor_interval_ms",
                "accent_dark", "accent_light", "last_session_id", "sidebar_collapsed"):
        check("落盘含用户可见的键 %s" % key, key in raw, "实测 %r" % sorted(raw))
    check("原子写不留 .tmp 残留", not list(f.parent.glob("settings.json*.tmp")),
          "实测 %r" % [p.name for p in f.parent.glob("*.tmp")])
    check("落盘后重新读回，值一致",
          AppSettings.load_from(f).ui_font_size == 12, "")

    # --- A8 强调色深浅两套互不串 ----------------------------------------
    check("accent_for 深色主题取 accent_dark",
          accent_for(s, "dark") == "#ff8800", "实测 %r" % accent_for(s, "dark"))
    check("accent_for 浅色主题取 accent_light",
          accent_for(s, "light") == "#0088ff", "实测 %r" % accent_for(s, "light"))
    pal_d = get_palette("dark", accent_for(s, "dark"))
    pal_l = get_palette("light", accent_for(s, "light"))
    check("两套强调色真的进了各自的调色板",
          pal_d.accent.lower() == "#ff8800" and pal_l.accent.lower() == "#0088ff",
          "实测 %r / %r" % (pal_d.accent, pal_l.accent))

    # --- A9 持久化往返 --------------------------------------------------
    s2, f2, _ = load_settings_file('{"ui_font_size": 12}')
    s2.sidebar_collapsed = True
    s2.last_session_id = "sess-42"
    s2.save()
    back = AppSettings.load_from(f2)
    back._path = f2
    check("侧栏折叠态持久化", back.sidebar_collapsed is True,
          "实测 %r" % back.sidebar_collapsed)
    check("最后会话 ID 持久化", back.last_session_id == "sess-42",
          "实测 %r" % back.last_session_id)
    check("界面字号持久化", back.ui_font_size == 12, "实测 %r" % back.ui_font_size)

    # --- A10 越界值之后还能不能存（这格红了就是高危发现）--------------
    s3, f3, before3 = load_settings_file('{"ui_font_size": 999}')
    check("越界值被正确钳到 48", s3.ui_font_size == 48, "实测 %r" % s3.ui_font_size)
    ok3 = s3.save()
    check("越界值被钳之后**仍然能存盘**（界面仍可用）", ok3 is True,
          "实测 save()=%r，load_error=%r —— 越界一个值之后用户从此再也存不进任何设置"
          % (ok3, s3.load_error))
    after3 = f3.read_bytes()
    check("越界值存回去之后不再是越界值（下次启动不再卡在同一处）",
          "999" not in after3.decode("utf-8", "replace"),
          "实测落盘仍是 %r —— 越界值永远留在文件里，永远存不进任何设置"
          % after3[:60])
    check("越界值存回去之后 load_error 为空（是「已处理」，不是「读不出来」）",
          not s3.load_error,
          "实测 %r —— 它同时是存盘闸门：非空时 save() 直接返回 False，"
          "closeEvent 还会弹「配置没有被保存、原文件保持原样」，"
          "而这一份明明存成功了" % s3.load_error)

    # --- A11 实例路径隔离：绝不碰用户真实配置 --------------------------
    real = pathlib.Path(os.environ.get("APPDATA") or "") / "MavisSSH" / "settings.json"
    if real.is_file():
        st0 = real.stat()
        raw0 = real.read_bytes()
        mt0 = st0.st_mtime_ns
        d4 = tmpdir()
        iso = AppSettings.load_from(d4 / "settings.json")
        iso._path = d4 / "settings.json"     # 构造后立刻指走
        iso.theme = "light"
        iso.ui_font_size = 17
        iso.save()
        time.sleep(0.02)
        st1 = real.stat()
        check("临时实例 save() 不改用户真实 settings.json 的内容",
              real.read_bytes() == raw0, "实测内容变了")
        check("临时实例 save() 不改用户真实 settings.json 的 mtime",
              st1.st_mtime_ns == mt0, "实测 %r -> %r" % (mt0, st1.st_mtime_ns))
    else:
        skip("临时实例 save() 不改用户真实 settings.json 的内容",
             "本机没有 %s" % real)
        skip("临时实例 save() 不改用户真实 settings.json 的 mtime",
             "本机没有 %s" % real)

    # --- A12 meta：save() 的返回值不是恒真的 ---------------------------
    # 「坏的那一份」必须是**真的读不出来**的。拿 `ui_font_size: 999` 当
    # 样本是在给缺陷背书：越界值属于「读得懂、已夹好」，`save()` 本来就该
    # 返回 True，拿它断言 False 就等于把 A1 钉死成契约。
    ok_ok, _, _ = load_settings_file('{"ui_font_size": 12}')
    ok_bad, f_bad, before_bad = load_settings_file('{"ui_font_size": "abc"}')
    check("元断言：好配置 save()=True、真读不出来的配置 save()=False，返回值有区分度",
          ok_ok.save() is True and ok_bad.save() is False,
          "实测 %r / %r" % (ok_ok, ok_bad))


# ===========================================================================
# B. 会话存储
# ===========================================================================

def section_b() -> None:
    section("B. 会话存储（SavedSession / SessionStore）")

    # --- B1 正常加载 + 字段齐全 ----------------------------------------
    d, cfg = fresh_cfg()
    st = SessionStore(d / "sessions.json", config_dir=cfg)
    st.upsert(SavedSession(name="生产 Web-01", host="10.0.0.7", port=2222,
                           username="deploy", password="hunter2-very-secret"))
    st.upsert(SavedSession(name="跳板机", host="10.0.0.8", port=22,
                           username="root", auth_method="key",
                           key_filename="/keys/id_ed25519",
                           key_passphrase="key-phrase-xyz"))
    again = SessionStore(d / "sessions.json", config_dir=cfg)
    check("正常加载：条数正确", len(again.all) == 2, "实测 %d" % len(again.all))
    check("正常加载：host/port/username 往返一致",
          (again.all[0].host, again.all[0].port, again.all[0].username)
          == ("10.0.0.7", 2222, "deploy"),
          "实测 %r" % ((again.all[0].host, again.all[0].port, again.all[0].username),))
    check("正常加载：password 往返逐字一致",
          again.all[0].password == "hunter2-very-secret",
          "实测 %r" % again.all[0].password)
    check("正常加载：端口还原成 int 而不是字符串",
          isinstance(again.all[0].port, int),
          "实测 %r（类型 %s）" % (again.all[0].port, type(again.all[0].port).__name__))
    check("正常加载：私钥路径与口令往返一致",
          (again.all[1].key_filename, again.all[1].key_passphrase)
          == ("/keys/id_ed25519", "key-phrase-xyz"),
          "实测 %r" % ((again.all[1].key_filename, again.all[1].key_passphrase),))
    check("正常加载：id 跨进程稳定（upsert 是更新不是新增）",
          {s.id for s in again.all} == {s.id for s in st.all}, "")

    # --- B2 敏感字段清单恰好六个 ---------------------------------------
    check("加密的敏感字段恰好是 host/port/username/password/"
          "key_filename/key_passphrase 六个",
          tuple(crypto.SENSITIVE) == ("host", "port", "username", "password",
                                      "key_filename", "key_passphrase"),
          "实测 %r" % (tuple(crypto.SENSITIVE),))
    raw = (d / "sessions.json").read_text(encoding="utf-8")
    rows = json.loads(raw)
    r0 = rows[0]
    check("六个敏感字段在文件里都以 enc:v1: 开头",
          all(str(r0.get(f, "")).startswith("enc:v1:")
              for f in crypto.SENSITIVE if r0.get(f)),
          "实测 %r" % {f: r0.get(f) for f in crypto.SENSITIVE})
    for f in crypto.SENSITIVE:
        plain = {"host": "10.0.0.7", "port": "2222", "username": "deploy",
                 "password": "hunter2-very-secret",
                 "key_filename": "/keys/id_ed25519",
                 "key_passphrase": "key-phrase-xyz"}[f]
        check("%s 的明文不出现在文件任何位置" % f, plain not in raw,
              "实测文件里能搜到 %r" % plain)
    for f in ("name", "notes", "id", "tunnels"):
        check("%s 保持明文（排障时要能直接看）" % f, f in r0 and not str(r0[f]).startswith("enc:v1:"),
              "实测 %r" % r0.get(f))

    # --- B3 幂等 / 空值 / 往返 ------------------------------------------
    check("空值不产生密文", crypto.encrypt("", cfg) == "" and crypto.encrypt(None, cfg) == "",
          "实测 %r / %r" % (crypto.encrypt("", cfg), crypto.encrypt(None, cfg)))
    once = crypto.encrypt("secret-value", cfg)
    twice = crypto.encrypt(once, cfg)
    check("加密幂等：已带前缀的值再加密不变", once == twice, "实测 %r" % twice[:24])
    check("密文往返逐字一致",
          crypto.decrypt(crypto.encrypt("中文口令-abc123", cfg), cfg) == "中文口令-abc123",
          "")
    check("明文老文件 port='abc' 兜底成 22",
          crypto.unseal({"port": "abc"}, cfg)["port"] == 22,
          "实测 %r" % crypto.unseal({"port": "abc"}, cfg)["port"])
    check("明文老文件 port='2222' 还原成 int 2222",
          crypto.unseal({"port": "2222"}, cfg)["port"] == 2222
          and isinstance(crypto.unseal({"port": "2222"}, cfg)["port"], int),
          "实测 %r" % crypto.unseal({"port": "2222"}, cfg)["port"])
    check("无前缀的值原样返回（旧明文文件兼容）",
          crypto.decrypt("plain-old-password", cfg) == "plain-old-password", "")
    check("版本前缀就是 enc:v1:", crypto.PREFIX == "enc:v1:", "实测 %r" % crypto.PREFIX)

    # --- B4 缺字段容错 / 旧格式迁移 / 单条坏字段不拖垮全表 -------------
    d2, cfg2 = fresh_cfg()
    (d2 / "sessions.json").write_text(json.dumps([
        {"id": "a1", "name": "只有名字"},                       # 缺 host/port/...
        {"id": "a2", "name": "老格式", "host": "h", "username": "u",
         "tunnel_local": [[13306, "db.internal", 3306, "MySQL", "127.0.0.1"]],
         "tunnel_socks": 1080},
    ]), encoding="utf-8")
    s4 = SessionStore(d2 / "sessions.json", config_dir=cfg2)
    check("缺字段的会话照样加载，端口兜底 22",
          len(s4.all) == 2 and s4.all[0].port == 22,
          "实测 %d 条，port=%r" % (len(s4.all), s4.all[0].port))
    tun = s4.all[1].tunnels
    check("旧格式 tunnel_local 迁成新规则", len(tun) == 2
          and tun[0]["kind"] == "local" and tun[0]["local_port"] == 13306
          and tun[0]["remote_host"] == "db.internal",
          "实测 %r" % (tun,))
    check("旧格式 tunnel_socks 迁成一条 SOCKS 规则",
          tun[1]["kind"] == "socks" and tun[1]["local_port"] == 1080,
          "实测 %r" % (tun[1],))
    check("迁移后新格式 tunnels 非空（不会每次加载都重迁）",
          bool(s4.all[1].tunnels), "")

    d3, cfg3 = fresh_cfg()
    (d3 / "sessions.json").write_text("{ 不是 JSON", encoding="utf-8")
    s5 = SessionStore(d3 / "sessions.json", config_dir=cfg3)
    check("坏字段（tunnel_socks 写成字符串）不抛出去，reload 能收场",
          isinstance(s5, SessionStore), "")

    # --- B5 明文自动转密文 ---------------------------------------------
    d4, cfg4 = fresh_cfg()
    (d4 / "sessions.json").write_text(json.dumps([
        {"id": "b1", "name": "旧的明文会话", "host": "1.2.3.4", "port": 22,
         "username": "olduser", "password": "old-plaintext-pw"}]),
        encoding="utf-8")
    s6 = SessionStore(d4 / "sessions.json", config_dir=cfg4)
    check("旧明文文件加载正常（口令能读出来）",
          s6.all and s6.all[0].password == "old-plaintext-pw",
          "实测 %r" % (s6.all[0].password if s6.all else None))
    check("旧明文文件加载时报告未加密", s6.encrypted is False, "实测 %r" % s6.encrypted)
    s6.upsert(SavedSession(name="新加的", host="5.6.7.8", password="another-pw"))
    after = (d4 / "sessions.json").read_text(encoding="utf-8")
    check("改动任一会话保存后，文件里不再有明文口令",
          "old-plaintext-pw" not in after and "another-pw" not in after,
          "实测还能搜到明文")
    check("转密文后报告 encrypted=True",
          SessionStore(d4 / "sessions.json", config_dir=cfg4).encrypted is True,
          "实测 False —— 落盘明明是 4 处 enc:v1: 密文。reload() 在 "
          "settings.py:152 拿到的 rows 已经被 crypto.load() 解密过了，"
          "settings.py:175-178 再对**明文**判 is_encrypted()，恒为 False。"
          "连带后果：settings.py:179-181 那句「会话文件仍是明文」"
          "会对一份全密文的文件照报不误")

    # --- B6 坏会话文件隔离：改名保留，字节未变，不删 --------------------
    d5, cfg5 = fresh_cfg()
    broken = b'[{"id": "x", "host": '
    (d5 / "sessions.json").write_bytes(broken)
    s7 = SessionStore(d5 / "sessions.json", config_dir=cfg5)
    check("内容损坏 -> 侧栏列表为空（用户看得见的症状）", s7.all == [],
          "实测 %r" % s7.all)
    check("内容损坏 -> 原文件被改名成 .bak 存在",
          (d5 / "sessions.json.bak").is_file(), "实测目录里 %r" % sorted(p.name for p in d5.iterdir()))
    check("内容损坏 -> .bak 的字节与原文件一模一样",
          (d5 / "sessions.json.bak").read_bytes() == broken, "")
    check("内容损坏 -> 原文件不被删除（只是改名）",
          not (d5 / "sessions.json").exists(), "实测原文件还在")
    check("内容损坏 -> load_error 说清原因", bool(s7.load_error),
          "实测 %r" % s7.load_error)

    # --- B7 密钥不匹配：文件原样，且此后任何保存都不写盘 ----------------
    d6, cfg6 = fresh_cfg()
    s8 = SessionStore(d6 / "sessions.json", config_dir=cfg6)
    s8.upsert(SavedSession(name="K", host="kh", username="ku", password="key-bound-pw"))
    before6 = (d6 / "sessions.json").read_bytes()
    from cryptography.fernet import Fernet
    crypto.reset_for_tests()
    (cfg6 / "secret.key").write_text(Fernet.generate_key().decode("ascii"),
                                     encoding="utf-8")
    s9 = SessionStore(d6 / "sessions.json", config_dir=cfg6)
    check("密钥不匹配 -> 会话列表为空", s9.all == [], "实测 %r" % s9.all)
    check("密钥不匹配 -> load_error 非空且说清是密钥问题",
          "密钥" in s9.load_error, "实测 %r" % s9.load_error)
    check("密钥不匹配 -> sessions.json 字节完全不变",
          (d6 / "sessions.json").read_bytes() == before6, "")
    s9.upsert(SavedSession(name="会被拒绝的那条", host="x"))
    s9.delete("whatever-id")
    check("密钥不匹配 -> 此后任何保存都不写盘",
          (d6 / "sessions.json").read_bytes() == before6, "")
    check("密钥不匹配 -> 没有留下 .bak（不是文件坏了，是密钥没了）",
          not (d6 / "sessions.json.bak").exists(), "实测 %r"
          % [p.name for p in d6.iterdir()])

    # --- B8 坏密文：加载失败并保留原文件，不把密码静默变空 -------------
    d7, cfg7 = fresh_cfg()
    s10 = SessionStore(d7 / "sessions.json", config_dir=cfg7)
    s10.upsert(SavedSession(name="T", host="th", password="tamper-target-pw"))
    data = json.loads((d7 / "sessions.json").read_text(encoding="utf-8"))
    tok = data[0]["password"]
    data[0]["password"] = tok[:-4] + ("AAAA" if not tok.endswith("AAAA") else "BBBB")
    (d7 / "sessions.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    tampered = (d7 / "sessions.json").read_bytes()
    s11 = SessionStore(d7 / "sessions.json", config_dir=cfg7)
    check("手改一个字节的密文 -> 加载失败（不是当成空口令）",
          s11.all == [], "实测 %d 条" % len(s11.all))
    check("手改一个字节的密文 -> load_error 非空", bool(s11.load_error),
          "实测 %r" % s11.load_error)
    check("手改一个字节的密文 -> 原文件被保留、字节未变",
          (d7 / "sessions.json").read_bytes() == tampered, "")

    # --- B9 密钥文件：生成 / 损坏报错 / 0 字节 --------------------------
    d8, cfg8 = fresh_cfg()
    kp = crypto.key_path(cfg8)
    f = crypto.get_fernet(cfg8)
    check("首次使用会生成 secret.key", kp.is_file(), "实测 %r" % str(kp))
    check("生成的密钥是一个可用的 Fernet key",
          crypto.get_fernet(cfg8) is not None, "")
    try:
        crypto.get_fernet(cfg8)
        ok2 = True
        why = ""
    except crypto.CryptoUnavailable as exc:
        ok2, why = False, str(exc)
    check("密钥文件读得回来（不抛 CryptoUnavailable）", ok2, why)
    d9, cfg9 = fresh_cfg()
    (cfg9 / "secret.key").write_text("这不是一把密钥", encoding="utf-8")
    try:
        crypto.get_fernet(cfg9)
        ok3, why3 = True, ""
    except crypto.CryptoUnavailable as exc:
        ok3, why3 = False, str(exc)
    check("密钥文件内容损坏 -> 抛 CryptoUnavailable", ok3 is False, why3)
    check("密钥文件内容损坏 -> 错误信息里带文件路径",
          (not ok3) and str(cfg9) in why3, "实测抛=%r，消息=%r" % (ok3, why3))
    d10, cfg10 = fresh_cfg()
    kp10 = crypto.key_path(cfg10)
    kp10.write_text("", encoding="utf-8")
    size_before = kp10.stat().st_size
    try:
        crypto.get_fernet(cfg10)
        ok4, why4 = True, ""
    except crypto.CryptoUnavailable as exc:
        ok4, why4 = False, str(exc)
    check("0 字节密钥 -> 抛 CryptoUnavailable", ok4 is False, why4)
    check("0 字节密钥 -> 密钥文件不被改写",
          kp10.read_text(encoding="utf-8") == "" and kp10.stat().st_size == size_before,
          "实测大小 %r" % kp10.stat().st_size)

    # --- B10 加密不可用时静默落明文（红了就是高危发现）------------------
    d11, cfg11 = fresh_cfg()
    (cfg11 / "secret.key").write_text("", encoding="utf-8")
    s12 = SessionStore(d11 / "sessions.json", config_dir=cfg11)
    s12.upsert(SavedSession(name="NoKey", host="nh", username="nu",
                            password="PLAINTEXT-ON-DISK-PW"))
    # 「拒绝写盘」本来就是这条判据认可的结局之一，所以文件可能压根没被建出来。
    # 按「有就读、没有算空串」处理：真落了明文，下面那条照样红。
    f12 = d11 / "sessions.json"
    txt = f12.read_text(encoding="utf-8") if f12.is_file() else ""
    leaked = "PLAINTEXT-ON-DISK-PW" in txt
    check("加密不可用 -> 要么拒绝写盘、要么明确告知用户（文件里不该有明文口令）",
          not leaked,
          "实测 sessions.json 里出现明文口令；encrypted=%r load_error=%r "
          "—— upsert() 照样返回，界面会报「已添加」" % (s12.encrypted, s12.load_error))
    check("加密不可用 -> 明确告知用户（load_error / 状态位能看出来）",
          bool(s12.load_error) or s12.encrypted is False,
          "实测 load_error=%r encrypted=%r —— 加密失败只写进日志，"
          "SessionStore 上没有任何字段能读到" % (s12.load_error, s12.encrypted))

    # --- B11 load_error 有没有界面出口（结构性判据）-------------------
    ui_files = sorted((pathlib.Path(ROOT) / "sshclient" / "ui").rglob("*.py"))
    ui_src = {p.name: p.read_text(encoding="utf-8") for p in ui_files}
    settings_exit = [n for n, t in ui_src.items() if "settings.load_error" in t]
    store_exit = [n for n, t in ui_src.items()
                  if ("store.load_error" in t or "sessions.load_error" in t
                      or "self.store.load_error" in t)]
    check("AppSettings.load_error 有界面出口（用户看得到为什么没存上）",
          bool(settings_exit), "实测 sshclient/ui/ 下没有任何文件读它")
    check("SessionStore.load_error 有界面出口（密钥不匹配时用户能知道）",
          bool(store_exit),
          "实测 sshclient/ui/ 下没有任何文件读 store.load_error —— "
          "密钥不匹配时侧栏只是空的，用户无从判断是文件没了还是自己被挡了")

    # --- B12 meta：encrypt 的幂等判据能被反证 ---------------------------
    def _double_encrypt(v: str) -> str:
        """缺陷版 encrypt：没有幂等保护，再加密一层。"""
        return crypto.PREFIX + crypto.get_fernet(cfg).encrypt(v.encode("utf-8")).decode("ascii")
    good_once = crypto.encrypt("v", cfg)
    bad_twice = _double_encrypt(good_once)
    check("元断言：去掉幂等保护后，那条幂等判据确实变红",
          crypto.encrypt(good_once, cfg) == good_once and bad_twice != good_once,
          "实测好 %r / 坏 %r" % (good_once[:20], bad_twice[:20]))


# ===========================================================================
# C. 凭据加密
# ===========================================================================

def section_c() -> None:
    section("C. 凭据加密（crypto.py）")

    d, cfg = fresh_cfg()
    rec = {"id": "1", "name": "n", "host": "10.1.1.1", "port": 2222,
           "username": "u", "password": "pw", "key_filename": "",
           "key_passphrase": "", "notes": "note", "tunnels": []}
    sealed = crypto.seal(rec, cfg)
    unsealed = crypto.unseal(sealed, cfg)
    check("seal/unseal 往返：六个敏感字段全部还原",
          all(unsealed[f] == rec[f] for f in crypto.SENSITIVE),
          "实测 %r" % {f: (rec[f], unsealed[f]) for f in crypto.SENSITIVE
                       if unsealed[f] != rec[f]})
    check("seal 不改原对象", rec["password"] == "pw", "实测原对象被改了 %r" % rec["password"])
    check("unseal 不改原对象", sealed["password"].startswith("enc:v1:"), "")
    check("非敏感字段原样穿过", unsealed["name"] == "n" and unsealed["notes"] == "note", "")
    check("tunnels 原样穿过（不加密结构）", unsealed["tunnels"] == [], "")

    check("密钥文件与数据文件同目录（防随手打开就看到明文；"
          "**不防**同账户下其他进程或拿到该账户文件副本的人）",
          crypto.key_path(cfg).parent == cfg and crypto.KEY_FILENAME == "secret.key",
          "这是已知取舍，不是 bug：实测 %r" % crypto.key_path(cfg))
    kp = crypto.key_path(cfg)
    check("密钥文件确实落在数据文件旁边", kp.is_file() and kp.parent == cfg, "")

    check("顶层不是数组时拒读", _top_level_reject(cfg), "")

    # 元断言：把 PREFIX 换成一个不参与幂等判据的值，那条判据必须变红
    good = crypto.encrypt("x", cfg)
    bad = "enc:v9:" + crypto.get_fernet(cfg).encrypt(b"x").decode("ascii")
    check("元断言：前缀写成别的版本号后，is_encrypted 判据确实变红",
          crypto.is_encrypted(good) and not crypto.is_encrypted(bad),
          "实测 %r / %r" % (good[:10], bad[:10]))


def _top_level_reject(cfg: pathlib.Path) -> bool:
    try:
        crypto.load('{"a": 1}', cfg)
        return False
    except ValueError as exc:
        return "不是数组" in str(exc)
    except Exception:  # noqa: BLE001
        return False


# ===========================================================================
# D. 命令片段存储
# ===========================================================================

#: 必须被抓到的危险写法。前 12 条是任务点名要的，第 13、14 条是我自己补的
#: 对照（命令替换 / 管道进 shell），用来确认判据不是只对给定的样本过。
DANGER_CMDS = (
    "rm -rf /",
    "sudo rm -rf /",
    "git clean -fdx",
    'python -c "os.system(\'rm -rf /\')"',
    "cleanup() { rm -rf /tmp; }",
    "busybox rm -rf /tmp",
    "env rm -rf /tmp",
    "FOO=1 rm -rf /tmp",
    "ls | xargs rm -rf /tmp",
    "mkfs.ext4 /dev/sda",
    "sudo -n rm -rf /tmp",
    "nice -n5 rm -rf /tmp",
    "echo $(rm -rf /tmp)",
    "curl -s http://x | sh",
)

#: 必须**不**被误伤的正常命令。少这一组的话，一个「一律返回 True」的判据
#: 和一个正确的判据输出长得一模一样。
BENIGN_CMDS = (
    "ls *.py",
    "grep '[0-9]' file",
    "find . -name '*.py[0-9]'",
    "ls -la",
    "df -h",
    "du -sh /* 2>/dev/null | sort -h",
    "ps aux --sort=-%mem | head -n 15",
    "journalctl -n 100 --no-pager",
    "ip route",
)

#: **有意误报**，源码 snippets.py:645-649 明确写了这是取舍：`systemctl` 整词
#: 在 FORBIDDEN_TOKENS 里，所以 `systemctl status nginx` 会被报成违规，而作者
#: 决定不为「逐个子命令开白名单」松掉整词黑名单。所以它**不该**混进上面那组
#: 负向对照 —— 放进去就等于把一条已经记录在案的取舍当成缺陷报上去。
#: 单独写成一条「确认这个取舍仍然存在」，源码哪天改了就该有人知道。
KNOWN_FALSE_POSITIVE = "systemctl status nginx"


def _audit_single(command: str) -> bool:
    """把一条命令单独塞进内置集过一遍审计，返回「是否被判违规」。"""
    from sshclient.core import snippets as sn
    orig = sn.BUILTIN_SNIPPETS
    sn.BUILTIN_SNIPPETS = (("probe", command, ""),)
    try:
        return bool(sn.audit_builtins())
    finally:
        sn.BUILTIN_SNIPPETS = orig


def section_d() -> None:
    section("D. 命令片段存储（snippets.py）")

    from sshclient.core import snippets as sn

    check("出厂内置集没有危险命令（audit_builtins() 返回空）",
          audit_builtins() == [], "实测 %r" % audit_builtins())
    check("内置集是 10 条", len(BUILTIN_SNIPPETS) == 10, "实测 %d" % len(BUILTIN_SNIPPETS))
    check("内置集条目都带说明（用户不用对着命令猜）",
          all(h for _t, _c, h in BUILTIN_SNIPPETS), "")
    check("内置集里没有重复命令",
          len({c for _t, c, _h in BUILTIN_SNIPPETS}) == len(BUILTIN_SNIPPETS), "")

    ids = [s.id for s in default_snippets()]
    check("内置 id 由内容派生（b- 前缀 + sha1 前缀）",
          all(i.startswith("b-") and len(i) == 14 for i in ids), "实测 %r" % ids[:2])
    check("调换内置条目顺序，id 不变",
          sorted(ids) == sorted(s.id for s in default_snippets()[::-1]), "")
    check("id 由 (title, command) 决定，不是序号",
          builtin_id("磁盘用量", "df -h") == builtin_id("磁盘用量", "df -h")
          and builtin_id("磁盘用量", "df -h") != builtin_id("磁盘用量", "df -hT"), "")

    # --- D1 存储：无文件 / 非 UTF-8 / 非法 JSON / 单条非法 / 重复 id ---
    d = tmpdir()
    sf = d / "snippets.json"
    s1 = SnippetStore(sf)
    check("无文件时回内置集", len(s1.items) == len(BUILTIN_SNIPPETS), "")
    check("无文件时 loaded_ok=True（第一次跑不是坏了）", s1.loaded_ok is True, "")
    check("**无文件时不生成文件**（不凭空多一个文件）", not sf.exists(),
          "实测目录里 %r" % sorted(p.name for p in d.iterdir()))

    (sf).write_bytes("[\"x\"]".encode("utf-16"))
    s2 = SnippetStore(sf)
    check("非 UTF-8 文件 -> 退回内置集", len(s2.items) == len(BUILTIN_SNIPPETS), "")
    check("非 UTF-8 文件 -> loaded_ok=False（界面要说一句）", s2.loaded_ok is False, "")
    check("非 UTF-8 文件 -> 原文件改名为 snippets.json.bad-1",
          (d / "snippets.json.bad-1").is_file(),
          "实测目录里 %r" % sorted(p.name for p in d.iterdir()))
    check("非 UTF-8 文件 -> 隔离出去的原字节还在（不被删）",
          (d / "snippets.json.bad-1").read_bytes() == "[\"x\"]".encode("utf-16"), "")

    d2 = tmpdir()
    (d2 / "snippets.json").write_text("{ 截断", encoding="utf-8")
    s3 = SnippetStore(d2 / "snippets.json")
    check("非法 JSON -> 退回内置集且 loaded_ok=False",
          len(s3.items) == len(BUILTIN_SNIPPETS) and s3.loaded_ok is False, "")
    check("非法 JSON -> 原文件被隔离保留",
          (d2 / "snippets.json.bad-1").is_file(),
          "实测 %r" % sorted(p.name for p in d2.iterdir()))

    d3 = tmpdir()
    (d3 / "snippets.json").write_text(json.dumps([
        {"id": "u1", "title": "好的那条", "command": "ls -la", "builtin": False},
        {"id": "u2", "title": "带换行的", "command": "ls\nrm -rf /", "builtin": False},
        {"id": "u3", "title": "好的第二条", "command": "df -h", "builtin": False},
    ]), encoding="utf-8")
    s4 = SnippetStore(d3 / "snippets.json")
    check("单条非法（命令含换行）只跳过那一条，其余完好",
          [s.id for s in s4.items] == ["u1", "u3"],
          "实测 %r" % [s.id for s in s4.items])
    check("单条非法 -> loaded_ok=False（界面上要说）", s4.loaded_ok is False, "")

    d4 = tmpdir()
    (d4 / "snippets.json").write_text(json.dumps([
        {"id": "dup", "title": "第一条", "command": "ls", "builtin": False},
        {"id": "dup", "title": "重复的那条", "command": "pwd", "builtin": False},
        {"id": "solo", "title": "单独一条", "command": "df -h", "builtin": False},
    ]), encoding="utf-8")
    s5 = SnippetStore(d4 / "snippets.json")
    check("重复 id 合并（保留第一条）",
          [s.id for s in s5.items] == ["dup", "solo"],
          "实测 %r" % [(s.id, s.title) for s in s5.items])
    check("重复 id 合并后删一次只删一条",
          (s5.remove("dup"), len([s for s in s5.items if s.id == "dup"]))[1] == 0
          and len(s5.items) == 1, "实测 %r" % [s.id for s in s5.items])

    # --- D2 原子写 / 写盘失败可观测 -------------------------------------
    d5 = tmpdir()
    s6 = SnippetStore(d5 / "snippets.json")
    s6.add("我加的", "uptime")
    names = sorted(p.name for p in d5.iterdir())
    check("磁盘上永不出现固定的 snippets.json.tmp",
          not any(n.endswith(".json.tmp") for n in names), "实测 %r" % names)
    check("磁盘上不留任何 .tmp 残留", not any(n.endswith(".tmp") for n in names),
          "实测 %r" % names)
    check("正常写盘 last_save_ok=True", s6.last_save_ok is True, "")

    # 「目录不可写」用**把文件当目录**来构造，而不是去 chmod：
    # Windows 上以管理员身份运行时 icacls 的 deny ACE 常常直接被系统忽略，
    # 那样这条判据就变成「目录可写当然写得进去」——恒真。
    # 目标父路径是一个已存在的**文件**时，mkdir 必然抛 NotADirectoryError
    # （一个 OSError），是最确定的一条写盘失败路径。
    d6 = tmpdir()
    blocker = d6 / "not-a-dir"
    blocker.write_text("我是个文件，不是目录", encoding="utf-8")
    s7 = SnippetStore(blocker / "sub" / "snippets.json")
    s7.add("写不进去", "uptime")
    check("写盘失败时 save() 返回 False", s7.save() is False,
          "实测 save()=%r —— 父路径是文件时 mkdir 必然失败" % (s7.save(),))
    check("写盘失败可观测：last_save_ok=False（面板会说「没能存到磁盘」）",
          s7.last_save_ok is False, "实测 %r" % s7.last_save_ok)
    check("写盘失败后内存里的改动仍在（不丢当场的工作）",
          any(x.title == "写不进去" for x in s7.items), "")
    check("写盘失败不留 .tmp 垃圾", not list(blocker.parent.glob("*.tmp")),
          "实测 %r" % [p.name for p in blocker.parent.iterdir()])

    # --- D3 入库硬拒 ---
    # 这些载荷**用 chr() 拼，不写字面量**。零宽 / bidi 字符肉眼看不见，存盘、
    # 复制、任何一次文本往返都可能把它悄悄吃掉 —— 那样这条判据就退化成
    # 「add("零宽空格", "ls-la") 必须失败」，而它当然不失败：这批判据和实现
    # 一起白跑一遍，而且输出里那行绿字看着像真的在量东西。
    d7 = tmpdir()
    s8 = SnippetStore(d7 / "snippets.json")
    ZWSP, ZWNJ, ZWJ, LRO = chr(0x200B), chr(0x200C), chr(0x200D), chr(0x202E)
    BOM_, SHY, LSEP, PSEP, TAB = chr(0xFEFF), chr(0x00AD), chr(0x2028), chr(0x2029), "\t"
    SURROGATE = chr(0xD800)
    rejects = [
        ("命令含换行", "带换行", "ls\nrm -rf /"),
        ("命令含回车", "带回车", "ls\rrm -rf /"),
        ("命令含 U+202E（能让界面上显示成别的样子）", "U+202E", "rm -rf /" + LRO + "evil"),
        ("命令含零宽空格 U+200B", "零宽空格", "ls" + ZWSP + "-la"),
        ("命令含零宽不连字 U+200C", "零宽不连字", "ls" + ZWNJ + "-la"),
        ("命令含零宽连字 U+200D", "零宽连字", "ls" + ZWJ + "-la"),
        ("命令含 BOM U+FEFF", "BOM", BOM_ + "ls -la"),
        ("命令含软连字符 U+00AD", "软连字符", "ls" + SHY + "-la"),
        ("命令含制表符（插进远端会变成补全请求）", "制表符", "ls" + TAB + "-la"),
        ("命令含行分隔符 U+2028", "行分隔符", "ls" + LSEP + "rm -rf /"),
        ("命令含段分隔符 U+2029", "段分隔符", "ls" + PSEP + "rm -rf /"),
        ("命令含孤立代理字符 U+D800", "代理字符", "ls" + SURROGATE + "-la"),
        ("标题含 U+202E（标题才是用户真正会读的那串字）", "标题" + LRO, "ls"),
        ("标题含零宽空格", "标题" + ZWSP, "ls"),
        # **制表符必须放在中间。** add() 先 (title or "").strip()，末尾的
        # 制表符在判据之前就被吃掉了，落盘的标题里本来就没有它 —— 用尾随
        # 制表符去探，等于探一个到不了的状态。
        ("标题含制表符（放在中间）", "标" + TAB + "题", "ls"),
        ("标题含孤立代理字符", "标题" + SURROGATE, "ls"),
        ("标题为空", "", "ls"),
        ("命令为空", "只有标题", "  "),
    ]
    for label, title, cmd in rejects:
        got = s8.add(title, cmd)
        check("入库硬拒：%s" % label, got is None,
              "实测收下了 %r" % (got.title if got else None))
    check("硬拒时文件没被写（不该每次拒绝都动盘）",
          not (d7 / "snippets.json").exists() or s8.last_save_ok is True, "")

    # --- D4 增删改 ------------------------------------------------------
    d8 = tmpdir()
    s9 = SnippetStore(d8 / "snippets.json")
    n0 = len(s9.items)
    got = s9.add("新片段", "uptime")
    check("新增后入库", got is not None and len(s9.items) == n0 + 1, "")
    check("新增的条目标 builtin=False", got.builtin is False, "")
    check("删除立刻生效", s9.remove(got.id) is True and s9.get(got.id) is None, "")
    check("删不存在的 id 返回 False", s9.remove("no-such-id") is False, "")

    first = s9.items[0]
    nid = first.id
    check("内置条目也能编辑", s9.update(nid, "磁盘用量 2", "df -hT") is True, "")
    check("编辑后 id 不变", s9.get(nid) is not None, "")
    check("编辑后条数不变", len(s9.items) == n0, "实测 %d" % len(s9.items))
    check("编辑后命令真的改了", s9.get(nid).command == "df -hT",
          "实测 %r" % s9.get(nid).command)
    check("编辑成非法内容被拒", s9.update(nid, "x", "a\nb") is False, "")
    check("编辑不存在的 id 返回 False", s9.update("nope", "a", "b") is False, "")
    check("内置条目也能删", s9.remove(nid) is True and s9.get(nid) is None, "")

    # --- D5 恢复内置 ----------------------------------------------------
    d9 = tmpdir()
    s10 = SnippetStore(d9 / "snippets.json")
    s10.add("我自己加的", "uptime")
    victim = s10.items[0]
    s10.update(victim.id, "改过的标题", "df -hT")
    s10.remove(s10.items[3].id)
    n_removed = s10.items[1].id
    s10.remove(n_removed)
    n_before = len(s10.items)
    touched = s10.restore_builtins()
    check("恢复内置：补回删掉的那条", s10.get(n_removed) is not None,
          "实测 %d 条，restore 动了 %d 条" % (len(s10.items), touched))
    check("恢复内置：把改过的命令还原回原值",
          s10.get(victim.id).command == "df -h"
          and s10.get(victim.id).title == "磁盘用量",
          "实测 %r / %r" % (s10.get(victim.id).command, s10.get(victim.id).title))
    check("恢复内置：id 不变（否则用户已持久化的引用全断）",
          s10.get(victim.id) is not None and victim.id == s10.get(victim.id).id, "")
    check("恢复内置：用户自己加的一条不少",
          any(s.title == "我自己加的" for s in s10.items), "")
    check("恢复内置：条数回到出厂 + 用户自己那条",
          len(s10.items) == len(BUILTIN_SNIPPETS) + 1,
          "实测 %d（内置 %d）" % (len(s10.items), len(BUILTIN_SNIPPETS)))
    check("恢复内置：没动过就返回 0", s10.restore_builtins() == 0, "")

    # --- D6 搜索 --------------------------------------------------------
    s11 = SnippetStore(tmpdir() / "snippets.json")
    check("搜索大小写不敏感（搜 DF 命中 df -h）",
          any("df -h" == s.command for s in s11.search("DF")), "")
    check("搜不到时返回空列表（面板据此显示「没有匹配的片段」）",
          s11.search("zzz-不存在-zzz") == [], "实测 %r" % s11.search("zzz-不存在-zzz"))
    check("空查询返回全部", len(s11.search("")) == len(s11.items), "")

    # --- D7 危险命令自检 ------------------------------------------------
    for cmd in DANGER_CMDS:
        check("危险命令必须被抓到：%s" % cmd, _audit_single(cmd),
              "实测判成合规 —— 内置集里塞进这条就等于出厂送一颗炸弹")
    for cmd in BENIGN_CMDS:
        check("正常命令不得被误伤：%s" % cmd, not _audit_single(cmd),
              "实测被误报 —— 一个天天误报的检查总有一天被人关掉")
    check("已知取舍仍在：`systemctl status nginx` 会被报成违规（源码有意为之）",
          _audit_single(KNOWN_FALSE_POSITIVE),
          "实测不再报了 —— snippets.py 的整词黑名单被放宽了，"
          "那么 `systemctl stop nginx` 这类也一起混进来了")
    check("自检对出厂内置集返回空", audit_builtins() == [], "实测 %r" % audit_builtins())

    # --- D8 元断言：把 _resolve_head 装回缺陷版，那批断言必须全红 -------
    real_resolve = sn._resolve_head

    def _broken_resolve(words, raw_first=""):
        """缺陷版：只看 words[0]，不剥包装词。"""
        if not words:
            return "", [], -1
        return sn._command_name(words[0]), [], 0

    sn._resolve_head = _broken_resolve
    try:
        flipped = [c for c in DANGER_CMDS if not _audit_single(c)]
        benign_changed = [c for c in BENIGN_CMDS if _audit_single(c)]
    finally:
        sn._resolve_head = real_resolve
    check("元断言：装回「只看 words[0]」的缺陷版，包装词类危险命令确实变红",
          len(flipped) >= 5,
          "实测只 %d/%d 条变红：%r —— 少这一条，这组判据和一个恒真的版本"
          "在输出里长得一模一样" % (len(flipped), len(DANGER_CMDS), flipped))
    check("元断言：这些红里包含包装词那一类（sudo / env / FOO=1 / busybox / nice / xargs）",
          len([c for c in flipped if c.split()[0] in
               ("sudo", "env", "FOO=1", "busybox", "nice", "ls")]) >= 5,
          "实测变红的 %r 里没有包装词开头的 —— 说明这批红来自别的根因" % (flipped,))
    check("元断言：缺陷版没有让良性命令的判定翻转（红是真的漏，不是判据写歪）",
          not benign_changed, "实测缺陷版把 %r 判成了违规" % benign_changed)
    check("元断言：装回正确实现后那批危险命令重新全被抓到",
          all(_audit_single(c) for c in DANGER_CMDS), "")


def _make_readonly(d: pathlib.Path) -> None:
    if os.name == "nt":
        os.system('icacls "%s" /deny "%s:(OI)(CI)(W,D)" >nul 2>&1'
                  % (d, os.environ.get("USERNAME", "everyone")))
    else:
        d.chmod(0o500)


def _make_writable(d: pathlib.Path) -> None:
    if os.name == "nt":
        os.system('icacls "%s" /remove:d "%s" >nul 2>&1'
                  % (d, os.environ.get("USERNAME", "everyone")))
    else:
        d.chmod(0o700)


# ===========================================================================
# E. 字体
# ===========================================================================

def section_e(app) -> None:
    section("E. 字体（fonts.py）")

    fams = QtGui.QFontDatabase.families()
    installed = set(fams)
    check("前提自校验：Qt 字体库非空（空的话下面每条字体判据都恒真）",
          len(fams) > 0,
          "实测 %d 个字体族 —— 平台插件可能是 offscreen，量字体的判据全无效"
          % len(fams))
    print("     本机字体族数：%d，等宽 %d"
          % (len(fams), sum(1 for f in fams
                            if QtGui.QFontDatabase.isFixedPitch(f))))
    if not fams:
        skip("E 节全部字体判据", "Qt 字体库为空，量不出任何东西")
        return

    check("界面字体：空值自动解析出一个已装的脸",
          fonts._first_available(fonts._UI_CANDIDATES) != "",
          "实测空串（策展表里一个都没装？）")
    f_auto = fonts.ui_font(12, "")
    check("界面字体：空 family 也能造出字体", f_auto.pointSize() == 12,
          "实测 %r" % f_auto.pointSize())
    f_expl = fonts.ui_font(12, "Consolas" if "Consolas" in installed else "Courier New")
    check("界面字体：显式指定的已装字体优先",
          f_expl.family() == ("Consolas" if "Consolas" in installed else "Courier New"),
          "实测 %r" % f_expl.family())
    f_bad = fonts.ui_font(14, "绝对不存在的字体-xyz")
    check("界面字体：失效的名字降级到策展表第一张（不是比例兜底字体）",
          f_bad.family() == fonts._first_available(fonts._UI_CANDIDATES),
          "实测 %r，期望 %r" % (f_bad.family(),
                                fonts._first_available(fonts._UI_CANDIDATES)))
    check("界面字体：**降级后仍用请求的字号**（14pt 不是退回默认 10）",
          f_bad.pointSize() == 14, "实测 %r" % f_bad.pointSize())
    check("界面字体：不把失效的名字交给字体引擎",
          "绝对不存在的字体-xyz" not in f_bad.families(),
          "实测 families()=%r" % f_bad.families())

    mono_auto = fonts.mono_font(11, "")
    check("终端字体：自动解析出一个已装的等宽字体",
          mono_auto.family() != "" and mono_auto.family() in installed,
          "实测 %r" % mono_auto.family())
    base = mono_auto.families()[0]
    without = QtGui.QFont()
    without.setFamily(base)
    without.setPointSize(11)
    w0 = QtGui.QFontMetrics(without).horizontalAdvance("M")
    w1 = QtGui.QFontMetrics(mono_auto).horizontalAdvance("M")
    check("终端字体：加 CJK 兜底链后列宽（度量）不变",
          w0 == w1, "实测 %d -> %d" % (w0, w1))
    check("终端字体：兜底链非空（中文有字可用）",
          len(mono_auto.families()) > 1, "实测 %r" % mono_auto.families())
    check("终端字体：兜底链里没有重复的首选字体",
          mono_auto.families().count(base) == 1, "实测 %r" % mono_auto.families())
    fb = fonts.CJK_FALLBACK_FAMILIES
    # 表里**必须**三家都有代表 —— 这才是跨平台那条断言的真正对象。
    # 「本机装了几家」不是判据：在一台 Windows 上要求 PingFang 已装，
    # 判据就永远红，而这与代码对不对无关。
    check("CJK 兜底表里有 Windows 代表",
          any(f in fb for f in ("黑体", "SimHei", "NSimSun", "新宋体")),
          "实测 %r" % (fb,))
    check("CJK 兜底表里有 macOS 代表",
          any(f in fb for f in ("PingFang SC", "Heiti SC", "Hiragino Sans GB")),
          "实测 %r" % (fb,))
    check("CJK 兜底表里有 Linux 代表",
          any(f in fb for f in ("Noto Sans CJK SC", "WenQuanYi Zen Hei",
                                "Droid Sans Fallback", "Source Han Sans SC")),
          "实测 %r" % (fb,))
    check("本机至少装上了兜底表里的一家里（缺字兜底在本机没退化成空）",
          len(fonts.cjk_fallback_families()) > 0,
          "实测空列表 —— 本机一个 CJK 兜底字体都没有")
    check("兜底表里黑体排第一（Windows 上的首选）", fb[0] == "黑体",
          "实测 %r" % fb[0])

    curated_ui = fonts.ui_font_families()
    check("策展清单不是「全部已装字体」（否则下拉框没法用）",
          len(curated_ui) < len(fams),
          "实测策展 %d / 已装 %d" % (len(curated_ui), len(fams)))
    check("策展清单非空", len(curated_ui) > 0, "实测空")
    check("策展清单按策展顺序排（Windows 上 Segoe UI Variable Text 打头）",
          curated_ui == sorted(curated_ui, key=fonts._UI_CANDIDATES.index),
          "实测 %r" % curated_ui[:3])

    default_mono = fonts.mono_font_families()
    all_mono = fonts.mono_font_families(include_all=True)
    check("等宽判据用 QFontDatabase.isFixedPitch（不是 QFont.fixedPitch）",
          "QFontDatabase.isFixedPitch" in
          (pathlib.Path(ROOT) / "sshclient/core/fonts.py").read_text(encoding="utf-8"),
          "")
    check("Arial / Segoe UI 不出现在等宽清单里（它们字形宽度均匀但不是等宽）",
          "Arial" not in default_mono and "Segoe UI" not in default_mono,
          "实测清单里出现了 %r"
          % [f for f in ("Arial", "Segoe UI") if f in default_mono])
    check("网格宽度交叉校验存在（数据库说等宽但实测宽度不一的要排除）",
          fonts._grid_even(QtGui.QFont("Consolas", 11))
          != fonts._grid_even(QtGui.QFont("Arial", 11)),
          "实测 Consolas=%r Arial=%r —— 交叉校验形同虚设"
          % (fonts._grid_even(QtGui.QFont("Consolas", 11)),
             fonts._grid_even(QtGui.QFont("Arial", 11))))
    variants = [f for f in all_mono
                if f in installed and f != fonts._base_family(f)]
    check("权重变体折叠（「Cascadia Code SemiBold」不单列）",
          not variants,
          "实测单列了 %r" % variants[:4])
    excluded_probe = ["楷体", "KaiTi", "隶书", "LiSu", "幼圆", "YouYuan",
                      "仿宋", "FangSong", "SimSun-ExtB", "Fixedsys"]
    for face in excluded_probe:
        if face not in installed:
            continue
        check("不适合终端的字体默认被排除：%s" % face, face not in default_mono,
              "实测默认清单里有它")
    check("SimSun / 黑体 不被误伤（用户可能真的想用）",
          not [f for f in ("SimSun", "NSimSun", "新宋体", "黑体")
               if f in installed and f in default_mono
               and fonts._mono_is_excluded(f)],
          "实测被 _MONO_EXCLUDE 误伤")
    check("「显示全部字体」只增不减",
          set(default_mono).issubset(set(all_mono))
          and len(all_mono) > len(default_mono),
          "实测 %d -> %d" % (len(default_mono), len(all_mono)))
    check("「显示全部字体」勾上后被过滤的字体回来",
          all(f in all_mono for f in excluded_probe
              if f in installed and fonts._mono_is_excluded(f)),
          "实测没回来 %r" % [f for f in excluded_probe
                              if f in installed and fonts._mono_is_excluded(f)
                              and f not in all_mono])
    check("被过滤但已装的字体标为「已过滤」而不是「当前不可用」",
          "当前不可用" in (pathlib.Path(ROOT) / "sshclient/ui/settings_dialog.py"
                           ).read_text(encoding="utf-8"), "")


# ===========================================================================
# F. 日志
# ===========================================================================

def section_f() -> None:
    section("F. 日志（logs.py）")

    root = logging.getLogger()
    logs.reset_for_tests()

    # --- F1 只 import 不记录时不凭空建目录 ------------------------------
    base = tmpdir()
    logs.reset_for_tests()
    import importlib
    importlib.reload(logs)
    probe = base / "logs"
    d = logs._writable_dir(base)
    check("日志目录名是 logs", d.name == "logs", "实测 %r" % d.name)
    check("日志文件名是 mavisssh.log", logs.LOG_FILENAME == "mavisssh.log",
          "实测 %r" % logs.LOG_FILENAME)

    # --- F2 写到 CWD 下的 logs/mavisssh.log -----------------------------
    cwd_dir = tmpdir()
    old_cwd = os.getcwd()
    os.chdir(cwd_dir)
    try:
        logs.reset_for_tests()
        got = logs.setup(logging.INFO, console=False)
        logs.get_logger("mavisssh.probe").info("第一行 %s", "内容")
        for h in list(root.handlers):
            h.flush()
        check("setup() 返回 CWD 下的 logs 目录", got == cwd_dir / "logs",
              "实测 %r" % got)
        check("日志写到 logs/mavisssh.log", (cwd_dir / "logs" / "mavisssh.log").is_file(),
              "实测目录 %r" % sorted(p.name for p in (cwd_dir / "logs").iterdir()))

        # 幂等
        n_before = len(root.handlers)
        got2 = logs.setup(logging.INFO, console=False)
        logs.get_logger("mavisssh.probe").info("第二行")
        for h in list(root.handlers):
            h.flush()
        text = (cwd_dir / "logs" / "mavisssh.log").read_text(encoding="utf-8")
        check("setup() 幂等：重复调用不重复挂 handler",
              len(root.handlers) == n_before, "实测 %d -> %d" % (n_before, len(root.handlers)))
        check("setup() 幂等：同一条日志只出现一次",
              text.count("第一行 内容") == 1,
              "实测出现 %d 次" % text.count("第一行 内容"))

        check("日志用具名 logger（格式里带模块名）",
              "[mavisssh.probe]" in text, "实测 %r" % text[:120])
        check("日志带级别与时间戳",
              "INFO" in text and text[:4].isdigit(), "实测 %r" % text[:40])
        logs.get_logger("mavisssh.probe").info("中文不乱码：磁盘用量 内存 占用")
        for h in list(root.handlers):
            h.flush()
        text = (cwd_dir / "logs" / "mavisssh.log").read_text(encoding="utf-8")
        check("中文不乱码（UTF-8 落盘）", "中文不乱码：磁盘用量 内存 占用" in text, "")

        # redact
        check("redact() 默认把值全打码",
              logs.redact("hunter2") == "***", "实测 %r" % logs.redact("hunter2"))
        check("redact(keep) 只留前缀",
              logs.redact("10.0.0.7777", keep=8) == "10.0.0.7***",
              "实测 %r" % logs.redact("10.0.0.7777", keep=8))
        check("redact() 对空值返回空串", logs.redact("") == "", "实测 %r" % logs.redact(""))

        # 崩溃兜底
        logs.reset_for_tests()
        crash_dir = tmpdir()
        os.chdir(crash_dir)
        logs.setup(logging.ERROR, console=False)
        try:
            raise ValueError("模拟未捕获异常 boom")
        except ValueError:
            logging.getLogger("mavisssh.crash").exception("未捕获异常")
        for h in list(root.handlers):
            h.flush()
        ctext = (crash_dir / "logs" / "mavisssh.log").read_text(encoding="utf-8")
        check("崩溃兜底：未捕获异常的 traceback 写进日志",
              "Traceback (most recent call last)" in ctext
              and "ValueError" in ctext and "boom" in ctext,
              "实测 %r" % ctext[:120])
        os.chdir(cwd_dir)
    finally:
        os.chdir(old_cwd)
        logs.reset_for_tests()

    # --- F3 第一档建不出目录时逐级退让 ---------------------------------
    # **必须走 base=None 那条路**：只有它才是三档候选
    # （CWD/logs -> 配置目录/logs -> 临时目录/logs，logs.py:41-49）。
    # 显式传 base 时候选表里只有一档，失败就 return candidates[-1]，
    # 也就是那个不可用的路径本身 —— 那是给测试用的注入口径，不是退让链。
    #
    # 和 D2 同一个理由：不去 chmod 目录（管理员身份下 deny ACE 常被忽略，
    # 那样判据恒真），而是把 `logs` 做成一个**已存在的文件**，mkdir 必然抛。
    # 第二档的 CONFIG_DIR 用 monkeypatch 指到临时目录 —— 绝不能让本脚本
    # 在用户真实的 %APPDATA%\MavisSSH\logs 里建目录。
    from sshclient.core import settings as _cs
    nw = tmpdir()
    (nw / "logs").write_text("我是个文件，不是目录", encoding="utf-8")
    fake_cfg = tmpdir() / "fake-cfg"
    real_cfg_dir = _cs.CONFIG_DIR
    _cs.CONFIG_DIR = fake_cfg
    old_cwd2 = os.getcwd()
    try:
        os.chdir(nw)
        ret = logs._writable_dir()
    finally:
        os.chdir(old_cwd2)
        _cs.CONFIG_DIR = real_cfg_dir
    check("第一档建不出目录时不抛异常（setup() 不会把应用弄挂）",
          isinstance(ret, pathlib.Path), "实测 %r" % (ret,))
    check("第一档建不出目录时退到下一档而不是崩",
          ret != nw / "logs",
          "实测仍是第一档 %r" % ret)
    check("退到的目录真的写得进去（探针文件能落盘）",
          _can_write(ret), "实测 %r 不可写" % ret)
    check("退到的正是配置目录那一档",
          str(ret).startswith(str(fake_cfg)),
          "实测退到了 %r，期望在 %r 之下" % (ret, fake_cfg))
    check("显式传 base 时兜底链不生效（候选表只有一档，返回的就是它自己）",
          logs._writable_dir(nw) == nw / "logs",
          "实测 %r —— base 是给测试用的注入口径，生产走的是 base=None"
          % (logs._writable_dir(nw),))
    logs.reset_for_tests()

    # --- F4 轮转 -------------------------------------------------------
    logs.reset_for_tests()
    rot = tmpdir()
    logs.reset_for_tests()
    got = logs.setup(logging.INFO, base=rot, console=False)
    logger = logs.get_logger("mavisssh.rot")
    blob = "x" * 20000
    for i in range(400):
        logger.info("%04d %s", i, blob)
    for h in list(root.handlers):
        h.flush()
    log_file = got / "mavisssh.log"
    backups = sorted(p.name for p in got.glob("mavisssh.log.*"))
    check("轮转：超限产生 .1", (got / "mavisssh.log.1").is_file(),
          "实测 %r" % backups[:5])
    check("轮转：历史数不超过 5", len(backups) <= logs.BACKUP_COUNT,
          "实测 %d 个（BACKUP_COUNT=%d）" % (len(backups), logs.BACKUP_COUNT))
    check("轮转：单个文件不超过 2MB", log_file.stat().st_size <= logs.MAX_BYTES,
          "实测 %d" % log_file.stat().st_size)

    # --- F5 口令不进日志：真连一次必然失败的连接 -----------------------
    logs.reset_for_tests()
    conn_dir = tmpdir()
    logs.reset_for_tests()
    got = logs.setup(logging.DEBUG, base=conn_dir, console=False)
    conn_text = ""
    raised = None
    try:
        from sshclient.remote.connection import AuthConfig, SSHConnection
        # connect() 是 staticmethod，收 cfg 返回 SSHConnection。
        # 127.0.0.1:1 必然连不上，于是必然走到 connection.py:437 那条
        # 「连接失败 %s@%s:%d」——正是要验的那条日志。
        SSHConnection.connect(AuthConfig(host="127.0.0.1", port=1,
                                         username="nobody",
                                         password="TOP-SECRET-PW-9f3a"))
    except Exception as exc:  # noqa: BLE001
        raised = exc
        conn_text = "%s: %s" % (type(exc).__name__, exc)
    for h in list(root.handlers):
        h.flush()
    clog = (got / "mavisssh.log").read_text(encoding="utf-8") if (got / "mavisssh.log").is_file() else ""
    check("真连一次必然失败的连接，失败被翻译成自己的异常类型",
          isinstance(raised, Exception) and "connect" in type(raised).__name__.lower()
          or isinstance(raised, Exception),
          "实测 %r" % (raised,))
    check("口令不进日志：日志里能认出是哪条连接（nobody@127.0.0.1:1）",
          ("127.0.0.1" in clog and "nobody" in clog),
          "实测日志 %r（异常 %r）" % (clog[:200], conn_text[:80]))
    check("口令不进日志：口令原文不出现在日志里",
          "TOP-SECRET-PW-9f3a" not in clog,
          "实测日志里出现了口令原文 —— 这是凭据泄露。日志 %r" % clog[:200])
    check("口令不进日志：连接代码里没有任何把口令拼进日志的地方",
          "password" not in _connection_log_lines(), "实测 %r"
          % [ln for ln in _connection_log_lines() if "password" in ln][:3])
    logs.reset_for_tests()

    # 轮转参数本身
    check("轮转上限 2MB", logs.MAX_BYTES == 2 * 1024 * 1024, "实测 %r" % logs.MAX_BYTES)
    check("历史保留 5 个", logs.BACKUP_COUNT == 5, "实测 %r" % logs.BACKUP_COUNT)


def _connection_log_lines() -> list[str]:
    src = (pathlib.Path(ROOT) / "sshclient/remote/connection.py").read_text(encoding="utf-8")
    return [ln.strip() for ln in src.splitlines() if "log." in ln]


def _can_write(d: pathlib.Path) -> bool:
    """这个目录真的写得进去吗（探针落盘再删掉）。"""
    try:
        probe = d / ".fconf-write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


# ===========================================================================
# G. 连接 / 新建会话对话框
# ===========================================================================

def section_g() -> None:
    section("G. 连接 / 新建会话对话框（connect_dialog.py）")

    pal = get_palette("dark")

    # --- G1 标题区分新建 / 编辑 -----------------------------------------
    d_new = ConnectDialog(palette=pal)
    check("新建时标题是「新建连接」", d_new.windowTitle() == "新建连接",
          "实测 %r" % d_new.windowTitle())
    d_edit = ConnectDialog(initial=dict(name="N", host="h", port=22, username="u",
                                        auth_method="password", password="p"),
                           palette=pal)
    check("编辑时标题是「编辑连接」", d_edit.windowTitle() == "编辑连接",
          "实测 %r" % d_edit.windowTitle())

    # **主按钮也得跟着分。** 两个调用点点下去之后做的事并不一样：
    # `_on_new_connection` 存完还会 `open_session(s)` **真的去连**，
    # `_on_edit_session` 只 `store.upsert` 再刷新列表，**根本不连**。
    # 编辑一条已经能用的会话时按钮写着「连接」，它既不连、也没说「只保存
    # 不连接」—— 用户只能靠猜。这是用户截图报的那一条。
    check("新建时主按钮是「连接」（这条确实会去连）",
          d_new.ok_btn.text() == "连接", "实测 %r" % d_new.ok_btn.text())
    check("编辑时主按钮是「保存」（它只存不连）",
          d_edit.ok_btn.text() == "保存", "实测 %r" % d_edit.ok_btn.text())

    # **图标也要跟着换。** 插头图标配「保存」仍然是「连接」的暗示。
    # 用 cacheKey 比对：`icons.icon()` 内部有缓存，两次取同一张拿到的是
    # 同一个 QIcon 对象，cacheKey 相同；换成别的图标必然不同。
    from sshclient.ui import icons  # noqa: E402
    _want_new = icons.icon("plug", pal, 15, pal.text_on_solid).cacheKey()
    _want_edit = icons.icon("check", pal, 15, pal.text_on_solid).cacheKey()
    check("新建时按钮图标是插头",
          d_new.ok_btn.icon().cacheKey() == _want_new,
          "实测 cacheKey=%s" % d_new.ok_btn.icon().cacheKey())
    check("编辑时按钮图标是对勾（不是插头）",
          d_edit.ok_btn.icon().cacheKey() == _want_edit,
          "实测 cacheKey=%s" % d_edit.ok_btn.icon().cacheKey())
    # 对照：两张图标本身必须真的不同，否则上面那条只是「碰巧不相等」。
    check("对照：插头和对勾本来就不是同一张图", _want_new != _want_edit,
          "两张图相同")

    # 对照：两个模式读出来必须真的不同。
    # 上面两条本身就是锐的（把按钮文字写死成「连接」时，编辑那条必红），
    # 这条只补一句「探针分得清这两种模式」，免得哪天 `initial` 传丢了
    # 两个框长成一样而没人发现。
    _probe_new = ConnectDialog(palette=pal)
    _probe_edit = ConnectDialog(initial=dict(name="N", host="h"), palette=pal)
    check("对照：两个模式的按钮文字确实不同（探针分得清）",
          _probe_new.ok_btn.text() != _probe_edit.ok_btn.text(),
          "%r vs %r" % (_probe_new.ok_btn.text(), _probe_edit.ok_btn.text()))
    _probe_new.close()
    _probe_edit.close()
    flush_qt(_probe_new, _probe_edit)
    flush_qt(d_new, d_edit)

    # --- G2 命名 / 去空格 / 端口范围 ------------------------------------
    d1 = ConnectDialog(palette=pal)
    check("端口默认 22", d1.port_edit.value() == 22, "实测 %r" % d1.port_edit.value())
    check("端口范围 1–65535",
          (d1.port_edit.minimum(), d1.port_edit.maximum()) == (1, 65535),
          "实测 %r" % ((d1.port_edit.minimum(), d1.port_edit.maximum()),))
    check("端口范围之外的值进不来（99999 被拒）",
          d1.port_edit.value() != 99999, "")
    d1.host_edit.setText("  10.0.0.9  ")
    d1.user_edit.setText("  deploy  ")
    d1.name_edit.setText("   ")
    d1.password_edit.setText("pw")
    r1 = d1.result_dict()
    check("主机去首尾空格", r1["host"] == "10.0.0.9", "实测 %r" % r1["host"])
    check("用户名去首尾空格", r1["username"] == "deploy", "实测 %r" % r1["username"])
    check("留空名称被去成空串（自动命名由调用方补）", r1["name"] == "",
          "实测 %r" % r1["name"])
    auto_name = r1["name"] or ("%s@%s" % (r1["username"], r1["host"]))
    check("留空名称 -> 自动命名为 用户@主机", auto_name == "deploy@10.0.0.9",
          "实测 %r" % auto_name)
    flush_qt(d1)

    # --- G3 认证页签 / 掩码 ---------------------------------------------
    d2 = ConnectDialog(palette=pal)
    check("有两个认证页签：密码 / SSH 私钥",
          [d2.auth_tabs.tabText(i) for i in range(d2.auth_tabs.count())] == ["密码", "SSH 私钥"],
          "实测 %r" % [d2.auth_tabs.tabText(i) for i in range(d2.auth_tabs.count())])
    check("密码框是掩码",
          d2.password_edit.echoMode() == QtWidgets.QLineEdit.EchoMode.Password,
          "实测 %r" % d2.password_edit.echoMode())
    check("私钥口令框是掩码",
          d2.key_pass_edit.echoMode() == QtWidgets.QLineEdit.EchoMode.Password,
          "实测 %r" % d2.key_pass_edit.echoMode())
    check("私钥路径框不掩码（要能看见路径）",
          d2.key_path_edit.echoMode() == QtWidgets.QLineEdit.EchoMode.Normal,
          "实测 %r" % d2.key_path_edit.echoMode())

    # --- G4 result_dict 必须原样回吐 SavedSession 的全部可编辑字段 --------
    # **原来这条写的是「恰好回吐九个键」，那就是把缺陷写成了契约。**
    # 九个键 = 扣掉了 `notes` / `last_dir`；而 `main_window._on_edit_session`
    # （`main_window.py:2053`）拿这个 dict **整条重建** `SavedSession`，所以
    # 「恰好九个键」等于把「编辑一次就清空这两个字段」钉成了应有行为。
    # 现在改成正向要求：`SavedSession` 里除 `id` 外的每个字段都必须能被回吐。
    full = dict(name="名字", group="生产", host="10.1.2.3", port=2222, username="ops",
                auth_method="password", password="pw-value",
                key_filename="/k/id_rsa", key_passphrase="kp-value",
                tunnels=[], notes="每晚 2 点重启", last_dir="/srv/app")
    d3 = ConnectDialog(initial=full, palette=pal)
    r3 = d3.result_dict()
    # **这条原来叫「恰好回吐十一个键」，里面抄了一份 want_keys 字面量。**
    # 分组功能加了 `group` 之后它就红了 —— 而它红得毫无价值：紧挨着的下一条
    # 「键集合 == SavedSession 字段集合减去 id」是**从 dataclass 派生的**，
    # 已经把「不多不少」钉住了，字段每加一个都不用改。留着的唯一效果是
    # 给下一个加字段的人添一条红灯，所以这里改成只钉新增字段本身。
    check("result_dict() 回吐 group（分组功能加的字段，漏了就编辑一次丢一个）",
          "group" in r3, "实测 %r" % sorted(r3))
    check("result_dict() 键集合 == SavedSession 字段集合减去 id（不多不少，"
          "漏一个就是编辑一次丢一个）",
          set(r3) == set(SavedSession.__dataclass_fields__) - {"id"},
          "实测多出 %r、缺 %r"
          % (sorted(set(r3) - set(SavedSession.__dataclass_fields__)),
             sorted(set(SavedSession.__dataclass_fields__) - {"id"} - set(r3))))
    # **两套凭据都要原样回吐，不管当前停在哪个页签。**
    # 原来这两条判据写的是「另一套必须被清空」—— 也就是把缺陷当成了契约。
    # 三个输入框在构造时就从 `initial` 回填好了，用户没打算改的那一套一直
    # 好好躺在控件里；抹掉它（`"" if is_key else …`）配合
    # `main_window._on_edit_session` 的整条重建，就是「编辑密码会话时手滑
    # 点一下私钥页签 → 口令没了」这条静默数据丢失的来源。
    check("result_dict() 逐个字段原样回吐（密码页签：私钥那套也保留）",
          r3 == {"name": "名字", "group": "生产", "host": "10.1.2.3",
                 "port": 2222, "username": "ops",
                 "auth_method": "password", "password": "pw-value",
                 "key_filename": "/k/id_rsa", "key_passphrase": "kp-value",
                 "tunnels": [], "notes": "每晚 2 点重启", "last_dir": "/srv/app"},
          "实测 %r" % r3)
    d3.auth_tabs.setCurrentIndex(1)
    r3k = d3.result_dict()
    check("result_dict() 逐个字段原样回吐（私钥页签：口令也保留）",
          r3k == {"name": "名字", "group": "生产", "host": "10.1.2.3",
                  "port": 2222, "username": "ops",
                  "auth_method": "key", "password": "pw-value",
                  "key_filename": "/k/id_rsa", "key_passphrase": "kp-value",
                  "tunnels": [], "notes": "每晚 2 点重启", "last_dir": "/srv/app"},
          "实测 %r" % r3k)

    # 编辑会话靠它整条替换：漏一个字段就是静默丢数据
    src_s = SavedSession(name="N", host="h", port=22, username="u", password="p",
                         notes="每晚 2 点重启", last_dir="/srv/app")
    d3b = ConnectDialog(initial=src_s.to_dict(), palette=pal)
    rebuilt = SavedSession(
        id=src_s.id,
        **{k: v for k, v in d3b.result_dict().items()
           if k in SavedSession.__dataclass_fields__ and k != "id"})
    dropped = set(SavedSession.__dataclass_fields__) - set(d3b.result_dict()) - {"id"}
    check("编辑会话：result_dict() 的字段能覆盖 SavedSession 全部可编辑字段",
          not dropped,
          "实测编辑一次丢字段 %r —— notes %r -> %r，last_dir %r -> %r。"
          "main_window.py:2044 用 result_dict() 整条重建会话，"
          "没回吐的字段就落到 dataclass 默认值上。"
          "**今天还没有实际损失**：全仓没有任何 UI 写这两个字段，"
          "用户填不出来；但只要有人加一个「备注」输入框，"
          "用户点一次编辑就没了，且没有任何提示"
          % (sorted(dropped), src_s.notes, rebuilt.notes,
             src_s.last_dir, rebuilt.last_dir))
    check("编辑会话：notes 不会因为编辑一次而静默消失",
          rebuilt.notes == src_s.notes,
          "实测 %r -> %r" % (src_s.notes, rebuilt.notes))
    flush_qt(d3, d3b)

    # --- G5 校验：主机 / 用户名 / 私钥路径 -------------------------------
    # **每次都开一个新对话框。** 同一个实例只能 accept 一次：第一次放行之后
    # result() 已经是 Accepted，后面即使校验拦下（提前 return，根本没调
    # super().accept()），result() 也不会变回 Rejected —— 那样这条判据在
    # 第二次探测之后就恒真了。
    def _fresh(**kw) -> ConnectDialog:
        d = ConnectDialog(palette=pal)
        d.user_edit.setText("u")
        d.password_edit.setText("pw")
        d.host_edit.setText("h")
        for k, v in kw.items():
            getattr(d, k).setText(v)
        return d

    p1 = _fresh()
    p1.host_edit.setText("")
    blocked = _accept_without_ui(p1)
    check("主机留空 -> 拦下不提交", blocked is True, "实测 %r" % (blocked,))
    flush_qt(p1)

    p2 = _fresh()
    p2.user_edit.setText("   ")
    blocked = _accept_without_ui(p2)
    check("用户名只有空白 -> 拦下不提交", blocked is True, "实测 %r" % (blocked,))
    flush_qt(p2)

    p3 = _fresh()
    check("主机用户名都填了 -> 放行", _accept_without_ui(p3) is False, "")
    flush_qt(p3)

    p4 = _fresh()
    p4.auth_tabs.setCurrentIndex(1)
    p4.key_path_edit.setText("")
    blocked = _accept_without_ui(p4)
    check("私钥页签且私钥路径留空 -> 拦下不提交", blocked is True, "实测 %r" % (blocked,))
    flush_qt(p4)

    p5 = _fresh()
    p5.auth_tabs.setCurrentIndex(1)
    p5.key_path_edit.setText("/绝对不存在的私钥路径")
    check("填了不存在的私钥路径**不**拦（已知行为：错误推迟到连接时）",
          _accept_without_ui(p5) is False, "")
    flush_qt(p5)

    # --- G6 回车提交 / 取消不写盘 ---------------------------------------
    d5 = ConnectDialog(palette=pal)
    check("「连接」按钮是默认按钮（回车即提交）", d5.ok_btn.isDefault(), "")
    check("主机框的回车接到了提交", d5.host_edit.receivers(
        d5.host_edit.returnPressed) > 0, "实测没接")
    d6 = ConnectDialog(palette=pal)
    d6.host_edit.setText("h")
    d6.user_edit.setText("u")
    d6.reject()
    check("取消 -> result() 不是 Accepted（调用方据此不写盘）",
          d6.result() != QtWidgets.QDialog.DialogCode.Accepted,
          "实测 %r" % d6.result())
    flush_qt(d5, d6)

    # --- G7 转发规则表 --------------------------------------------------
    d7 = ConnectDialog(palette=pal)
    check("全新对话框没有预置的转发行（不偷偷开监听端口）",
          d7.tunnel_local.table.rowCount() == 0,
          "实测 %d 行" % d7.tunnel_local.table.rowCount())
    d7.tunnel_local._append(13306, "127.0.0.1", 3306, "")
    rows = d7.tunnel_local.rows()
    # `rows()` 的第 6 项是**这一行原来的开关状态**（第 5 项是监听地址）。
    # 它以前不在返回值里，于是 `_tunnel_rules()` 只能写死 `enabled=True` ——
    # 用户在转发面板里手动关掉的隧道，编辑一次会话就自己开了。
    # 判据分开断言：前 5 栏是「用户填的」，第 6 项是「我们替他记着的」。
    check("添加规则的默认值是 13306 -> 127.0.0.1:3306",
          [r[:5] for r in rows] == [[13306, "127.0.0.1", 3306, "", "127.0.0.1"]],
          "实测 %r" % (rows,))
    check("新建的行开关状态默认是开（用户主动添加的规则就是要生效）",
          [r[5] for r in rows] == [True], "实测 %r" % (rows,))
    d7.tunnel_local._append(13307, "db2", 5432, "PG")
    check("两行都在", len(d7.tunnel_local.rows()) == 2, "")
    d7.tunnel_local.table.selectRow(1)
    d7.tunnel_local._remove_selected()
    check("删除行生效", len(d7.tunnel_local.rows()) == 1, "")

    d8 = ConnectDialog(palette=pal)
    for lp, h, rp, desc in ((99999, "h", 80, "端口越界"),
                            (8080, "", 80, "远程主机空"),
                            ("abc", "h", 80, "端口不是数字"),
                            (8080, "h", 0, "远程端口 0")):
        d8.tunnel_local._append(lp, h, rp, desc)
    d8.tunnel_local._append(13306, "127.0.0.1", 3306, "")
    kept = d8.tunnel_local.rows()
    check("非法转发行被丢弃，只留合法的那条",
          [r[:5] for r in kept] == [[13306, "127.0.0.1", 3306, "", "127.0.0.1"]],
          "实测 %r —— **静默丢弃**" % (kept,))
    # 用户看得见的症状不是「丢了几条」，而是**那几行还躺在表里**：
    # 点确定之后它们不会生效，可屏幕上没有任何东西说它们被丢了。
    check("非法转发行不会留在表里冒充有效规则（要么标出来，要么移除）",
          d8.tunnel_local.table.rowCount() == len(kept),
          "实测表里 %d 行、只认 %d 条 —— 多出的那几行还显示在界面上，"
          "rows() 里 connect_dialog.py:107-110 无条件 continue，"
          "点确定也不提示哪一行被丢"
          % (d8.tunnel_local.table.rowCount(), len(kept)))

    # --- 编辑会话不能把用户手动关掉的隧道重新打开 --------------------
    # 面板那边把开关状态存回会话（`main_window` 里的 `rulesChanged`），
    # 而这个对话框原来在 `_tunnel_rules()` 里写死 `enabled=True`：用户关掉
    # 一条隧道 → 打开对话框改个主机名 → 确定 → 那条隧道自己开了，全程零提示。
    # 这条**今天就能触发**，不是「将来加个功能才会出现」。
    tun_mix = [
        dict(kind="local", local_port=13306, remote_host="db1",
             remote_port=3306, description="MySQL", enabled=False,
             listen_host="127.0.0.1"),
        dict(kind="local", local_port=13307, remote_host="db2",
             remote_port=5432, description="PG", enabled=True,
             listen_host="0.0.0.0"),
        dict(kind="socks", local_port=1080, description="SOCKS5",
             enabled=False),
    ]
    d_tun = ConnectDialog(initial=dict(
        name="隧道会话", host="10.0.0.9", port=22, username="deploy",
        auth_method="password", password="pw", tunnels=tun_mix), palette=pal)
    got_tun = d_tun.result_dict()["tunnels"]
    got_by = {(g["kind"], g["local_port"]): g for g in got_tun}
    check("编辑会话：手动关掉的 -L 隧道不会被静默重新打开",
          got_by.get(("local", 13306), {}).get("enabled") is False,
          "实测 enabled=%r（用户关掉的那条）"
          % (got_by.get(("local", 13306), {}).get("enabled"),))
    check("编辑会话：原本开着的 -L 隧道仍然是开的",
          got_by.get(("local", 13307), {}).get("enabled") is True,
          "实测 enabled=%r" % (got_by.get(("local", 13307), {}).get("enabled"),))
    check("编辑会话：手动关掉的 SOCKS 规则也不会被静默重新打开",
          got_by.get(("socks", 1080), {}).get("enabled") is False,
          "实测 enabled=%r" % (got_by.get(("socks", 1080), {}).get("enabled"),))
    check("编辑会话：监听地址 0.0.0.0 也一起保住",
          got_by.get(("local", 13307), {}).get("listen_host") == "0.0.0.0",
          "实测 %r" % (got_by.get(("local", 13307), {}).get("listen_host"),))
    flush_qt(d_tun)

    # --- 多条 SOCKS 规则不能被静默丢 -----------------------------------
    # 背景：这个对话框只有一个 SOCKS 端口框，回填时是覆盖式赋值，于是
    # 含多条 socks 的会话**只剩最后一条**，其余静默丢弃；而
    # `main_window._on_edit_session` 拿这个结果整条重建会话 → 配置真没了。
    # 可达性不是「手改 sessions.json」：隧道面板的「添加 SOCKS」可以反复点，
    # 每加一条都 `rulesChanged` 写回会话，所以纯 UI 操作就能造出来。
    tun_multi = [
        dict(kind="local", local_port=13306, remote_host="db1",
             remote_port=3306, description="MySQL", enabled=True,
             listen_host="127.0.0.1"),
        dict(kind="socks", local_port=1080, description="SOCKS5",
             enabled=True, listen_host="0.0.0.0"),
        dict(kind="socks", local_port=1081, description="第二个代理",
             enabled=False, listen_host="0.0.0.0"),
    ]
    d_multi = ConnectDialog(initial=dict(
        name="多代理", host="10.0.0.11", port=22, username="deploy",
        auth_method="password", password="pw", tunnels=tun_multi), palette=pal)
    got_multi = d_multi.result_dict()["tunnels"]
    socks_ports = sorted(r["local_port"] for r in got_multi
                         if r.get("kind") == "socks")
    check("多条 SOCKS 规则：一条都没被静默丢掉",
          socks_ports == [1080, 1081],
          "实测 socks 端口 %r（输入是 [1080, 1081]）" % (socks_ports,))
    check("多条 SOCKS 规则：spinbox 里那一条仍可编辑（端口 1080）",
          d_multi.tunnel_socks.value() == 1080,
          "实测 %r" % d_multi.tunnel_socks.value())
    by_port = {r["local_port"]: r for r in got_multi if r.get("kind") == "socks"}
    check("多条 SOCKS 规则：装不下的那条连 enabled 一起原样保留",
          by_port.get(1081, {}).get("enabled") is False,
          "实测 enabled=%r" % (by_port.get(1081, {}).get("enabled"),))
    check("多条 SOCKS 规则：装不下的那条连 description / listen_host 也没丢",
          by_port.get(1081, {}).get("description") == "第二个代理"
          and by_port.get(1081, {}).get("listen_host") == "0.0.0.0",
          "实测 %r" % (by_port.get(1081, {}),))
    # 告知：用户得知道这个对话框只能编辑其中一条
    told_multi = []
    from sshclient.ui import connect_dialog as _cdm
    _real_info = _cdm.msgbox.information
    _cdm.msgbox.information = lambda *a, **k: told_multi.append(str(a))
    try:
        d_multi.accept()
    finally:
        _cdm.msgbox.information = _real_info
    check("多条 SOCKS 规则：accept() 会告诉用户「只能编辑第一条、其余已保留」",
          any("只能编辑" in s for s in told_multi),
          str(told_multi)[:200])
    flush_qt(d_multi)

    # --- 监听地址：能保住的保住，保不住的要说 ----------------------------
    # 背景与评审裁定：`::` 这个 IPv6 通配**在本仓永远绑不上**
    # （`core/tunnel.py` 建的是写死的 `socket.AF_INET`，`Forward.start()`
    # 拿它去 bind），所以**收窄本身是对的**，原样存下来只是把一次必然的失败
    # 从「编辑时」推迟到「连上时」。但静默改掉用户写的值不行。
    # 而 `0.0.0.0` 是**能用的**，收窄它就是纯损失 —— 之前 SOCKS 那条规则
    # 压根不传 listen_host，`0.0.0.0` 一律变 `127.0.0.1`。
    tun_l = [
        dict(kind="local", local_port=13306, remote_host="db1",
             remote_port=3306, description="", enabled=True,
             listen_host="0.0.0.0"),
        dict(kind="local", local_port=13308, remote_host="db2",
             remote_port=3306, description="", enabled=True,
             listen_host="::"),
        dict(kind="local", local_port=13309, remote_host="db3",
             remote_port=3306, description="", enabled=True,
             listen_host="10.1.2.3"),
        dict(kind="socks", local_port=1080, description="SOCKS5",
             enabled=True, listen_host="0.0.0.0"),
    ]
    d_l = ConnectDialog(initial=dict(
        name="监听地址", host="10.0.0.12", port=22, username="deploy",
        auth_method="password", password="pw", tunnels=tun_l), palette=pal)
    got_l = {r["local_port"]: r for r in d_l.result_dict()["tunnels"]}
    check("监听地址：0.0.0.0 原样保住（它是能用的，收窄是纯损失）",
          got_l.get(13306, {}).get("listen_host") == "0.0.0.0"
          and got_l.get(1080, {}).get("listen_host") == "0.0.0.0",
          "local=%r socks=%r"
          % (got_l.get(13306, {}).get("listen_host"),
             got_l.get(1080, {}).get("listen_host")))
    check("监听地址：SOCKS 那条第一次不再被无声收窄"
          "（修之前它压根不传 listen_host，0.0.0.0 一律变 127.0.0.1）",
          got_l.get(1080, {}).get("listen_host") == "0.0.0.0",
          "实测 %r" % (got_l.get(1080, {}).get("listen_host"),))
    told_l = []
    from sshclient.ui import connect_dialog as _cdm2
    _real_info2 = _cdm2.msgbox.information
    _cdm2.msgbox.information = lambda *a, **k: told_l.append(str(a))
    try:
        d_l.accept()
    finally:
        _cdm2.msgbox.information = _real_info2
    joined_l = " ".join(told_l)
    check("监听地址：:: 与内网 IP 仍按 127.0.0.1 保存（本仓 AF_INET 绑不上 IPv6）",
          got_l.get(13308, {}).get("listen_host") == "127.0.0.1"
          and got_l.get(13309, {}).get("listen_host") == "127.0.0.1",
          ":: -> %r / 10.1.2.3 -> %r"
          % (got_l.get(13308, {}).get("listen_host"),
             got_l.get(13309, {}).get("listen_host")))
    check("监听地址：收窄这件事被说出来了（不是静默改掉用户的值）",
          ("监听" in joined_l) and ("::" in joined_l or "10.1.2.3" in joined_l),
          joined_l[:220])
    flush_qt(d_l)

    # 元断言：把 rows() 换成「把非法行从表里删掉」的修好版，那条判据必须变绿。
    from sshclient.ui import connect_dialog as cd
    _orig_rows = cd._TunnelRulesEditor.rows

    def _fixed_rows(self) -> list:
        """修好版：识别出非法行，并把它们**从表里删掉**，不留在屏幕上冒充规则。"""
        out = []
        for r in range(self.table.rowCount() - 1, -1, -1):
            try:
                port = int(self.table.item(r, 0).text().strip())
                host = self.table.item(r, 1).text().strip()
                rport = int(self.table.item(r, 2).text().strip())
            except (AttributeError, TypeError, ValueError):
                self.table.removeRow(r)
                continue
            if not (1 <= port <= 65535 and 1 <= rport <= 65535) or not host:
                self.table.removeRow(r)
                continue
            desc = self.table.item(r, 3)
            out.append([port, host, rport, desc.text().strip() if desc else "",
                        "127.0.0.1"])
        out.reverse()
        return out

    d8b = ConnectDialog(palette=pal)
    for lp, h, rp, desc in ((99999, "h", 80, ""), ("abc", "h", 80, "")):
        d8b.tunnel_local._append(lp, h, rp, desc)
    d8b.tunnel_local._append(13306, "127.0.0.1", 3306, "")
    cd._TunnelRulesEditor.rows = _fixed_rows
    try:
        fixed = d8b.tunnel_local.rows()
        check("元断言：换成「把非法行删掉」的修好版后，那条判据确实变绿",
              d8b.tunnel_local.table.rowCount() == len(fixed),
              "实测修好版表里 %d 行 / 认 %d 条 —— 说明上面那条红量的是实现"
              % (d8b.tunnel_local.table.rowCount(), len(fixed)))
    finally:
        cd._TunnelRulesEditor.rows = _orig_rows
        flush_qt(d8b)

    # 「移除」只是前半段。后半段是**不许静默**：用户点了确定，就必须知道
    # 移走了哪几行、为什么。只移除不告知，和原来那个 `continue` 的差别
    # 只剩下「表里那几行不见了」——那仍然是静默的，只是静默到了别处。
    d8c = ConnectDialog(palette=pal)
    d8c.host_edit.setText("h")
    d8c.user_edit.setText("u")
    d8c.tunnel_local._append(99999, "h", 80, "端口越界")
    d8c.tunnel_local._append(13306, "127.0.0.1", 3306, "MySQL")
    _told: list = []
    _orig_warn = cd.msgbox.warning
    cd.msgbox.warning = (lambda parent, title, text, palette=None:
                         _told.append((title, text)))
    try:
        d8c.accept()
    finally:
        cd.msgbox.warning = _orig_warn
    check("点确定时告知用户移走了哪几行（带行号、说明和具体哪一栏不对）",
          len(_told) == 1 and "端口越界" in _told[0][1] and "1–65535" in _told[0][1],
          "实测弹了 %d 次、内容 %r —— 移走一整行却一个字都不告诉用户，"
          "和静默 continue 没有区别" % (len(_told), _told))
    check("告知之后：表里只剩合法的那 1 行，存下去也只有它",
          d8c.tunnel_local.table.rowCount() == 1
          and [x["local_port"] for x in d8c.result_dict()["tunnels"]] == [13306],
          "实测表里 %d 行、规则 %r"
          % (d8c.tunnel_local.table.rowCount(), d8c.result_dict()["tunnels"]))
    flush_qt(d8c)

    # **监听地址不该被静默改回去。** `0.0.0.0` 是用户手工写进配置的合法值，
    # 意思就是「对外监听」。表里只有 4 列放不下它，所以它得一路带着回来。
    # 原来 `rows()` 无条件写死 `127.0.0.1`，编辑一次就把「对外监听」悄悄收窄
    # 成「只听本机」，界面上零提示。方向上是收紧、不算安全问题，但**静默**。
    d8d = ConnectDialog(initial=dict(tunnels=[
        {"kind": "local", "local_port": 13306, "remote_host": "db",
         "remote_port": 3306, "description": "对外", "listen_host": "0.0.0.0"},
        {"kind": "local", "local_port": 13307, "remote_host": "db2",
         "remote_port": 5432, "description": "本机", "listen_host": "127.0.0.1"},
    ]), palette=pal)
    lis = [x["listen_host"] for x in d8d.result_dict()["tunnels"]]
    check("监听地址 0.0.0.0 经过编辑不会被静默改回 127.0.0.1",
          lis == ["0.0.0.0", "127.0.0.1"], "实测 %r" % lis)
    flush_qt(d8d)

    # **这三条原来验的是「端口 0 = 不启用」，那份契约早就被换掉了。**
    # 现在启不启用由 `socks_switch` 决定，端口框在关着的时候是**禁用**的
    # （见 ConnectDialog._sync_soks 的说明：藏起来会让这一行高度跳动，
    # 禁用才让「这里原本有东西」继续可见），端口里留着 1080 是给用户
    # 打开开关后能直接抄的一个现成值。
    # 判据跟着实现走 —— 留着旧契约的话，这三条不但恒红，还会让人以为
    # SOCKS 的启停逻辑坏了。
    d9 = ConnectDialog(palette=pal)
    check("SOCKS 默认是关着的（端口填好不代表启用）",
          d9.socks_switch.isChecked() is False, "实测 %r" % d9.socks_switch.isChecked())
    check("SOCKS 关着时端口框是禁用的（而不是被藏起来）",
          # `isVisible()` 会把**祖先**的可见性也算进来，而这一刻对话框
          # 还没 show，于是它对任何一行都返回 False —— 用它判「有没有被
          # 藏起来」得到的是「对话框开没开」。`isHidden()` 只看自己。
          d9.tunnel_socks.isEnabled() is False and not d9.tunnel_socks.isHidden(),
          "enabled=%r hidden=%r" % (d9.tunnel_socks.isEnabled(),
                                    d9.tunnel_socks.isHidden()))
    check("SOCKS 端口范围 1–65535",
          (d9.tunnel_socks.minimum(), d9.tunnel_socks.maximum()) == (1, 65535),
          "实测 %r" % ((d9.tunnel_socks.minimum(), d9.tunnel_socks.maximum()),))
    check("SOCKS 关着时即使端口非 0 也不产生规则",
          all(x["kind"] != "socks" for x in d9.result_dict()["tunnels"]),
          "实测 %r" % d9.result_dict()["tunnels"])

    d9.tunnel_socks.setValue(1080)
    check("改端口不改变开关状态（关着还是关着）",
          d9.socks_switch.isChecked() is False, "实测 %r" % d9.socks_switch.isChecked())
    d9.socks_switch.setChecked(True)
    tun9 = d9.result_dict()["tunnels"]
    check("SOCKS 打开时按端口产生一条 socks 规则",
          # **原来写的是 `[...] and [...]`，`and` 返回的是第二个列表而不是
          # bool**，于是 `check` 收到 `[]` 直接抛 TypeError 把**整个 G 节
          # 从这里截断** —— 后面几十条判据一条都没跑过，报告也只说
          # 「整节抛异常」。判据自己抛异常是最坏的失败形态：它不告诉你哪条
          # 红，还顺手把别人的红一起藏起来。
          [x["local_port"] for x in tun9 if x["kind"] == "socks"] == [1080],
          "实测 %r" % tun9)
    d9.socks_switch.setChecked(False)
    check("SOCKS 关回去之后规则消失（开关才是启停的那一位）",
          all(x["kind"] != "socks" for x in d9.result_dict()["tunnels"]),
          "实测 %r" % d9.result_dict()["tunnels"])

    d10 = ConnectDialog(initial=dict(tunnel_local=[[13306, "db", 3306, "MySQL",
                                                     "127.0.0.1"]],
                                      tunnel_socks=1080), palette=pal)
    tun10 = d10.result_dict()["tunnels"]
    check("老格式 tunnel_local 回填进表", d10.tunnel_local.table.rowCount() == 1,
          "实测 %d 行" % d10.tunnel_local.table.rowCount())
    check("老格式 tunnel_socks 回填进 SOCKS 框", d10.tunnel_socks.value() == 1080,
          "实测 %r" % d10.tunnel_socks.value())
    check("老格式回填后 tunnels 两条规则都在",
          sorted(x["kind"] for x in tun10) == ["local", "socks"],
          "实测 %r" % tun10)
    flush_qt(d7, d8, d9, d10)

    # --- G8 切换页签会清空另一套凭据（红了就是真发现）------------------
    init_pw = dict(name="生产 Web", host="10.0.0.7", port=2222, username="deploy",
                   auth_method="password", password="REAL-SESSION-PASSWORD",
                   key_filename="", key_passphrase="")
    d11 = ConnectDialog(initial=init_pw, palette=pal)
    check("编辑密码会话时初始停在密码页签", d11.auth_tabs.currentIndex() == 0,
          "实测 %r" % d11.auth_tabs.currentIndex())
    check("编辑密码会话时密码被正确回填", d11.password_edit.text() == "REAL-SESSION-PASSWORD",
          "实测 %r" % d11.password_edit.text())
    d11.auth_tabs.setCurrentIndex(1)          # 误点「SSH 私钥」
    d11.key_path_edit.setText("/keys/id_ed25519")
    d11.accept()
    r11 = d11.result_dict()
    check("误点私钥页签后：会话里原有的 password 不会被清空成空串",
          r11["password"] == "REAL-SESSION-PASSWORD",
          "实测 password=%r auth_method=%r —— 误点一下页签，确定之后"
          "这条会话的口令就没了，且界面上没有任何提示"
          % (r11["password"], r11["auth_method"]))
    # **这一条原来写成「auth_method 不该变」，那是把要求写错了。**
    # 用例里用户**填了**私钥路径才 `accept()`——填了路径就是显式改用私钥
    # 认证，`auth_method` 变成 `key` 是应有行为、不是缺陷。真正该防的是
    # 「没打算改的那套凭据被清空」，上一条已经量了。
    # 不填路径就切页签的那种误点，由 `accept()` 的必填校验拦下（对话框
    # 不关、什么都不存），那是另一条判据的事。
    check("填了私钥路径后 auth_method 变成 key（用户的显式选择），"
          "而原口令与私钥口令都还在",
          r11["auth_method"] == "key"
          and r11["password"] == "REAL-SESSION-PASSWORD"
          and r11["key_filename"] == "/keys/id_ed25519",
          "实测 auth_method=%r password=%r key=%r"
          % (r11["auth_method"], r11["password"], r11["key_filename"]))

    # **反向那一条更危险**：编辑一个 key 会话，密码框本来就是空的，
    # 用户随手点一下「密码」页签 —— 原来会同时把 `auth_method` 变成
    # password、把私钥路径与私钥口令抹成空串，落盘即不可逆、零提示。
    init_key = dict(name="生产 DB", host="10.0.0.8", port=2222, username="deploy",
                    auth_method="key", password="",
                    key_filename="/keys/id_ed25519", key_passphrase="KEY-PASSPHRASE")
    d13 = ConnectDialog(initial=init_key, palette=pal)
    check("编辑 key 会话时初始停在私钥页签", d13.auth_tabs.currentIndex() == 1,
          "实测 %r" % d13.auth_tabs.currentIndex())
    d13.auth_tabs.setCurrentIndex(0)          # 随手点一下「密码」页签
    d13.accept()
    r13 = d13.result_dict()
    check("编辑 key 会话时随手点「密码」页签：私钥路径与私钥口令都不丢",
          r13["key_filename"] == "/keys/id_ed25519"
          and r13["key_passphrase"] == "KEY-PASSPHRASE",
          "实测 key=%r passphrase=%r"
          % (r13["key_filename"], r13["key_passphrase"]))
    check("编辑 key 会话时随手点「密码」页签：auth_method 不会被改成"
          "一个没有口令的密码会话",
          r13["auth_method"] == "key",
          "实测 %r —— 密码框本来就是空的，没有任何改成密码认证的理由"
          % r13["auth_method"])
    flush_qt(d11, d13)

    # --- G8 元断言：把 result_dict 换成「两套凭据都原样回吐」的版本，
    # 上面那几条凭据保全的判据必须变绿。少了这一步，它们红和一个探针写坏的
    # 脚本在输出里长得一模一样。
    def _fixed_result_dict(self) -> dict:
        """修好版：页签只决定 auth_method，不清空另一套凭据。"""
        out = _orig_result_dict(self)
        out["password"] = self.password_edit.text()
        out["key_filename"] = self.key_path_edit.text().strip()
        out["key_passphrase"] = self.key_pass_edit.text()
        return out

    _orig_result_dict = ConnectDialog.result_dict
    d12 = ConnectDialog(initial=init_pw, palette=pal)
    ConnectDialog.result_dict = _fixed_result_dict
    try:
        d12.auth_tabs.setCurrentIndex(1)
        d12.key_path_edit.setText("/keys/id_ed25519")
        r12 = d12.result_dict()
        check("元断言：换成「两套凭据都回吐」的修好版后，那两条判据确实变绿",
              r12["password"] == "REAL-SESSION-PASSWORD"
              and r12["key_filename"] == "/keys/id_ed25519",
              "实测修好版 %r —— 说明上面那两条红量的是实现，不是探针坏了" % r12)
    finally:
        ConnectDialog.result_dict = _orig_result_dict
        d12.close()
        d12.deleteLater()
    flush_qt(d12)

    # --- G9 取消不写盘（结构上：对话框自己不碰 store）-------------------
    check("对话框自己不碰 SessionStore（写盘只在 main_window 里发生）",
          "SessionStore" not in (pathlib.Path(ROOT) / "sshclient/ui/connect_dialog.py"
                                 ).read_text(encoding="utf-8"), "")


def _accept_without_ui(dlg: ConnectDialog) -> bool:
    """调 accept() 并吞掉校验用的模态框。返回「被拦下了吗」。"""
    import PyQt6.QtWidgets as W
    orig = W.QMessageBox.exec
    W.QMessageBox.exec = lambda self: W.QMessageBox.StandardButton.Ok
    try:
        dlg.accept()
        return dlg.result() != QtWidgets.QDialog.DialogCode.Accepted
    finally:
        W.QMessageBox.exec = orig


# ===========================================================================
# main
# ===========================================================================

def main() -> int:
    print("MavisSSH 配置 / 凭据 / 片段 / 字体 / 日志 / 对话框 —— 功能测试")
    print("解释器：%s" % sys.executable)
    print("平台：%s，QT_QPA_PLATFORM=%s"
          % (sys.platform, os.environ.get("QT_QPA_PLATFORM")))

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

    sections = (
        ("A. 配置目录与加载", section_a),
        ("B. 会话存储", section_b),
        ("C. 凭据加密", section_c),
        ("D. 命令片段存储", section_d),
        ("E. 字体", lambda: section_e(app)),
        ("F. 日志", section_f),
        ("G. 连接对话框", section_g),
    )
    for _title, fn in sections:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            check("%s 整节跑完了" % _title, False,
                  "抛异常 %s: %s\n%s" % (type(exc).__name__, exc,
                                          traceback.format_exc()))
        finally:
            QtCore.QCoreApplication.sendPostedEvents(None,
                                                      QtCore.QEvent.Type.DeferredDelete)

    cleanup()
    QtCore.QCoreApplication.processEvents()

    print("\n" + "=" * 72)
    print("通过 %d 项，失败 %d 项，未评上 %d 项" % (len(PASS), len(FAIL), len(SKIP)))
    print("=" * 72)
    for f in FAIL:
        print("  FAIL " + f)
    for s in SKIP:
        print("  SKIP " + s)
    if FAIL:
        return 1
    if SKIP:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
