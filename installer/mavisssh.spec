"""PyInstaller 打包配置，三个平台共用一份。

**关键前提：PyInstaller 不能交叉编译。** Windows 的 .exe 只能在 Windows 上打，
Linux 的二进制只能在 Linux 上打，macOS 的 .app 只能在 macOS 上打。所以这里
写的是**同一份 spec 的三个平台分支**，不是"一个脚本打三个包"。跨平台要用
CI（GitHub Actions 的 matrix 或类似）分别跑。

平台差异都在 ``IS_*`` 三个开关上：

- **Windows**：默认 ``onedir`` 而不是 ``onefile``——单文件版每次启动都要把
  Qt（约 60MB）解压到临时目录，冷启动以秒计；目录版是"复制过去就能跑"。
- **Windows** 想出**自包含的单个 exe**，用 ``installer/build.py --onefile``。
  **默认的 onedir 产物不是自包含的**：``MavisSSH.exe`` 单独拷走一定起不来，
  实测会弹系统的 "Error" 对话框（缺 ``_internal`` 里的 DLL），用户看到的就是
  "窗口一闪就没 / 缺依赖"。要分发单个 exe 就必须加 ``--onefile``。
- **macOS**：出 ``.app`` bundle（``BUNDLE``），图标是 ``.icns``。
- **Linux**：默认出 ``onedir``。

单文件模式由环境变量 ``MAVISSSH_ONEFILE=1`` 传进来（``build.py`` 设），**不是
命令行上的 ``--onefile``** —— spec 是自己写的、写死了 ``COLLECT``，PyInstaller
不会替用户把它拆成"二进制全部塞进 EXE"。两处必须同时改，只改一边会打出一个
"界面能起来但功能全缺"的包。

数据文件：仓库自带的 ``bin/rz.exe`` / ``bin/sz.exe`` / ``msys-2.0.dll`` 必须
跟着走，否则 lrzsz 传输会在打包后静默失效（``lrzsz.resolve_binary`` 找不到二进制。**只打包当前平台能用的那一份**——POSIX 上是 .exe，PyInstaller 的
``datas`` 是无条件复制的，不做判断。
"""
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

SPEC_ROOT = Path(SPECPATH).resolve()          # PyInstaller 注入的变量
REPO = SPEC_ROOT.parent if SPEC_ROOT.name == "installer" else SPEC_ROOT

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

#: 单文件模式。**由 build.py 通过环境变量传进来**（spec 是普通 Python，
#: 执行时能读 os.environ），不要指望命令行上的 ``--onefile`` —— spec 里
#: 写死了 ``COLLECT``，而单文件要的是「把二进制也塞进 EXE 的那种」，
#: PyInstaller 不会替你把 COLLECT 拆掉。实测原来那条 ``--onefile`` 是**空转**，
#: 打出来的还是目录版，文档里却写着能出单文件。
ONEFILE = os.environ.get("MAVISSSH_ONEFILE") == "1"

# PyQt6 自己的插件（platforms/、imageformats/…）不收的话，打出来的程序
# 一起身就 "could not find or load the Qt platform plugin windows"。
hiddenimports = collect_submodules("paramiko") + collect_submodules("cryptography")

# bin/ 里只带当前平台跑得起来的那几个文件
_datas = []
bin_dir = REPO / "bin"
if bin_dir.is_dir():
    wanted = ("rz.exe", "sz.exe", "msys-2.0.dll") if IS_WIN else ("rz", "sz", "lrz", "lsz")
    for name in wanted:
        src = bin_dir / name
        if src.is_file():
            _datas.append((str(src), "bin"))

icons_dir = REPO / "installer" / "icons"
icon = None
for name in ("mavisssh.ico", "mavisssh.icns"):
    p = icons_dir / name
    if p.is_file():
        icon = str(p)
        break

# **协议全文必须跟着产物走。** 目录版产物是要整个目录拷给别人用的，
# GPL-3.0 第 4 / 6 条要求随二进制提供协议全文与对应源码的获取途径。
# 少了这几份文件，分发出去的就是一个「许可证没交代」的包。
_licenses = REPO / "LICENSE"
if _licenses.is_file():
    _datas.append((str(_licenses), "."))
for _extra in (REPO / "THIRD_PARTY_NOTICES.md", REPO / "bin" / "THIRD_PARTY_NOTICE.md"):
    if _extra.is_file():
        _datas.append((str(_extra), "."))
_lic_dir = REPO / "LICENSES"
if _lic_dir.is_dir():
    for _txt in sorted(_lic_dir.glob("*.txt")):
        _datas.append((str(_txt), "LICENSES"))

block_cipher = None

a = Analysis(                                            # noqa: F821
    [str(REPO / "main.py")],
    pathex=[str(REPO)],
    binaries=[],
    datas=_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pytest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)   # noqa: F821

exe_kwargs = dict(
    name="MavisSSH",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI 程序：不要黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)

if ONEFILE:
    # **单文件：二进制、zip、数据全部塞进 EXE，而且不要 COLLECT。**
    # 少写 `a.binaries / a.zipfiles / a.datas` 任何一个，打出来就是「能启动
    # 但功能全缺」的那种包 —— 界面在（PyQt 在 zip 里），lrzsz 不在。
    exe = EXE(                                             # noqa: F821
        pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
        runtime_tmpdir=None, **exe_kwargs)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True,   # noqa: F821
              **exe_kwargs)
    coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas,  # noqa: F821
                   strip=False, upx=False, name="MavisSSH")

if IS_MAC and not ONEFILE:
    app = BUNDLE(                                            # noqa: F821
        coll,
        name="MavisSSH.app",
        icon=icon,
        bundle_identifier="com.mavisssh.client",
        info_plist={
            "CFBundleName": "MavisSSH",
            "CFBundleDisplayName": "MavisSSH",
            "CFBundleShortVersionString": "0.2.0",
            "NSHighResolutionCapable": True,
            # 用得到 SFTP/TCP，转折点：本地转发监听端口不需要，
            # 但远端主机名解析和出站连接在沙箱里要放行。
            "NSAppTransportSecurity": {"NSAllowsArbitraryLoads": True},
        },
    )
