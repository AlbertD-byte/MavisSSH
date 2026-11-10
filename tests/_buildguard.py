"""打包守卫：`build/` 里那个同名的 exe 必须被认出来，不能当成产物放行。

用户报的是「打包完启动 exe 报错」，弹的是

    Failed to load Python DLL '...\\build\\mavisssh\\_internal\\python314.dll'

实测确认：他打开的是 ``build/mavisssh/MavisSSH.exe`` —— PyInstaller 的**工作
目录**中间产物。它和交付物 ``dist/MavisSSH/MavisSSH.exe`` **同名、同为 exe、
大小几乎一样（4.25MB vs 4.25MB）**，并排躺在同一个仓库里。

根因在 spec：onedir 走 ``EXE(pyz, a.scripts, [], exclude_binaries=True)``，
二进制和数据**被故意排除**在这个 EXE 之外，由随后的 ``COLLECT`` 搬进
``_internal/``。所以 ``build/`` 里那个是「只有壳」的半成品 —— 它缺依赖不是
因为「有人把依赖拷走了」，而是**依赖还没生成**。

**所以判据必须把这两件事分开。** 原来只有「有没有 `_internal/`」这一个判据，
工作目录的 exe 当然也没有，于是走进「少了 bin/sz.exe、Qt6Core.dll…」那条
分支 —— 每一行都是对的，而每一条建议（去 dist/ 找、拷整个目录）都在把用户
往另一个方向带。这正是本条要拦的。

判据跑在**真产物**上（不能造假的 exe 假装测），所以这里用真正的
``build/`` 和 ``dist/``；没有产物时先建一份最小目录结构再测标记逻辑。
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(ROOT / "installer"))

import verify  # noqa: E402

PY = ROOT / ".venv/Scripts/python.exe"
if not PY.exists():
    PY = ROOT / ".venv/bin/python"

bad = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}: {got!r} (want {want!r})")
    if not ok:
        bad.append(f"{name}: got {got!r}, want {want!r}")


print("仓库：%s" % ROOT)

# ------------------------------------------------- 1. 工作目录的 exe 会被认出来
build_exe = sorted(ROOT.glob("build/*/MavisSSH.exe"))
check("仓库里有 build 中间产物（这条是前提，没有就说明 --clean 过了）",
      bool(build_exe), True)
if build_exe:
    be = build_exe[0]
    check("被认成工作目录中间产物", verify._is_workdir_exe(be), True)
    print(f"       {be.relative_to(ROOT)}")
    sib = sorted(p.name for p in be.parent.glob("*.toc"))
    print(f"       同目录 toc：{sib}")
    for marker in ("MavisSSH.pkg", "PYZ-00.pyz"):
        check(f"  标记文件 {marker} 在（前提）", (be.parent / marker).is_file(), True)

# ------------------------------------------------ 2. 交付物不能被误判成中间产物
dist_exe = sorted(ROOT.glob("dist/MavisSSH/MavisSSH.exe")) + \
    sorted(ROOT.glob("dist/MavisSSH.exe"))
check("dist/ 下有交付物（这条是前提）", bool(dist_exe), True)
if dist_exe:
    de = dist_exe[0]
    check("交付物没有被误判成中间产物", verify._is_workdir_exe(de), False)
    print(f"       {de.relative_to(ROOT)}")

# ------------------------------------------- 3. 标记逻辑：造出来的目录也认
# 不能只靠真产物 —— 判据得在「标记还在 / 标记被删」两边都跑过，
# 否则「恒绿」和「压根没判」分不开。
tmp = pathlib.Path(tempfile.mkdtemp())
fake_work = tmp / "build" / "mavisssh"
fake_work.mkdir(parents=True)
(fake_work / "MavisSSH.exe").write_bytes(b"MZ")
(fake_work / "MavisSSH.pkg").write_bytes(b"x")
(fake_work / "PYZ-00.pyz").write_bytes(b"x")
check("只有 .pkg 也认得出是中间产物", verify._is_workdir_exe(fake_work / "MavisSSH.exe"), True)

(fake_work / "MavisSSH.pkg").unlink()
check("只剩 .pyz 也认得出", verify._is_workdir_exe(fake_work / "MavisSSH.exe"), True)
(fake_work / "PYZ-00.pyz").unlink()
(fake_work / "Analysis-00.toc").write_text("x")
check("只剩 .toc 也认得出", verify._is_workdir_exe(fake_work / "MavisSSH.exe"), True)
(fake_work / "Analysis-00.toc").unlink()
check("标记都没了就不认（否则会把正常目录也拒了）",
      verify._is_workdir_exe(fake_work / "MavisSSH.exe"), False)

# 交付目录长什么样才不会被拒：只有 exe + _internal，没有任何 toml/pkg
fake_good = tmp / "dist" / "MavisSSH"
(fake_good / "_internal").mkdir(parents=True)
(fake_good / "MavisSSH.exe").write_bytes(b"MZ")
check("交付目录（有 _internal、无中间文件）不会被拒",
      verify._is_workdir_exe(fake_good / "MavisSSH.exe"), False)
shutil.rmtree(tmp)

# --------------------------------------------- 4. 命令行：拿中间 exe 去查必须被拒
if build_exe:
    be = build_exe[0]
    p = subprocess.run([str(PY), str(ROOT / "installer" / "verify.py"), str(be)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
    out = p.stdout
    print("\n--- verify.py %s ---" % be.relative_to(ROOT))
    for line in out.splitlines():
        print("   ", line)
    check("退出码非 0（必须拒绝）", p.returncode != 0, True)
    check("说清了「中间产物」", "中间产物" in out, True)
    check("说清了为什么（COLLECT 还没搬依赖）", "COLLECT" in out, True)
    check("点名了真正该启动的文件",
          "dist" in out and "MavisSSH.exe" in out, True)
    # **不能把用户往「去 dist/ 拷整个目录」那条路上带** —— 那是针对
    # 「dist 的 exe 被单独拷走」的，套在这个错上是错的建议。
    check("没有给出「只拿 exe 会起不来」这种误导建议",
          "只拿 exe 不行" in out, False)

# --------------------------------------------- 5. 查交付物时提醒 build 里的同名
if dist_exe:
    p = subprocess.run([str(PY), str(ROOT / "installer" / "verify.py")],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
    out = p.stdout
    print("\n--- verify.py（无参数，查 dist/）---")
    for line in out.splitlines():
        print("   ", line)
    check("交付物自检通过", p.returncode, 0)
    if build_exe:
        check("提醒了 build/ 里有同名的中间产物",
              "同名" in out and "中间产物" in out, True)

print()
if bad:
    print(f"FAIL ({len(bad)}):")
    for b in bad:
        print("  -", b)
else:
    print("build guard: all checks passed")
sys.exit(1 if bad else 0)
