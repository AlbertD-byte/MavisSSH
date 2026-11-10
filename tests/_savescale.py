"""量 `SessionStore.upsert` 的耗时随会话数怎么长。

跑 10000 轮存储往返时发现的：每 upsert 一次都重写整个 sessions.json，
所以第 n 次的代价与 n 成正比，累计是 **O(N²)** 的写放大。
这不是稳定性问题（不会崩、不会泄漏），但它是实打实的规模特性，
值得用数字说清楚，而不是靠等一小时去撞。

同时确认：在这个过程中**没有异常、没有句柄泄漏、落盘始终是密文**。
"""
import os
import pathlib
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ctypes

from sshclient.core import crypto
from sshclient.core.settings import SavedSession, SessionStore

_K32 = ctypes.windll.kernel32
_K32.GetCurrentProcess.restype = ctypes.c_void_p
_K32.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
_K32.GetProcessHandleCount.restype = ctypes.c_int


def handles() -> int:
    n = ctypes.c_ulong(0)
    _K32.GetProcessHandleCount(_K32.GetCurrentProcess(), ctypes.byref(n))
    return n.value


tmp = pathlib.Path(tempfile.mkdtemp(prefix="mavisssh_scale_"))
crypto.reset_for_tests()
store = SessionStore(tmp / "s.json", config_dir=tmp / "cfg")

print("=" * 72)
print("upsert 耗时 vs 已有会话数（单次操作）")
print("=" * 72)
print(f"  {'已有会话':>8s}  {'单次耗时':>10s}  {'相对上一档':>12s}  {'文件大小':>10s}")
base_h = handles()
prev = None
points = []
REPS = 20
for target in (20, 60, 120, 240, 480, 960):
    # 真正把 store 长到 target，而不是每档只加 REPS 条 ——
    # 第一版就是这里写错的：每档固定加 20 条，却按 target 打标签，
    # 于是"已有会话"那一列全是假的（跑到最后也才 120 条）。
    while len(store.all) < target:
        store.upsert(SavedSession(
            name=f"会话{len(store.all)}", host="10.0.0.5", port=22,
            username="deploy", password="pw", notes="x" * 200))
    n0 = len(store.all)
    t0 = time.perf_counter()
    for _ in range(REPS):
        store.upsert(SavedSession(
            name=f"会话{len(store.all)}", host="10.0.0.5", port=22,
            username="deploy", password="pw", notes="x" * 200))
    ms = (time.perf_counter() - t0) / REPS * 1000
    size = (tmp / "s.json").stat().st_size
    ratio = f"x{ms / prev:.2f}" if prev else "  -"
    print(f"  {n0:8d}  {ms:9.2f}ms  {ratio:>12s}  {size / 1024:9.1f} KB")
    points.append((n0, ms))
    prev = ms

print()
print("=" * 72)
print("判据：会话数翻倍时，单次耗时该涨多少")
print("=" * 72)
print("  整体重写 -> 单次耗时随 n 线性涨，倍率趋近 2；")
print("  若存在真正的 O(N²) 项，倍率会明显超过 2。")
prev_n = prev_ms = None
worst = 0.0
for n, m in points:
    if prev_n:
        r = m / prev_ms
        worst = max(worst, r)
        print(f"  {prev_n:5d} -> {n:5d}  (会话数 x{n / prev_n:.1f})   耗时倍率 x{r:.2f}")
    prev_n, prev_ms = n, m
print()
print(f"  最大倍率 x{worst:.2f}  ->  " +
      ("存在超线性项" if worst > 2.5 else "单次耗时近似线性于会话数（符合'整体重写'的预期）"))

raw = (tmp / "s.json").read_text(encoding="utf-8")
n_total = len(store.all)
store2 = SessionStore(tmp / "s.json", config_dir=tmp / "cfg")
print()
print(f"  最终会话数        {n_total}")
print(f"  文件含 enc:v1:    {'enc:v1:' in raw}")
print(f"  文件含明文口令    {'\"pw\"' in raw}")
print(f"  重开后条数        {len(store2.all)}")
print(f"  重开后读回正确    {bool(store2.all) and store2.all[-1].password == 'pw'}")
print(f"  句柄漂移          {handles() - base_h:+d}")
print(f"  残留 .tmp         {[p.name for p in tmp.glob('*.tmp')] or '无'}")
