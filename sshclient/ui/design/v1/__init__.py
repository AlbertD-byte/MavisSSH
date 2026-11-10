"""设计版本 v1：当前生效的一版。

`palette.py` —— `Palette` 深浅两套 token、ANSI/xterm256 表、颜色工具。
`qss.py` —— 整份 QSS，由 `build_qss(p, dialog, ui_size)` 生成。

**改这一版之前先读两条硬约束**（都是被评审和实测打出来的）：

1. **色值不许跨族借用。** `text_on_accent` 是按用户自选 accent 推的，
   拿它去压 `danger` 在深色 accent 下会原样退回原来的低对比度 ——
   数值不变，所以没有任何测试会红。跨族要么新增按目标 token 自己推的
   字段，要么就地按目标 token 算。
   **同族的两个反例**：主按钮底色换成实心档 `accent_solid` 之后，
   压在它上面的字/图标必须用 `text_on_solid`（恒白）而不是
   `text_on_accent` —— 后者在浅色下是**近黑**，近黑压深蓝和蓝底黑字是
   同一种「不柔和」；`accent_solid` 也不能拿去当 `accent` 的深一档使，
   它按定义要满足白字 ≥4.5，压得比任何「看起来更深」的值都深。
2. **可達性要先于对比度。** `[variant="x"]` / `[role="x"]` 这类属性选择器，
   可达性 = 有生产者 **且** 生产者的实参真的取到那个值。只 grep
   `setProperty` 的字符串出现会同时骗人两次：零生产者的看着像死规则，
   而「看起来有生产者」的（实参恒为默认值）更该打掉。
   详见 `tests/_accent.py` 的契约 6~9。
3. **每个 token 字段都要有一个真消费者。** 本文件历史上出现过
   `bg_scrim` / `border_focus` / `danger_hover` 三个零消费者字段 ——
   被定义、被推导、被测试钉成字面量，却谁也不看。给
   `tests/_fconn.py` 的死 token 判据加字段时先确认有人用。
"""
