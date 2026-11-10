"""打包产物的**端到端**验收：不止看文件在不在，还要真跑起来。

`installer/verify.py` 是静态自检（产物里该有的文件有没有）。这份是它的补充，
量的是**用户真正会遇到的那三件事**：

1. 从**别的位置**启动，窗口出不出来（日志有没有写出来）
2. **整个目录拷到别处**还能不能跑 —— 这才是"安装"的动作，onedir 最容易在这里
   翻车（依赖用相对路径找时，目录一挪就找不到）
3. **只拷 exe** 会不会像上次那样弹系统的 Error 对话框

不能自动化断言的（真实 SSH 连接、ZMODEM 传输）会在最后列出来，那部分得人验。

    python installer/verify_runtime.py
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
PY = ROOT / ".venv/Scripts/python.exe"
if not PY.exists():
    PY = ROOT / ".venv/bin/python"
DIST = ROOT / "dist"

FAIL: list[str] = []
OK = 0


def check(name, cond, detail=""):
    global OK
    if cond:
        OK += 1
        print("  [ok] %s" % name)
    else:
        FAIL.append(name)
        print("  [!!] %s%s" % (name, ("  " + detail) if detail else ""))


def launch_and_wait(exe: pathlib.Path, workdir: pathlib.Path,
                    want_title=True, timeout=40.0):
    """启动 exe，等一个**真有窗口**的进程出现，返回 (标题, 秒数)。

    单文件模式下 `Start-Process` 拿到的是 PyInstaller 引导器的 PID，真正的窗口
    在**子进程**上 —— 只看传进来的那个 PID 会永远等不到，量出来就是
    "起不来"，而其实它好好的。所以这里按进程名去找，不认 PID。
    """
    subprocess.run(["taskkill", "/F", "/IM", "MavisSSH.exe"],
                   capture_output=True)
    time.sleep(0.8)
    t0 = time.monotonic()
    subprocess.Popen([str(exe)], cwd=str(workdir),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    title, elapsed = None, None
    while time.monotonic() - t0 < timeout:
        time.sleep(0.2)
        ps = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq MavisSSH.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True)
        # 窗口标题不在 tasklist 里，改用 PowerShell 问一次（只在找到进程后问，
        # 免得空转）。
        if "MavisSSH" in ps.stdout:
            q = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-Process MavisSSH -ErrorAction SilentlyContinue | "
                 "Where-Object { $_.MainWindowHandle -ne 0 } | "
                 "Select-Object -First 1).MainWindowTitle"],
                capture_output=True, text=True)
            title = (q.stdout or "").strip()
            if title:
                elapsed = time.monotonic() - t0
                break
    return title, elapsed


def kill_all():
    subprocess.run(["taskkill", "/F", "/IM", "MavisSSH.exe"],
                   capture_output=True)


def newest_log(under: pathlib.Path):
    logs = sorted(under.rglob("*.log"), key=lambda p: p.stat().st_mtime,
                  reverse=True)
    return logs[0] if logs else None


onedir_exe = DIST / "MavisSSH" / "MavisSSH.exe"
onefile_exe = DIST / "MavisSSH.exe"

print("=" * 78)
if not onedir_exe.is_file() and not onefile_exe.is_file():
    print("dist/ 下没有产物，先跑 python installer/build.py")
    sys.exit(1)

# ---------------------------------------------------------------- 1. 静态自检
print("\n[1] 静态自检（installer/verify.py）")
rc = subprocess.call([str(PY), str(ROOT / "installer" / "verify.py")],
                     cwd=str(ROOT))
check("verify.py 判定产物自包含", rc == 0, "退出码 %d" % rc)

# ------------------------------------------------- 2. 只拷 exe 必须被拒（守门）
print("\n[2] 只拷 exe 的目录必须起不来（这条是被守护的行为）")
solo = pathlib.Path(tempfile.mkdtemp(prefix="mavis_solo_"))
if onedir_exe.is_file():
    shutil.copy2(onedir_exe, solo / "MavisSSH.exe")
    title, _ = launch_and_wait(solo / "MavisSSH.exe", solo)
    check("只拷 onedir 的 exe：没有出现正常窗口（弹的是系统 Error）",
          title in (None, "", "Error"), "标题=%r" % title)
else:
    title, _ = launch_and_wait(onefile_exe, solo)
    check("单文件 exe 单独拷走照样能跑（这是它该有的样子）",
          bool(title) and title != "Error", "标题=%r" % title)
kill_all()

# ------------------------------------------------- 3. lrzsz 二进制能不能真执行
print("\n[3] lrzsz 二进制真的能执行（静态存在 ≠ 能跑）")
bin_dir = (DIST / "MavisSSH" / "_internal" / "bin") if onedir_exe.is_file() \
    else None
if bin_dir is None:
    print("  （当前是单文件产物，二进制在归档里，交给 [1] 的归档检查）")
else:
    for tool in ("sz", "rz"):
        exe = bin_dir / ("%s.exe" % tool)
        check("%s.exe 在 _internal/bin 里" % tool, exe.is_file(), str(exe))
        if not exe.is_file():
            continue
        try:
            # **必须 errors="replace"。** rz/sz 是 MSYS2 程序，跑起来会往管道里
            # 吐 ZMODET 的二进制（`**B0100...`），而 `text=True` 默认按本机编码
            # 严格解码，GBK 下直接 UnicodeDecodeError。换台机器就是这个崩。
            p = subprocess.run([str(exe)], cwd=str(bin_dir),
                               capture_output=True, text=True,
                               errors="replace", timeout=25)
            out = (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired as exc:
            def _s(v):
                return v.decode("utf-8", "replace") if isinstance(v, bytes) else (v or "")
            out = _s(exc.stdout) + _s(exc.stderr)
        # **真正的失败是 Windows 装载器报错**，特征是 0xc0000135 / "not a valid
        # Win32 application" / "找不到指定的模块"。那和 lrzsz 自己的报错长得很
        # 不像，必须分开判，否则「它报错了」会被当成「它坏了」。
        loader_errors = ("0xc0000135", "not a valid Win32 application",
                         "找不到指定的模块", "The specified module could not")
        looks_like_loader = any(e.lower() in out.lower() for e in loader_errors)
        check("%s 是程序自己报的错，不是装载器报的" % tool,
              not looks_like_loader, out[:160].replace("\n", " "))
        check("%s 能跑到协议层（认出了自己的用法/超时）" % tool,
              any(k in out for k in ("need at least one file", "waiting to",
                                     "Try `", "usage", "Usage")),
              out[:160].replace("\n", " "))

# ---------------------------------------------- 4. 整目录/单文件换位置能不能跑
print("\n[4] 换位置运行（这才是「安装」的动作）")
if onedir_exe.is_file():
    moved = pathlib.Path(tempfile.mkdtemp(prefix="mavis_moved_")) / "MavisSSH"
    shutil.copytree(DIST / "MavisSSH", moved)
    check("整个目录已拷到别处", (moved / "MavisSSH.exe").is_file())
    check("_internal 里的 lrzsz 也跟着走了",
          (moved / "_internal" / "bin" / "sz.exe").is_file())
    work = pathlib.Path(tempfile.mkdtemp(prefix="mavis_cwd_"))
    title, elapsed = launch_and_wait(moved / "MavisSSH.exe", work)
    check("换位置后能起（标题=%r，%.1fs）" % (title, elapsed or -1),
          bool(title) and title != "Error", "标题=%r" % title)
    lg = newest_log(work)
    check("在工作目录写出了日志", lg is not None,
          "没找到 *.log —— cwd=%s" % work)
    if lg is not None:
        txt = lg.read_text(encoding="utf-8", errors="replace")
        check("日志里有启动记录", "MavisSSH 启动" in txt, txt[-200:])
        check("日志里有会话载入记录", "已载入" in txt, txt[-200:])
    kill_all()
    exe_under_test = moved / "MavisSSH.exe"
else:
    exe_under_test = onefile_exe
    work = pathlib.Path(tempfile.mkdtemp(prefix="mavis_cwd_"))
    title, elapsed = launch_and_wait(exe_under_test, work)
    check("换位置后能起（标题=%r，%.1fs）" % (title, elapsed or -1),
          bool(title) and title != "Error", "标题=%r" % title)
    lg = newest_log(work)
    check("在工作目录写出了日志", lg is not None)
    kill_all()

# ------------------------------------------------------------- 5. 不能自动化的
print("\n[5] 以下只能人验，自动化跑不到：")
for item in (
    "真连一台 SSH（localhost:22），确认终端能出字、远端 pty 尺寸同步正常",
    "上传/下载走一整轮 ZMODEM：sz/rz 协议握手 + 文件落地（脚本只验到二进制"
    "能执行，验不到和远端的协议交互）",
    "切主题 / 改强调色，确认配置写回 %APPDATA%\\MavisSSH",
):
    print("    - " + item)

print("\n" + "=" * 78)
print("通过 %d 项，失败 %d 项" % (OK, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  失败：%s" % f)
    sys.exit(1)
