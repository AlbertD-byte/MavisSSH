"""核心层：本地决策，不依赖 paramiko / pyte。

调色板、字体、配置、端口转发规则、ZMODEM 都在这里。放进这一层的判据是
**不需要一台服务器就能测**——`core/` 里的东西都可以：屏蔽 paramiko 和 pyte
之后，本包 6 个模块照常 import。

Qt 是唯一的例外，且只出现在 `fonts.py`：字体解析的输入就是 `QFontDatabase`，
绕不开。`palette.py` 原本也在导入期拉 PyQt6（只为 `qcolor` / `blend` 两个函数），
已改成函数内延迟导入，所以整个包除了 fonts 之外都不碰 Qt。
"""
