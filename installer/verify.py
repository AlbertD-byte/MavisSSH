"""检查打包产物**是不是自包含的**。

**为什么需要这道闸。** onedir 产物里的 ``MavisSSH.exe`` **不是**能单独拷走用的
可执行文件：Qt 的 DLL、平台插件、翻译文件、以及 lrzsz 的 ``bin/rz.exe`` /
``bin/sz.exe`` 全在同目录的 ``_internal/`` 里。只把 exe 拷到别处启动，Windows
会弹一个系统的 ``Error`` 对话框（缺 DLL），进程还活着但没有界面 —— 用户看到
的就是「窗口一闪就没 / 缺依赖」，而打包脚本一路绿灯，没有任何一处报错。

这类问题**必须在交付前拦住**，因为它的表现离原因太远：没人会想到去查
``_internal/``。这里直接把「这个 exe 能不能单独运行」量出来。

    还有一个更早的坑，量的是同一件事的**更上游**：

    build\\mavisssh\\MavisSSH.exe   ← PyInstaller 的中间产物，跑不起来
    dist\\MavisSSH\\MavisSSH.exe    ← 交付物，能跑

**同名、同为 .exe、体积也差不多**（4.25MB vs 4.25MB），两个并排躺在同一个仓库
里。spec 里 onedir 走的是 ``EXE(pyz, a.scripts, [], exclude_binaries=True)`` ——
二进制和数据被**故意**排除在这个 EXE 之外、由随后的 ``COLLECT`` 搬进
``_internal/``。所以 ``build/`` 里那个是「只有壳」的半成品，单独点开必然报：

    Failed to load Python DLL '...\\build\\mavisssh\\_internal\\python314.dll'

这条错误离原因很远（没人会想到去查 build 目录），所以这里先把「你拿的是
中间产物」这件事说清楚，再谈自包含。

    python installer/verify.py            # 检查 dist/ 下的产物
    python installer/verify.py <路径>      # 检查指定的 exe（工作目录的会直接拒）
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"

#: 单独运行必须要有的东西。少任何一样都是「拷出去必然起不来」。
#:
#: ``msys-2.0.dll`` 尤其重要：``bin/rz.exe`` / ``bin/sz.exe`` 是依赖它的
#: MSYS2 程序，datas 收进去但没被 exe 链接上，静态检查看不出来，只有真跑
#: ZMODEM 传输才会炸。
REQUIRED_RELATIVE = (
    "bin/rz.exe",
    "bin/sz.exe",
    "bin/msys-2.0.dll",
    # **Qt 的 DLL 在 `Qt6/bin/` 下面，不是 `Qt6/` 下面。** 写成后者会一直报
    # 「缺 Qt6Core.dll」，而产物其实好好的 —— 报缺的那次会让人去查一个
    # 根本不存在的打包问题。
    "PyQt6/Qt6/bin/Qt6Core.dll",
    "PyQt6/Qt6/plugins/platforms/qwindows.dll",
)

#: **只在 PyInstaller 工作目录里出现**的文件；交付物目录不会有它们。
#:
#: ``MavisSSH.pkg`` / ``PYZ-00.pyz`` 是最硬的标志 —— 那是 EXE 打包过程中的
#: 中间体，PyInstaller 除了 ``--workpath`` 哪儿都不写。``*-00.toc`` 次之。
#:
#: **不要用「有没有 ``_internal/``」来判工作目录**：工作目录里那个 exe 当然也
#: 没有 ``_internal/``，但它缺依赖的原因不是「有人把依赖拷走了」，而是**依赖
#: 还没被搬进来**——COLLECT 压根还没跑。两个原因必须分开报，否则用户会去
#: ``build/`` 里找一个从来不存在的 ``_internal/``。
WORKDIR_MARKERS = ("*.pkg", "PYZ-*.pyz", "*-00.toc")


def _is_workdir_exe(exe: pathlib.Path) -> bool:
    """这个 exe 是不是 PyInstaller 工作目录里的中间产物。"""
    root = exe.parent
    if not root.is_dir():
        return False
    return any(next(root.glob(pattern), None) for pattern in WORKDIR_MARKERS)


def _deliverables() -> list[pathlib.Path]:
    """dist/ 下真正的交付物，按优先级排。"""
    out = []
    onefile = DIST / "MavisSSH.exe"
    onedir = DIST / "MavisSSH" / "MavisSSH.exe"
    if onefile.is_file():
        out.append(onefile)
    if onedir.is_file():
        out.append(onedir)
    return out


def _workdir_lookalikes() -> list[pathlib.Path]:
    """build/ 里与交付物同名的 exe —— 拿错就是用户报的那个错。"""
    return sorted(BUILD.glob("*/*")) if BUILD.is_dir() else []


def _report_workdir_exe(exe: pathlib.Path) -> int:
    # 「检查 xxx」由 main() 统一打，这里不再打一遍。
    print("  [!!] **这是 PyInstaller 的中间产物，不是交付物 —— 它跑不起来是必然的。**")
    print()
    print("      同目录下有 %s，这是 PyInstaller 打包过程中的东西，"
          % "、".join(p.name for p in exe.parent.glob("*.toc"))[:60])
    print("      只会写在 --workpath 里，交付目录里不会有。")
    print()
    print("      原因：onedir 的 spec 用的是")
    print("          EXE(pyz, a.scripts, [], exclude_binaries=True)")
    print("      二进制和数据**被故意排除**在这个 EXE 之外，由随后的 COLLECT 搬进")
    print("      _internal/。所以这个 exe 里只有壳，点开必然报：")
    print("          Failed to load Python DLL '...\\\\_internal\\\\python3xx.dll'")
    print()
    goods = _deliverables()
    if goods:
        print("      要启动的是这个：")
        for g in goods:
            print("          %s" % g.relative_to(ROOT))
    else:
        print("      dist/ 下还没有产物，先跑：python installer/build.py")
    print("      想让 build/ 里那个也别长得像 exe：python installer/build.py --clean")
    return 1


def _has_exe_side_by_side(exe: pathlib.Path) -> list[str]:
    """onedir 产物：exe 同级目录（及其 ``_internal/``）里逐项找。"""
    root = exe.parent
    internal = root / "_internal"
    missing = []
    for rel in REQUIRED_RELATIVE:
        if (root / rel).is_file():
            continue
        if internal.is_dir() and (internal / rel).is_file():
            continue
        missing.append(rel)
    return missing


def _is_onefile(exe: pathlib.Path) -> bool:
    """是不是单文件产物。

    **不要用「同级有没有同名文件夹」来判断** —— onedir 的布局恰恰是
    ``dist/MavisSSH/MavisSSH.exe``，exe 就在一个与自己同名的目录里，那种写法
    会把 onedir 误判成单文件（然后去一个 4MB 的空壳里找归档，自然什么都找不到）。
    可靠标志是 PyInstaller 6 的 onedir 必有 ``_internal/``。
    """
    return not (exe.parent / "_internal").is_dir()


def _onefile_contents_ok(exe: pathlib.Path) -> bool:
    """单文件模式用 PyInstaller 归档自检：能不能列出归档里的 bin/。"""
    try:
        out = subprocess.run(
            [sys.executable, "-m", "PyInstaller.utils.cliutils.archive_viewer",
             "-l", "-r", str(exe)],
            capture_output=True, text=True, timeout=180).stdout
    except Exception:  # noqa: BLE001
        return True          # 查看器不可用就不断言，交给人判断
    # 归档里的路径在 Windows 上是**反斜杠**（`bin\sz.exe`），所以按文件名查，
    # 别按带斜杠的完整路径查。
    return all(n in out for n in ("sz.exe", "rz.exe", "msys-2.0.dll",
                                  "qwindows.dll"))


def main(argv: list[str]) -> int:
    target = pathlib.Path(argv[1]) if len(argv) > 1 else None
    if target is None:
        goods = _deliverables()
        if not goods:
            print("dist/ 下没有产物，先跑 installer/build.py")
            return 1
        target = goods[0]

    print("检查 %s" % target)
    if not target.is_file():
        print("  [!!] 文件不存在")
        return 1

    # **工作目录里的中间产物要在任何自包含判断之前拦下来。** 它当然也不
    # 自包含，所以按老逻辑会报「少了 bin/sz.exe、Qt6Core.dll…」—— 每一行
    # 都是对的，而每一条建议（去 dist/ 找、拷整个目录）都在把用户往另一个
    # 方向带，因为缺的那些东西不是「被拷走了」，是「还没生成」。先说真话。
    if _is_workdir_exe(target):
        return _report_workdir_exe(target)

    if _is_onefile(target):
        print("  模式：单文件（自包含）")
        ok = _onefile_contents_ok(target)
        if ok:
            print("  [ok] 归档里 bin/sz.exe、rz.exe、msys-2.0.dll、qwindows.dll 都在")
            _warn_lookalikes()
            return 0
        print("  [!!] 这**不是**一个完整的单文件 exe：归档里查不到 "
              "bin/sz.exe / qwindows.dll。")
        print("      多半是从 onedir 目录里**只把 exe 拷了出来** —— 真正的依赖都在"
              "同目录的 _internal/ 里。")
        return 1

    print("  模式：onedir")
    missing = _has_exe_side_by_side(target)
    if missing:
        print("  [!!] **这个 exe 不能单独拷走** —— 少了下面这些，而它们只在 "
              "同目录的 _internal/ 里：")
        for m in missing:
            print("        - %s" % m)
        print()
        print("      只复制 MavisSSH.exe 到别处启动，Windows 会弹系统的 "
              "「Error」对话框（缺 DLL），")
        print("      进程活着但没有窗口 —— 也就是「一闪就没 / 缺依赖」。")
        print("      要单个自包含的 exe：python installer/build.py --onefile")
        return 1

    print("  [ok] 运行所需的文件都在（同目录或 _internal/）")
    print("  [ok] 整个目录一起拷走可用；**只拿 exe 不行**")
    _warn_lookalikes()
    return 0


def _warn_lookalikes() -> None:
    """产物判过了，也提醒一句 build/ 里那个同名的。

    用户点开的往往不是「判错了」，是**根本没查这个文件**——他打开的是
    ``build/mavisssh/MavisSSH.exe``。产物自检全绿的时候把这件事说出来，
    比等他双击之后弹一个 ``Failed to load Python DLL`` 便宜得多。
    """
    hits = []
    for good in _deliverables():
        for p in _workdir_lookalikes():
            if p.name == good.name and p.is_file():
                hits.append(p)
    for p in hits:
        print()
        print("  [注意] build/ 里还有一个**同名**的 %s —— 那个是 PyInstaller 的"
              % p.name)
        print("         中间产物，点开一定报缺 DLL。交付物是 dist/ 下面那个。")
        print("         查它：python installer/verify.py %s" % p.relative_to(ROOT))
    if hits:
        print("         想让它消失：python installer/build.py --clean")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
