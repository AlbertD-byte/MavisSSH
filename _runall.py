"""跑完全部独立测试脚本，每套一个独立进程 + 独立期限。

Windows / macOS / Linux 通用，替代原来的 `_runall.ps1`。行为逐条对齐那份
PowerShell 版（判定口径、退出码语义、输出格式都没变），换过来的理由只有
一个：`.ps1` 只有 Windows 有。

**为什么每套要独立进程 + 独立期限。** 曾经有一套挂住，整个扫荡就停在那一条
管线上，后面的一概没跑——回归被报成"卡住"，而不是"这一套超时"。现在挂住的
只是它自己那一套。

**为什么超时时要连子进程一起杀。** 旧版 PowerShell 的做法是
`Get-Process python | Where-Object Id -eq $p.Id | Stop-Process`，那只杀到了
直接子进程；测试要是自己再拉起子进程（真起一个本地服务器、跑一个 zmodem
子进程），它们会活下来继续占着端口，下一套就莫名其妙地失败。这里按进程组
整体收掉：Windows 用 `taskkill /T`，POSIX 用 `killpg`。

**exit=2 是「一条都没红，但有判据因为前提没成立而没评上」。**
它**绝不能和 exit=0 混在一起算通过**：评审实测过一个具体的洞——「只坏焦点」
和「焦点坏 + Esc 逻辑也真坏」两种情况输出完全一样，而退出码只看 FAIL，于是
真的回归被静默跳过、整套还报成功。所以单列一类，最后也让它 exit 1：
**没全评上就不算全过**。

用法：

    python _runall.py              # 用 sys.executable 跑全部
    python _runall.py _titlebar    # 只跑指定的几套

判据在 `tests/_readme.py` 里：它用 AST 读本文件的 ``TESTS``，所以往里加一套
测试必须同时加进这个元组，否则门禁不知道有这套。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

# 路径从本文件推，不写死。之前那份 .ps1 里三处写死了 D:\workspaces\...，
# 结果仓库一移动或一 clone，整个测试就废了。
ROOT = pathlib.Path(__file__).resolve().parent

TESTS = (
    "_e2e", "_smoke", "_setkeys", "_uifont", "_glyphfb", "_tabclose",
    "_tabbtn", "_sideitem", "_rowfit", "_sidefold", "_tabtest",
    "_comptest", "_tabwire", "_zmtest", "_closebtn", "_tunnel", "_cfgfuzz",
    "_stress", "_swclick", "_tunpersist", "_crypto", "_logs",
    "_portable", "_sftpsync", "_ptyresize", "_zmodem", "_icons",
    "_deadfn", "_snippets", "_uibtns", "_readme", "_titlebar", "_lrzprobe",
    "_accent", "_terquery", "_sfpop", "_tunports", "_winframe", "_sftptilde",
    "_taberase", "_decoding", "_scrollkeep", "_group",
    # 功能测试套件：按功能域逐项判定，见 docs/FUNCTION-TEST-REPORT.md。
    # **它们是「功能清单逐项过一遍」的载体，红的条目是真缺陷**，
    # 不要为了让这一轮全绿而改软判据 —— 报告里引用的就是这些红。
    "_fwin", "_fterm", "_fsftp", "_fconf", "_fconn",
    # 安全整改与后续修复带来的判据（2026-10-20 一轮安全评审之后新增）。
    # **它们此前一直不在这个元组里** —— 也就是说仓库自己提交的门禁
    # 根本没跑过主机密钥信任、远端命令拼接、隧道暴露面这三组判据，
    # 而它们恰恰是那轮整改的核心产出。补进来是必须的：门禁不跑，
    # 等于没有门禁。
    "_fhostkey", "_fhostkey2", "_fshelljoin", "_ftunnel",
    # 真机崩溃修复（worker 往已销毁的控件投递）与标签页右键菜单的判据。
    "_fthreadpost", "_ftabcontext",
    # 导入器与 tab 高度判据。
    "_import", "_tabh",
    # 无障碍判据。**这两套此前一直没进门禁** —— `_uia11y.py` 守的是
    # WCAG 对比度契约，而它**从来没跑过**（CI 也不会跑，因为它不在这个
    # 元组里）。「门禁不跑，等于没有门禁」。
    # `_filltext.py` 守的是「压在填充色上的文字必须按填充色自己算黑白」——
    # 它验的是产品**实际会画哪个颜色**，不是 `palette.text_on()` 这个函数
    # 本身选得对不对（那是 `_uia11y.py` 的活）。
    "_uia11y", "_filltext",
)

DEADLINE = 180          # 每套的期限（秒）
KILL_GRACE = 5          # 超时后先礼后兵，给这么久还不走就强杀


def _last_line(path: pathlib.Path) -> str:
    """输出文件里最后一个非空行，也就是这套的结论。

    必须按 UTF-8 读。不指定编码的话，Windows 上会拿系统 ANSI 代码页去解
    这些字节，每一行中文结论都变成乱码——测试输出全是英文的时候一直没暴露，
    直到 `_readme.py` 开始打印中文。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def _kill_tree(proc: subprocess.Popen) -> None:
    """连同子进程一起收掉。"""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        # /T = 连子进程树一起杀，/F = 不给它机会自己收尾
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        import signal
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if proc.poll() is not None:
                return
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.send_signal(sig)
                except ProcessLookupError:
                    return
            try:
                proc.wait(timeout=KILL_GRACE)
            except subprocess.TimeoutExpired:
                continue


def main(argv: list[str]) -> int:
    # 控制台按 UTF-8 写。Windows 上默认代码页是 GBK，结论里全是中文，
    # 不改的话 print 自己就抛 UnicodeEncodeError。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    os.environ["PYTHONIOENCODING"] = "utf-8"

    if sys.prefix == sys.base_prefix:
        print("提醒：当前不在虚拟环境里（sys.prefix == sys.base_prefix）。"
              "本仓库要求用 .venv 里的解释器跑，见 README。\n")

    tests = [a for a in argv[1:] if a.startswith("_")] or list(TESTS)
    unknown = [t for t in tests if not (ROOT / "tests" / (t + ".py")).exists()]
    if unknown:
        print("tests/ 下找不到：%s" % "、".join(unknown))
        return 1

    passed = 0
    failures: list[str] = []
    skipped: list[str] = []

    # **每套的 stdout / stderr 落到 `logs/tests/`，不是仓库根。**
    # 原来写在根目录，一轮 36 套就是 72 个 `_run_*.out/.err` 堆在那儿，
    # `ls` 里除了源码和目录只剩日志。而 `logs/` 本来就已经是应用日志
    # （`core/logs.py` 写的 `mavisssh.log`）的落点，测试日志归到它下面的
    # `tests/` 一层，两类日志各归其位、根目录清空。
    # `.gitignore` 的 `logs/` 一条就盖住了，不用再单独加。
    out_dir = ROOT / "logs" / "tests"
    out_dir.mkdir(parents=True, exist_ok=True)

    for name in tests:
        out_path = out_dir / ("_run_%s.out" % name)
        err_path = out_dir / ("_run_%s.err" % name)
        # 只有 POSIX 需要 start_new_session：它才是 `killpg` 能按进程组收掉的
        # 前提。Windows 上**不要**加 CREATE_NEW_PROCESS_GROUP——旧版 runner
        # 没有它，而 taskkill /T 也不需要它，凭空多一个控制台控制处理的差异
        # 只会给测试引入原版没有的行为变化。
        spawn_kwargs = ({"start_new_session": True}
                        if sys.platform != "win32" else {})
        timed_out = False
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            proc = subprocess.Popen(
                [sys.executable, "-u", str(ROOT / "tests" / (name + ".py"))],
                cwd=str(ROOT), stdout=out, stderr=err, **spawn_kwargs)
            try:
                code = proc.wait(timeout=DEADLINE)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_tree(proc)
                code = None

        tail = _last_line(out_path)
        if timed_out:
            failures.append("%s : TIMEOUT after %ds" % (name, DEADLINE))
            print("%-12s TIMEOUT" % name)
        elif code == 0:
            passed += 1
            print("%-12s ok    %s" % (name, tail))
        elif code == 2:
            skipped.append("%s : 有判据未评估（前提没成立）  %s" % (name, tail))
            print("%-12s SKIP  exit=2  %s" % (name, tail))
        else:
            failures.append("%s : exit=%s  %s" % (name, code, tail))
            print("%-12s FAIL  exit=%s  %s" % (name, code, tail))

    print()
    print("passed %d / %d" % (passed, len(tests)))
    if skipped:
        print("not fully evaluated:")
        for line in skipped:
            print("  - " + line)
    if failures:
        print("failures:")
        for line in failures:
            print("  - " + line)
        return 1
    if skipped:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
