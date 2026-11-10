"""打安装包。三个平台跑同一个脚本，产出各自平台的格式。

    python installer/build.py            # 当前平台的默认产物
    python installer/build.py --clean    # 先清 dist/ build/
    python installer/build.py --onefile  # 单文件（Windows/macOS/Linux 都支持）

**不能交叉编译。** PyInstaller 打出来的二进制只能用在本平台：Windows 的
.exe 里有 Windows PE，拿到 macOS 上跑不了，反之亦然。所以这三个包**必须分
三次、在三个系统上各打一次**；要自动化就上 CI 的 matrix，别指望一台机器出
三个平台的包。

产物：

===========  ============================================  ===========
平台        产物                                         位置
===========  ============================================  ===========
Windows     ``MavisSSH/MavisSSH.exe`` + 依赖（onedir）     ``dist/``
macOS       ``MavisSSH.app``                              ``dist/``
Linux       ``MavisSSH/MavisSSH`` + 依赖（onedir）          ``dist/``
===========  ============================================  ===========

**为什么 Windows 默认是 onedir 而不是 onefile。** 单文件版每次启动都要把整个
Qt（~60MB）解压到临时目录再运行，冷启动以秒计；目录版是"复制过去就能跑"。

**但默认产物不是自包含的 exe。** ``MavisSSH.exe`` 单独拷走必然起不来：它依赖同
目录的 ``_internal/``（Qt 的 DLL、插件、翻译都在那儿），缺了就弹系统的
"Error" 对话框，用户看到的是"窗口一闪就没 / 缺依赖"。实测过：只复制 exe 到别
的目录启动，进程活着但 ``MainWindowTitle`` 是 ``Error``。

要**单个可分发的 exe** 就加 ``--onefile``：

===========  =========================================  ==================
产物        怎么用                                      代价
===========  =========================================  ==================
``MavisSSH/`` 整个目录拷走（不能只拿 exe）            启动快
``MavisSSH.exe`` 单个文件，随处可放可发                 每次启动解压 Qt，慢
===========  =========================================  ==================
"""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = ROOT / "installer" / "mavisssh.spec"
ICONS = ROOT / "installer" / "icons"

PLATFORM_NAME = {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, "Linux")
IS_WIN = sys.platform.startswith("win")


def venv_python() -> pathlib.Path:
    """用 .venv 里的解释器，别用系统 python——依赖装在那儿。"""
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
        p = ROOT / rel
        if p.exists():
            return p
    raise SystemExit("没找到 .venv，先建：\n"
                     "    python -m venv .venv\n"
                     "    %s -m pip install -r requirements-build.txt"
                     % (".venv/Scripts/python.exe -m pip install -r requirements.txt"
                        if sys.platform == "win32" else
                        ".venv/bin/python -m pip install -r requirements.txt"))


def ensure_icons(py: pathlib.Path) -> None:
    """图标是构建产物，不在版本库里。缺了就现生成一份。"""
    need = {"win32": "mavisssh.ico", "darwin": "mavisssh.icns"}.get(
        sys.platform, "mavisssh-256.png")
    if not (ICONS / need).exists():
        print("图标不在（%s），现生成…" % need)
        subprocess.check_call([str(py), str(ROOT / "installer" / "make_icons.py")])


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 MavisSSH")
    ap.add_argument("--clean", action="store_true", help="先删掉 dist/ 和 build/")
    ap.add_argument("--onefile", action="store_true",
                    help="单文件（启动慢，见文件头的说明）")
    ap.add_argument("--no-confirm", action="store_true",
                    help="跳过 PyInstaller 删旧产物的确认（CI 里用）")
    ap.add_argument("--runtime-check", action="store_true",
                    help="构建完再跑一遍端到端验收（启进程、整目录挪位置重跑；"
                         "要多花一分多钟，交付前跑一次合适）")
    ap.add_argument("--nsis", action="store_true",
                    help="onefile 打完后再跑 NSIS，产出 dist\\MavisSSH-Setup.exe"
                         "（需要系统已装 NSIS 且 makensis 在 PATH 里）")
    args = ap.parse_args()

    py = venv_python()
    ensure_icons(py)

    if args.clean:
        for d in ("dist", "build"):
            p = ROOT / d
            if p.exists():
                print("删掉 %s/" % d)
                shutil.rmtree(p)

    cmd = [str(py), "-m", "PyInstaller", str(SPEC),
           "--noconfirm",
           "--distpath", str(ROOT / "dist"),
           "--workpath", str(ROOT / "build")]
    if args.onefile:
        # **单文件模式只能靠环境变量传给 spec。** 命令行上的 `--onefile`
        # 在这里没用：spec 是自己写的，它写死了 `COLLECT`，而 PyInstaller
        # 不会替用户把 COLLECT 拆成「二进制塞进 EXE」。原来这一行传了
        # `--onefile`，打出来的仍然是目录版 —— 而文件头的说明里偏偏写着
        # 「愿意拿启动时间换单文件的话加 --onefile」，等于这条路是空的。
        #
        # 原来那句 `"--windowed" if win32 else "--windowed"` 两边一样，
        # 是个恒等三元，什么都没选。
        os.environ["MAVISSSH_ONEFILE"] = "1"
    else:
        os.environ.pop("MAVISSSH_ONEFILE", None)

    print("\n$ " + " ".join(cmd) + "\n")
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc != 0:
        return rc

    print("\n完成。产物在 dist/ ：")
    dist = ROOT / "dist"
    for p in sorted(dist.rglob("*")):
        if p.is_dir() and p.name in ("__pycache__", "base_library.zip"):
            continue
        kind = "/" if p.is_dir() else ""
        size = "" if p.is_dir() else "  %.1f MB" % (p.stat().st_size / 1048576)
        print("    %s%s%s" % (p.relative_to(dist), kind, size))

    # **把「该双击哪个」摆在产物清单的最前面。** 上面那份清单按字母序排，
    # 而 build/ 里那个同名的中间 exe 又跟它长得几乎一样，用户从
    # `build\mavisssh\MavisSSH.exe` 打开过一次，弹的是
    # `Failed to load Python DLL ..._internal\python314.dll` —— 那条错误离
    # 原因太远，没人会顺着想到去 dist/。这里直接点名。
    goods = sorted(p for p in dist.glob("*")
                   if p.is_dir() and (p / "MavisSSH.exe").is_file())
    onefile = dist / "MavisSSH.exe"
    print("\n" + "=" * 70)
    print("要启动的是下面这个（不是 build/ 里那个同名的）：")
    for g in goods:
        print("    %s" % (g / "MavisSSH.exe"))
    if onefile.is_file():
        print("    %s   ← 单文件版" % onefile)
    print("=" * 70)

    # **构建完立刻自检产物是不是自包含的。** onedir 的 exe 单独拷走必然起不来
    # （实测弹系统「Error」对话框，进程活着但没窗口），而打包过程一路绿灯、
    # 没有任何一处会报错 —— 不主动量一遍，这个坑只能等用户装完才发现。
    print("\n检查产物是否可单独运行…")
    vrc = subprocess.call([str(py), str(ROOT / "installer" / "verify.py")])
    if vrc != 0 and not args.onefile:
        print("\n提示：上面这个目录版产物**要整个目录一起拷**，只拿 exe 会起不来。")
        print("      想要单个自包含的 exe：python installer/build.py --onefile")

    # ---- NSIS 安装向导 ----
    if args.nsis:
        nsi = ROOT / "installer" / "mavisssh.nsi"

        # 找 makensis。**用 shutil.which 而不是 subprocess.call 去"试跑一下"**：
        # Windows 上命令不在 PATH 时抛的是 FileNotFoundError 异常，不是返回码，
        # 靠 `if rc in (0,1)` 判存在会把整个构建脚本带崩 —— 而且崩在**异常**上，
        # 连"NSIS 没装"这句提示都印不出来，用户只看到一屏 traceback。
        # which 专门干这件事：找到给路径，找不到给 None，不抛异常。
        candidates = []
        on_path = shutil.which("makensis")           # nsis.exe 不在 PATH 时为 None
        if on_path:
            candidates.append(on_path)
        if IS_WIN:
            # 装完 NSIS 不一定把 %ProgramFiles(x86)%\NSIS 加进 PATH（要重启
            # shell 才生效），所以绝对路径那几支要照样试。
            for env in ("ProgramFiles(x86)", "ProgramFiles"):
                pf = os.environ.get(env)
                if not pf:
                    continue
                for sub in ("NSIS", "NSIS-portable", "NSIS-3"):
                    p = pathlib.Path(pf) / sub / "makensis.exe"
                    if p.is_file():
                        candidates.append(str(p))

        makensis = candidates[0] if candidates else None
        if not makensis:
            print("\n** NSIS 未找到，跳过安装包步骤。")
            print("  下载 NSIS（https://nsis.sourceforge.io/，免费开源）后重试：")
            print("  python installer/build.py --onefile --nsis")
        else:
            print("\n用 makensis: %s" % makensis)
            print("$ %s %s\n" % (makensis, nsi.relative_to(ROOT)))
            rc = subprocess.call([makensis, str(nsi)], cwd=str(ROOT))
            if rc == 0:
                setups = sorted((ROOT / "dist").glob("MavisSSH-*-Setup*.exe"),
                               key=lambda p: p.stat().st_size)
                if setups:
                    print("\n安装包已生成：")
                    for s in setups:
                        print("    %s  (%.1f MB)" % (s.name, s.stat().st_size / 1048576))
            else:
                print("\n** NSIS 编译失败（exit %d）" % rc)
                return rc

    # 端到端验收：真把产物跑起来、整目录挪个位置再跑一遍。
    # **默认不跑** —— 它要启进程、等窗口、拷 100MB 到临时目录，构建会慢一分多钟；
    # 交付前手动跑一次：python installer/verify_runtime.py
    if args.runtime_check:
        print("\n端到端验收（启动产物、换位置再启动）…")
        rrc = subprocess.call([str(py), str(ROOT / "installer" / "verify_runtime.py")])
        if rrc != 0:
            return rrc
    return vrc


if __name__ == "__main__":
    sys.exit(main())
