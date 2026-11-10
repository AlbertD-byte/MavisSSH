"""可移植性：在 macOS / Linux 上会不会静默失效或直接崩。

这台机器只有 Windows，所以这里**不模拟**别的系统——模拟出来的绿灯没有意义。
真正要守的是那些「不报错、只是功能没了」的地方，因为它们在任何平台上都不会
变红，只会在用户换机器那天才发现：

  1. 日志目录在 CWD 不可写时要有退路（macOS 从 .app 启动时 CWD 是 `/`，
     直接 `mkdir /logs` 权限拒绝，而那行抛在 QApplication 建好之前 = 应用起不来）
  2. 测试字体目录不能写死 `C:\\Windows\\Fonts`（写死的话 mac/Linux 上
     families() 返回 0，**所有**截图断言变成恒真）
  3. CJK 兜底字体表三家都要有代表，否则缺字兜底在另两家上静默失效
  4. lrzsz 在 POSIX 上不能去执行仓库里那个 Windows PE 二进制
  5. 配置目录三条分支都给出合法路径
"""
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(ROOT))

FAIL: list[str] = []
SRC = ROOT / "sshclient"


def check(name, cond, extra=""):
    print(f'  {"PASS" if cond else "FAIL"}  {name}' + (f"  ({extra})" if extra else ""))
    if not cond:
        FAIL.append(name)


def read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


# 字体相关的断言要真的问 QFontDatabase，所以必须先有 QApplication。
# tests.fonts 在 import 时就设好了 QT_QPA_PLATFORM，boot() 负责建应用。
from tests.fonts import _font_dirs, boot

app = boot([])


print("=" * 70)
print("A. 日志目录在不可写的 CWD 下必须有退路")
print("=" * 70)

from unittest import mock

from sshclient.core import logs

blocked = pathlib.Path(tempfile.mkdtemp(prefix="mavisssh_ro_"))
# 用「父路径是一个普通文件」来强制 mkdir 失败。这个在所有平台都成立；
# `os.chmod(dir, 0o500)` 那套是 POSIX 语义，在 Windows 上根本挡不住写入，
# 用它当前提只会得到一个「测过了」的假结论。
blocker = blocked / "i-am-a-file"
blocker.write_text("x", encoding="utf-8")

# 显式传 base 是给测试用的：那就**按原路径返回**，不该偷偷跑去写用户配置目录。
logs.reset_for_tests()
got = logs._writable_dir(base=blocker)
check("显式 base 下不抛异常", got == blocker / "logs", str(got))

# 真正要守的是默认那档：CWD 不可用时必须有下一级可去。
# 这里 patch Path.cwd 而不是去删进程 CWD —— Windows 不允许删除正在使用的
# 目录，那样测出来的还是「Windows 上没出事」，不是「退路存在」。
logs.reset_for_tests()
with mock.patch.object(pathlib.Path, "cwd", return_value=blocker):
    got = logs._writable_dir()
    check("CWD 不可用时不抛异常", True, str(got))
    check("退到了别处而不是死磕 CWD", got != blocker / "logs", str(got))
    check("退路本身可写", os.access(got, os.W_OK), str(got))
    check("退路就是配置目录那一档", got.name == "logs" and "MavisSSH" in str(got), str(got))

# setup() 整条路也不能炸——它抛在 QApplication 建好之前就等于应用起不来
logs.reset_for_tests()
with mock.patch.object(pathlib.Path, "cwd", return_value=blocker):
    try:
        d = logs.setup(console=False)
        check("CWD 不可用时 setup() 不抛异常", True, str(d))
    except Exception as exc:  # noqa: BLE001
        check("CWD 不可用时 setup() 不抛异常", False, f"{type(exc).__name__}: {exc}")
logs.reset_for_tests()

import inspect

check("默认调用链有多级退路（CWD -> 配置目录 -> 临时目录）",
      inspect.getsource(logs._writable_dir).count("candidates.append") >= 3,
      str(inspect.getsource(logs._writable_dir).count("candidates.append")))

print()
print("=" * 70)
print("B. 测试字体目录是按平台算的，不是写死 Windows")
print("=" * 70)

fonts_src = (ROOT / "tests" / "fonts.py").read_text(encoding="utf-8")
check("没有硬编码 C:\\Windows\\Fonts", r"C:\Windows\Fonts" not in fonts_src)
check("有 darwin 分支", 'darwin' in fonts_src)
for d in ("/System/Library/Fonts", "/usr/share/fonts", ".local"):
    check(f"包含 {d}", d in fonts_src)

from tests.fonts import _font_dirs  # noqa: F811

dirs = _font_dirs()
check("本机至少找到一个字体目录", len(dirs) > 0, f"{len(dirs)} 个")
check("列出来的目录都真实存在", all(os.path.isdir(d) for d in dirs),
      str([d for d in dirs if not os.path.isdir(d)]))

print()
print("=" * 70)
print("C. CJK 兜底字体三家都有代表")
print("=" * 70)

from sshclient.core import fonts as F

families = set(F.CJK_FALLBACK_FAMILIES)
check("Windows 有代表", bool(families & {"黑体", "SimHei", "NSimSun", "新宋体"}))
check("macOS 有代表", bool(families & {"PingFang SC", "Heiti SC", "Hiragino Sans GB"}),
      str(sorted(families)[:6]))
check("Linux 有代表",
      bool(families & {"Noto Sans CJK SC", "Noto Sans SC", "WenQuanYi Zen Hei"}))
check("黑体仍在最前（Windows 上是首选）", F.CJK_FALLBACK_FAMILIES[0] == "黑体",
      F.CJK_FALLBACK_FAMILIES[0])
check("没有重复项", len(F.CJK_FALLBACK_FAMILIES) == len(families))
check("本机兜底不为空（Windows 应当有黑体）", len(F.cjk_fallback_families()) > 0,
      str(F.cjk_fallback_families()))

print()
print("=" * 70)
print("D. 界面与终端首选字体表也覆盖三家")
print("=" * 70)

ui = set(F._UI_CANDIDATES)
mono = set(F._MONO_CANDIDATES)
check("界面表有 macOS 的 PingFang SC", "PingFang SC" in ui)
check("界面表有 Linux 的 Noto Sans / DejaVu",
      bool(ui & {"Noto Sans", "DejaVu Sans"}), str(sorted(ui)[:5]))
check("终端表有 macOS 的 Menlo", "Menlo" in mono)
check("终端表有 Linux 的 DejaVu Sans Mono", "DejaVu Sans Mono" in mono)
check("终端表仍有 Windows 的 Consolas", "Consolas" in mono)

print()
print("=" * 70)
print("E. lrzsz 在 POSIX 上不会去执行 Windows 的 .exe")
print("=" * 70)

from sshclient.core import lrzsz

orig = sys.platform
try:
    sys.platform = "linux"
    names = lrzsz._candidate_names("rz")
    check("POSIX 候选名里没有 .exe", not any(n.endswith(".exe") for n in names), str(names))
    check("POSIX 优先 lrz", names[0] == "lrz", str(names))
    sys.platform = "darwin"
    names = lrzsz._candidate_names("sz")
    check("macOS 同样排除 .exe", not any(n.endswith(".exe") for n in names), str(names))
    sys.platform = "win32"
    names = lrzsz._candidate_names("rz")
    check("Windows 仍然认 .exe", "rz.exe" in names, str(names))
    check("Windows 也认 lrz.exe", "lrz.exe" in names, str(names))
finally:
    sys.platform = orig

# 仓库里那个 bin/rz.exe 确实是 Windows PE —— 顺手证明「它不该被 POSIX 选中」
pe = ROOT / "bin" / "rz.exe"
if pe.exists():
    magic = pe.read_bytes()[:2]
    check("bin/rz.exe 确实是 Windows PE（所以 POSIX 必须绕开它）",
          magic == b"MZ", repr(magic))

print()
print("=" * 70)
print("F. 配置目录三分支都给出合法路径")
print("=" * 70)

from sshclient.core import settings as S

seen = {}
orig_plat = sys.platform
# macOS 分支走的是 ``Path.home() / "Library/Application Support"``，
# **根本不看 APPDATA 也不看 XDG_CONFIG_HOME**，所以光改那两个环境变量还
# 拦不住它——必须连"家目录在哪儿"一起拨走，否则它算出来的还是真实的
# ~/Library/Application Support/MavisSSH。
_orig_env2 = {k: os.environ.get(k)
              for k in ("APPDATA", "XDG_CONFIG_HOME", "USERPROFILE")}
for plat in ("win32", "darwin", "linux"):
    d = ROOT / "_portable_cfg" / plat
    d.mkdir(parents=True, exist_ok=True)
    os.environ["APPDATA"] = str(d)
    os.environ["XDG_CONFIG_HOME"] = str(d)
    os.environ["USERPROFILE"] = str(d)
    sys.platform = plat
    try:
        seen[plat] = S._config_dir()
    finally:
        sys.platform = orig_plat
        for k, v in _orig_env2.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    for plat, p in seen.items():
        check(f"{plat} 得到一个绝对路径", p.is_absolute(), str(p))
        check(f"{plat} 指向 MavisSSH 目录", p.name == "MavisSSH", p.name)
        # 三条都必须落在仓库内的临时目录里。**这条是下面那段 rmtree 的保险丝**：
        # 哪条路径跑到了真实用户目录上，这里当场红。
        check(f"{plat} 路径落在仓库临时目录里（不是真实用户配置）",
              str(p).startswith(str(ROOT / "_portable_cfg")), str(p))
check("三个平台的路径确实不同", len(set(str(p) for p in seen.values())) == 3,
      str({k: str(v) for k, v in seen.items()}))

print()
print("=" * 70)
print("F. 片段库自己的 _config_dir() 也有三平台分支")
print("=" * 70)
# 上一条只测了 settings._config_dir()。片段库**自己抄了一份**三平台分支
# （不共用 settings 的常量是有意的：免得为一个 store 把 crypto 一起拉进来），
# 于是这一份当时是裸奔的 —— 而"平台分支写漏一个"恰恰是本项目栽过的坑：
# POSIX 上字体目录找不到 -> QFontDatabase 返回 0 -> 所有"有没有墨迹"的
# 断言恒真，一套悄悄什么都没测的测试比没有测试更糟。
from sshclient.core import snippets

src_sn = (ROOT / "sshclient" / "core" / "snippets.py").read_text(encoding="utf-8")
for plat in ("win32", "darwin", "linux"):
    check(f"片段库有 {plat} 分支", plat in src_sn)
check("片段库用了 XDG_CONFIG_HOME", "XDG_CONFIG_HOME" in src_sn)
check("片段库用了 macOS 的 Application Support",
      "Application Support" in src_sn)
check("片段库用了 APPDATA", "APPDATA" in src_sn)
check("分支清单和 _PLATFORM_BRANCHES 声明对得上（新增平台记得补标记）",
      all(p in getattr(snippets, "_PLATFORM_BRANCHES", ()) for p in
          ("win32", "darwin", "linux")),
      str(getattr(snippets, "_PLATFORM_BRANCHES", "没有这个标记")))
check("片段库没写死 C:\\Users 或 C:\\Windows",
      "C:\\Users" not in src_sn and "C:\\Windows" not in src_sn)

# 三平台各跑一遍 _config_dir()：不能抛，且都要落在 MavisSSH 目录下
import tempfile

_snip_seen = {}
_orig_plat = sys.platform
_orig_env = {k: os.environ.get(k)
             for k in ("APPDATA", "XDG_CONFIG_HOME", "USERPROFILE")}
for plat in ("win32", "darwin", "linux"):
    d = ROOT / "_portable_snip" / plat
    d.mkdir(parents=True, exist_ok=True)
    # USERPROFILE 也要拨：片段库的 darwin 分支走 Path.home()，和上面
    # settings 那段是同一个坑。
    os.environ["USERPROFILE"] = str(d)
    os.environ["APPDATA"] = str(d)
    os.environ["XDG_CONFIG_HOME"] = str(d)
    sys.platform = plat
    try:
        _snip_seen[plat] = snippets._config_dir()
    finally:
        sys.platform = _orig_plat
        for k, v in _orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
for plat, p in _snip_seen.items():
    check(f"片段库在 {plat} 上得到一个绝对路径", p.is_absolute(), str(p))
    check(f"片段库在 {plat} 上指向 MavisSSH 目录", p.name == "MavisSSH", p.name)
    check(f"片段库在 {plat} 上的路径落在仓库临时目录里",
          str(p).startswith(str(ROOT / "_portable_snip")), str(p))
check("片段库三个平台的路径确实不同",
      len(set(str(p) for p in _snip_seen.values())) == 3,
      str({k: str(v) for k, v in _snip_seen.items()}))

# _config_dir() 内部会 mkdir(parents=True)，而 macOS 分支在本机解析到
# ~/Library/Application Support —— **真会在用户主目录下建出目录**。
# 测完自己收拾干净，别在别人机器上留一堆 MavisSSH 空壳。
# 注意要连父目录一起收：只 rmtree(".../MavisSSH") 会留下空的
# "Application Support" 和 "Library"，而 ~/Library 平时根本不该存在。
import shutil
from pathlib import Path

_leftover = {str(p) for p in list(_snip_seen.values()) + list(seen.values())}
_leftover.add(str(ROOT / "_portable_snip"))
_leftover.add(str(ROOT / "_portable_cfg"))
# **保险丝：只删仓库里的这两个临时目录，其余一律不碰。**
# 原来这里是"把上面算出来的每条路径 rmtree 掉"——而上面算出来的其中一条
# 就是**用户真实的 %APPDATA%\MavisSSH**（只拨了 sys.platform、没拨环境变量）。
# 于是跑一次全量回归就把用户的配置目录整个删掉，ignore_errors=True 还让它
# 一声不响。删东西的地方，宁可多判一次也不能"应该不会走到那儿吧"。
_ALLOWED_PREFIXES = (str(ROOT / "_portable_snip"), str(ROOT / "_portable_cfg"))
_refused = sorted(p for p in _leftover
                  if not any(p.startswith(pre) for pre in _ALLOWED_PREFIXES))
if _refused:
    # 只拒删越界的那几条，**其余照常清理**——一刀切全不删的话，仓库里会
    # 留一堆空壳目录，比原来还糟。
    print("  [FAIL] 拒绝删除仓库临时目录之外的路径：%s" % "、".join(_refused))
    FAIL.append("拒绝删除仓库临时目录之外的路径")
for p in sorted(_leftover):
    if p in _refused:
        continue
    shutil.rmtree(p, ignore_errors=True)
    parent = Path(p).parent
    # 只往上收那些"明显是为了这次测试才冒出来"的中间层
    while parent.name in ("MavisSSH", "Application Support", "Library",
                          "_portable_snip", "_portable_cfg"):
        try:
            parent.rmdir()          # 非空就抛，抛了就停
        except OSError:
            break
        parent = parent.parent
check("测完没在用户主目录下留下 Library 空壳",
      not (Path.home() / "Library" / "Application Support" / "MavisSSH").exists(),
      str(Path.home() / "Library"))
check("测完没在仓库里留下临时目录", not (ROOT / "_portable_snip").exists())

print()
print("=" * 70)
if FAIL:
    print(f"FAILURES ({len(FAIL)}):")
    for n in FAIL:
        print("  -", n)
    sys.exit(1)
print("RESULT: PASS -- 关键路径都不再写死 Windows")
