"""通道字节不是 UTF-8 时，不能把它们变成一片豆腐块。

**症状**：远端用非 UTF-8 字符集（Latin-1 / cp437 之类）输出时，屏幕上
非 ASCII 段**整段变成方块**，而 ASCII 段完好；**把内容复制出来却是好的**。

**机制**：`write()` 原来无条件 `data.decode("utf-8", errors="replace")`。
非法 UTF-8 的字节全被替换成 **U+FFFD**，而字体没有 U+FFFD 的字形 ——
于是每个替换字符画成一个豆腐块。实测喂 141 字节（ASCII 段 + Latin-1 段）
进去，旧实现产生 **27 个 U+FFFD**，新实现 **0 个**。

真实终端在这里会按当前字符集正常显示，复制也完全正常。

**为什么退回 latin-1 而不是「猜一个编码」**：latin-1 把 0x00~0xFF 一一对应
到 U+0000~U+00FF，**永不失败、也不丢字节**。猜 cp437/GBK 的话猜错了照样是
乱码，而且猜错时字节已经被吃掉、无法补救。

**为什么必须是增量解码器**：UTF-8 的多字节字符可能被 TCP 拆包，逐块独立
解码会把一个汉字劈成两半。

Checks:
  1. Latin-1 输出不再产生 U+FFFD
  2. ASCII 段不受影响
  3. 被拆包的多字节 UTF-8 仍然正确重组（增量解码的意义）
  4. 纯 UTF-8 输出零变化（**这一条锁住绝大多数场景不被改坏**）
  5. 解码器跨块保持状态
  6. 退路不丢字节：latin-1 是 1 字节 1 字符
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtWidgets

from sshclient.ui.terminal_widget import TerminalWidget

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)


def new_term():
    t = TerminalWidget()
    t.resize(900, 300)
    t.show()
    for _ in range(4):
        app.processEvents()
    return t


print("=== 1. Latin-1 输出：旧实现是豆腐块，新实现不是 ===")
t = new_term()
# 和用户截图同构：0x21~0x7E（ASCII）+ 0xA0~0xFF（Latin-1 高半区）
payload = bytes(range(0x21, 0x7F)) + b"|" + bytes(range(0xA0, 0x100))
t.write(payload)
for _ in range(15):
    app.processEvents()
line = t._screen.display[0]
check("Latin-1 输出不再产生 U+FFFD", "�" not in line,
      "U+FFFD x%d" % line.count("�"))
check("ASCII 段完好（没有被动过）",
      line.startswith(chr(0x21) + chr(0x22) + "#$%&"),
      line[:12])
check("Latin-1 段显示成真正的字符（而不是方块）",
      "¡¢£¤¥" in line, "含 ¡¢£¤¥ = %s" % ("是" if "¡¢£¤¥" in line else "否"))

print()
print("=== 2. 纯 UTF-8 输出零变化（锁住绝大多数场景）===")
t2 = new_term()
utf8 = "中文测试 English 混排 with ASCII\n".encode("utf-8")
t2.write(utf8)
for _ in range(15):
    app.processEvents()
row = [l for l in t2._screen.display if l.strip()][0]
check("UTF-8 内容原样显示", row.strip() == "中文测试 English 混排 with ASCII",
      row.strip())
check("UTF-8 场景零 U+FFFD", "�" not in row)

print()
print("=== 3. 被 TCP 拆包的多字节字符仍然正确重组 ===")
# 这是「必须用增量解码器」的理由：逐块独立解码会把一个汉字劈成两半
whole = "终端渲染".encode("utf-8")          # 12 字节，3 个汉字
t3 = new_term()
for i in range(0, len(whole), 5):           # 故意在多字节字符中间切断
    t3.write(whole[i:i + 5])
    for _ in range(3):
        app.processEvents()
got = [l for l in t3._screen.display if l.strip()][0].strip()
check("拆包后汉字仍完整", got == "终端渲染", repr(got))

print()
print("=== 4. 退路不丢字节 ===")
check("latin-1 是 1 字节 = 1 字符（永不失配）",
      len(bytes(range(0xA0, 0x100)).decode("latin-1")) == 0x100 - 0xA0)
t4 = new_term()
t4.write(b"\x80\x81\x82\x83")
for _ in range(10):
    app.processEvents()
r4 = t4._screen.display[0].rstrip()
check("高位字节也没有变成方块", "�" not in r4, repr(r4[:8]))

print()
print("=== 5. 解码器跨块保持状态 ===")
t5 = new_term()
check("_dec 在构造后已初始化（不是靠 _decode 兜底）",
      hasattr(t5, "_dec") and t5._dec is None)
whole2 = "abcdef终端".encode("utf-8")
for i in range(0, len(whole2), 3):
    t5.write(whole2[i:i + 3])
    for _ in range(3):
        app.processEvents()
r5 = [l for l in t5._screen.display if l.strip()][0].strip()
check("多块混合输入也正确", r5 == "abcdef终端", repr(r5))
check("解码器已被真正建立", t5._dec is not None)

print()
if FAIL:
    print("失败 %d 项：" % len(FAIL))
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("通过 %d 项，失败 0 项" % len(PASS))
sys.exit(0)
