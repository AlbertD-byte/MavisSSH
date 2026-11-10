"""稳定性压测：10000 轮。

「10000 次」如果理解成**进程级**反复启动应用，那在物理上不现实——每次冷启动
约 1.5 秒，10000 次要 4 小时。所以这里拆成三种**失效模式**分别压，每一种
对应一类不同的崩溃，合并起来的信息量比单纯重复同一件事大得多：

  A. 进程内控件生命周期 10000 轮 —— 建 MainWindow / SessionTab，显示，
     拆掉。抓的是 Qt 对象泄漏、事件处理器堆积、句柄/fd 泄漏、内存增长。
     这是唯一能真跑到 10000 的那一档（实测 13.9 ms/轮，约 2.3 分钟）。
  B. 会话存储往返 —— 加密/解密/落盘/重读。抓密钥处理、文件句柄、
     临时文件残留。**实测不能跑 10000**：单次 upsert 的耗时随已有会话数线性
     增长（整体重写整个文件，20→60→120… 倍率稳定在 x1.9），于是 10000 次是
     O(N²) 的累计写放大，要一个多小时。跑 2000 足够覆盖同样的失效模式。
     增长曲线由 `_savescale.py` 单独量。
  C. 进程级冷启动 —— 抓**退出期**崩溃（Qt 析构顺序问题：上一轮实测
     修复前 30 次崩 1 次）。每轮要起一个真进程，约 0.66 s/次，
     所以按时间预算取次数并如实报数，不假装它跑满了。

全程跟踪：异常数、工作集增长、句柄/fd 漂移、耗时。任何一项持续单向增长
都是泄漏信号，结束时统一报。

    python tests/_soak.py                 # 默认 10000 / 10000 / 120
    python tests/_soak.py --child         # C 档的子进程，做一次就退
"""
from __future__ import annotations

import argparse
import ctypes
import gc
import os
import pathlib
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAIL: list[str] = []
NOTES: list[str] = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({extra})" if extra else ""),
          flush=True)


PASS: list[str] = []


# --------------------------------------------------------------------------- 资源计量


class _PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


# **ctypes 不设 argtypes/restype，默认按 c_int 传参并接返回值。**
# 64 位句柄和 64 位内存数会被截断：句柄数恒为 0，工作集恒为 -1。
# 下面这一段看起来啰嗦，少了它这个压测就是在拿假数字说话。
_K32 = ctypes.windll.kernel32
_PSAPI = ctypes.windll.psapi
_K32.GetCurrentProcess.restype = ctypes.c_void_p
_K32.GetProcessHandleCount.argtypes = [ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_ulong)]
_K32.GetProcessHandleCount.restype = ctypes.c_int
_PSAPI.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p,
                                        ctypes.POINTER(_PMC),
                                        ctypes.c_ulong]
_PSAPI.GetProcessMemoryInfo.restype = ctypes.c_int


def working_set_mb() -> float:
    """当前进程的工作集（MB）。拿不到就返回 -1，不让计量本身成为故障点。"""
    try:
        c = _PMC()
        c.cb = ctypes.sizeof(_PMC)
        rc = _PSAPI.GetProcessMemoryInfo(_K32.GetCurrentProcess(),
                                         ctypes.byref(c), c.cb)
        return c.WorkingSetSize / (1024 * 1024) if rc else -1.0
    except Exception:
        return -1.0


def open_handles() -> int:
    """本进程打开的内核对象数。Windows 数句柄，POSIX 数 fd。"""
    if sys.platform == "win32":
        n = ctypes.c_ulong(0)
        if not _K32.GetProcessHandleCount(_K32.GetCurrentProcess(),
                                          ctypes.byref(n)):
            return -1
        return n.value
    for d in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(d):
            return len(os.listdir(d))
    return -1


# --------------------------------------------------------------------------- 环境


#: QApplication 必须**在模块级留一个引用**。PyQt 的对象一旦没有 Python 侧的
#: 引用就会被析构，而 QApplication 被析构之后再建任何 QWidget 都只会得到一句
#: "Must construct a QApplication before a QWidget"——和"没 boot 过"完全一样，
#: 很容易被误判成别的问题。
_APP = None


def boot_app():
    global _APP
    from tests.fonts import boot
    _APP = boot([])
    return _APP


def neutralise_dialogs():
    """模态框会让无头压测永久死锁（QMessageBox::exec 跑的是嵌套事件循环）。
    这里全部换成记录，不 exec。"""
    from PyQt6 import QtWidgets
    seen: list = []

    def _rec(self, *a, **k):
        seen.append(getattr(self, "windowTitle", lambda: "?")())
    for name in ("exec", "information", "warning", "critical", "question"):
        setattr(QtWidgets.QMessageBox, name, _rec)
    return seen


def make_store(tmp: pathlib.Path):
    from sshclient.core import crypto
    from sshclient.core.settings import SessionStore
    crypto.reset_for_tests()
    return SessionStore(tmp / "sessions.json", config_dir=tmp / "cfg")


def new_settings():
    from sshclient.core.settings import AppSettings
    s = AppSettings()
    s.theme = "dark"
    return s


def build_window(store):
    from sshclient.ui.main_window import MainWindow
    return MainWindow(settings=new_settings(), store=store)


def teardown(w):
    """把 widget 真正销毁。

    **只做 setParent(None) + deleteLater() + processEvents() 是不够的。**
    实测（_leakprobe2）：那样跑 20 次 processEvents 之后 `sip.isdeleted(w)`
    仍然是 False——C++ 对象根本没被删掉，于是它连同那份解析后的 14197 字符 QSS
    一直挂着，表现为 +0.33 MB/轮 的假"泄漏"。

    必须显式 `sendPostedEvents(None, DeferredDelete)` 才会真正析构；补上这一步后
    同样的循环是 +0.000 MB/轮。
    """
    from PyQt6 import QtCore
    from PyQt6.QtWidgets import QApplication
    w.setParent(None)
    w.deleteLater()
    QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    QApplication.processEvents()


# --------------------------------------------------------------------------- A


def phase_a(rounds: int, store) -> dict:
    print(f"\n{'=' * 70}\nA. 进程内控件生命周期 × {rounds}\n{'=' * 70}", flush=True)
    app_mod = __import__("PyQt6.QtWidgets", fromlist=["QApplication"])
    QApplication = app_mod.QApplication

    # 预热：首次构造会加载字体/图标，混进计时和内存基线都不准
    w = build_window(store)
    w.show()
    for _ in range(3):
        QApplication.processEvents()
    teardown(w)
    gc.collect()

    base_mem, base_h = working_set_mb(), open_handles()
    t0 = time.perf_counter()
    errors: list[str] = []
    samples = []
    done = 0
    every = max(1, rounds // 20)          # 固定采 20 个点，曲线形状比总数重要
    for i in range(rounds):
        try:
            win = build_window(store)
            win.show()
            for _ in range(2):
                QApplication.processEvents()
            teardown(win)
            done += 1
        except Exception as exc:                       # noqa: BLE001
            errors.append(f"第 {i} 轮: {type(exc).__name__}: {exc}")
            if len(errors) > 20:
                break
        if (i + 1) % every == 0:
            gc.collect()
            m, h = working_set_mb(), open_handles()
            samples.append((i + 1, m, h))
            print(f"    {i + 1:6d} 轮  内存 {m:7.1f} MB  句柄 {h:6d}  "
                  f"用时 {time.perf_counter() - t0:6.1f}s", flush=True)
    dt = time.perf_counter() - t0
    end_mem, end_h = working_set_mb(), open_handles()
    print(f"\n    完成 {done} 轮，用时 {dt:.1f}s（{dt / max(done, 1) * 1000:.1f} ms/轮）")

    mem_growth = end_mem - base_mem
    handle_drift = end_h - base_h
    check("A 没有抛出未捕获异常", not errors, f"{len(errors)} 个: {errors[:2]}")
    check("A 内存没有持续增长超过 120 MB", mem_growth < 120,
          f"起始 {base_mem:.1f} -> 结束 {end_mem:.1f} MB (+{mem_growth:.1f})")
    check("A 句柄没有持续增长超过 150", handle_drift < 150,
          f"起始 {base_h} -> 结束 {end_h} (+{handle_drift})")
    # 形状判据：比较前后两段的**斜率**，而不是两段的增量。
    # 上一版拿"前段增量"和"后段增量"相减，线性增长时两段都是大数，
    # 相减必然为负，于是永远"收敛"——那是个恒真的废判据。
    if len(samples) >= 4:
        h = len(samples) // 2
        r1 = ((samples[h][1] - samples[0][1]) /
              max(1, samples[h][0] - samples[0][0]))
        r2 = ((samples[-1][1] - samples[h][1]) /
              max(1, samples[-1][0] - samples[h][0]))
        check("A 内存斜率没有在后半段变大（不是持续线性爬）", r2 <= r1 * 1.5 + 0.05,
              f"前半 {r1:+.3f} MB/轮 -> 后半 {r2:+.3f} MB/轮")
    if errors:
        for e in errors[:5]:
            print(f"      {e}")
    return {"rounds": done, "sec": dt, "mem_growth": mem_growth,
            "handle_drift": handle_drift, "samples": samples}


# --------------------------------------------------------------------------- B


def phase_b(rounds: int, tmp: pathlib.Path) -> dict:
    print(f"\n{'=' * 70}\nB. 会话存储加解密往返 × {rounds}\n{'=' * 70}", flush=True)
    from sshclient.core.settings import SavedSession

    tmp_b = tmp / "b"
    tmp_b.mkdir(parents=True, exist_ok=True)
    store = make_store(tmp_b)
    base_h = open_handles()
    t0 = time.perf_counter()
    errors = []
    last = None
    for i in range(rounds):
        try:
            s = SavedSession(
                name=f"会话{i}", host=f"10.0.{i % 256}.{(i * 7) % 256}",
                port=1000 + (i % 60000), username="u",
                password=f"p{i}", notes="x" * 200)
            # 每轮都是新 uuid，upsert 是**追加**而不是替换，所以要按 id 取回，
            # 拿 all[0] 只会永远拿到最早那一条（第一版就是这么写错的）。
            store.upsert(s)
            last = s
            back = store.get(s.id)
            if back is None or back.password != f"p{i}":
                errors.append(f"第 {i} 轮: 往返后密码对不上 "
                              f"({back.password!r} if back else 'None')")
        except Exception as exc:                       # noqa: BLE001
            errors.append(f"第 {i} 轮: {type(exc).__name__}: {exc}")
            if len(errors) > 20:
                break
    dt = time.perf_counter() - t0
    # 重开一次，确认落盘的是密文且能读回来
    store2 = make_store(tmp_b)
    raw = (tmp_b / "sessions.json").read_text(encoding="utf-8")
    drift = open_handles() - base_h
    n = len(store2.all)

    check("B 没有抛出未捕获异常", not errors, f"{len(errors)} 个: {errors[:2]}")
    check("B 落盘文件里搜不到明文密码", f'"p{rounds - 1}"' not in raw)
    check("B 落盘文件带 enc:v1: 前缀", "enc:v1:" in raw)
    check("B 重开后条数正确", n == rounds, f"{n} 条")
    check("B 重开后读回最后一条", n and store2.all[-1].password == f"p{rounds - 1}",
          store2.all[-1].password if n else "空")
    check("B 没有残留 .tmp 文件", not list(tmp_b.glob("*.tmp")),
          str([p.name for p in tmp_b.glob('*.tmp')]))
    check("B 句柄没有持续增长", drift < 60, f"{base_h} -> {open_handles()} (+{drift})")
    print(f"\n    用时 {dt:.1f}s（{dt / max(rounds, 1) * 1000:.2f} ms/轮）")
    return {"rounds": rounds, "sec": dt, "handle_drift": drift, "errors": errors}


# --------------------------------------------------------------------------- C


def phase_c(rounds: int) -> dict:
    print(f"\n{'=' * 70}\nC. 进程级冷启动 × {rounds}（抓退出期崩溃）\n{'=' * 70}",
          flush=True)
    t0 = time.perf_counter()
    codes: dict[int, int] = {}
    for i in range(rounds):
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "--child"],
                           capture_output=True, timeout=180)
        codes[p.returncode] = codes.get(p.returncode, 0) + 1
        if (i + 1) % 20 == 0:
            print(f"    {i + 1:5d}/{rounds}  用时 {time.perf_counter() - t0:6.1f}s  "
                  f"退出码分布 {codes}", flush=True)
    dt = time.perf_counter() - t0
    bad = sum(v for k, v in codes.items() if k != 0)
    check("C 每次冷启动都干净退出", bad == 0, f"退出码分布 {codes}")
    if bad:
        print(f"    非零退出 {bad} 次：{codes}")
    print(f"\n    用时 {dt:.1f}s（{dt / max(rounds, 1):.2f} s/次）")
    return {"rounds": rounds, "sec": dt, "codes": codes, "bad": bad}


# --------------------------------------------------------------------------- child


def run_child() -> int:
    """C 档子进程：建一次窗口、拆一次、退出。退出期崩溃就发生在这里。"""
    app = boot_app()
    neutralise_dialogs()
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="mavisssh_soak_"))
    store = make_store(tmp)
    win = build_window(store)
    win.show()
    for _ in range(3):
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()
    teardown(win)
    del win
    gc.collect()
    return 0


# --------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", action="store_true")
    ap.add_argument("--a", type=int, default=10000)
    ap.add_argument("--b", type=int, default=2000)
    ap.add_argument("--c", type=int, default=200)
    args = ap.parse_args()

    if args.child:
        return run_child()

    boot_app()
    neutralise_dialogs()
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="mavisssh_soakrun_"))

    print("=" * 70)
    print(f"MavisSSH 稳定性压测   pid={os.getpid()}")
    print(f"起始内存 {working_set_mb():.1f} MB   起始句柄 {open_handles()}")
    print(f"目标: A×{args.a}  B×{args.b}  C×{args.c}")

    t_all = time.perf_counter()
    store = make_store(tmp)
    a = phase_a(args.a, store)
    b = phase_b(args.b, tmp)
    c = phase_c(args.c)
    total = time.perf_counter() - t_all

    print(f"\n{'=' * 70}\n汇总\n{'=' * 70}")
    print(f"  A 控件生命周期   {a['rounds']:6d} 轮  {a['sec']:7.1f}s  "
          f"内存 +{a['mem_growth']:.1f} MB  句柄 +{a['handle_drift']}")
    print(f"  B 存储往返       {b['rounds']:6d} 轮  {b['sec']:7.1f}s  "
          f"句柄 +{b['handle_drift']}")
    print(f"  C 进程冷启动     {c['rounds']:6d} 轮  {c['sec']:7.1f}s  "
          f"失败 {c['bad']}  退出码 {c['codes']}")
    print(f"  合计用时 {total:.1f}s")

    print(f"\n{'=' * 70}")
    if FAIL:
        print(f"FAILURES ({len(FAIL)}):")
        for n in FAIL:
            print("  -", n)
        return 1
    print("RESULT: PASS -- 没有崩溃、没有泄漏")
    return 0


if __name__ == "__main__":
    sys.exit(main())
