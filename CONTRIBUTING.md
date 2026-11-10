# 贡献指南

感谢你愿意为 MavisSSH 花时间。这份文档写的是**这个仓库特有的规矩**，
不是一份通用的开源模板 —— 通用模板不会告诉你「为什么这里连断言写法都有要求」。

## 目录

- [开始之前](#开始之前)
- [开发环境](#开发环境)
- [改动流程](#改动流程)
- [判据：怎么写才对](#判据怎么写才对)
- [提交规范](#提交规范)
- [文档](#文档)
- [安全问题](#安全问题)

## 开始之前

先读这三个文件，它们是这个项目的上下文：

| 文件 | 讲什么 |
|---|---|
| [`AGENTS.md`](AGENTS.md) | 编码规约：行为准则、代码规范、日志规范、异常类型、提交规则。**改代码前必读。** |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 设计决策、实测数据、**否决过哪些方案**、什么条件下该改回去。 |
| [`docs/FUNCTION-TEST-REPORT.md`](docs/FUNCTION-TEST-REPORT.md) | 按功能域逐项核对的结果清单。 |

第二份尤其重要。这个项目里有不少「看起来可以简化」的地方，
其实是某个方案被否决后留下的坑 —— 不知道那段历史的人很容易把它「优化」回去。

## 开发环境

```bash
git clone https://github.com/AlbertD-byte/MavisSSH.git
cd MavisSSH
python -m venv .venv
```

Windows：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

macOS / Linux：

```bash
./.venv/bin/python -m pip install -r requirements.txt
```

**一律用项目内的虚拟环境。** 应用不应依赖 `PATH` 上碰巧是哪个 Python。
`requirements.txt` 只写下限；想复现门禁的确切环境，看 README 的「验证环境」一节。

## 改动流程

1. **开一个分支**，命名随意但能看出意图（`fix/…`、`feat/…`、`docs/…`）。
2. **改动 + 配套判据**，在同一次提交里。
3. **跑门禁**，全绿再推。
4. **开 PR**，描述里说清「改了什么」和「为什么」。

### 跑门禁

```bash
python _runall.py            # 全部
python _runall.py _fconn     # 只跑名字里含 _fconn 的几套
python tests/_fconn.py       # 跑单套
```

门禁是**一个脚本一个进程**跑的 —— 这是刻意的。同一个进程里连着 `exec` 多个
Qt 脚本，前一个留下的 QApplication / Widget / 定时器会污染下一个，
跑到第 6 个左右就访问冲突，**而每个脚本单独跑都是绿的**。崩的是测试环境，不是你的代码。

每个脚本有独立的 180 秒期限。某个脚本卡住时，报错只落在它身上，不会卡住整轮。

### 平台插件不能错

判据里有大量基于**字体度量**的断言，所以必须跑在真实的 `windows` 平台插件下：

```bash
QT_QPA_PLATFORM=windows python _runall.py
```

在 `QT_QPA_PLATFORM=offscreen` 下 Qt 根本找不到字体目录，
`QFontDatabase.families()` 返回 0 —— 于是所有截图断言变成恒真，
**测的东西看起来还在，其实已经测不到了**。offscreen 只用于纯逻辑判据。

## 判据：怎么写才对

这个仓库的判据全部是 `tests/` 下的独立脚本，不引入 pytest。
下面几条不是风格偏好，是踩过坑之后定下的。

### 钉性质，不钉字面量

```python
# ❌ 钉绝对像素：字号一改、字重一改，它就红了，而产品并没有坏
ck("旧实现的高度就是 50", old_h == 50)

# ✅ 钉关系：随基线一起漂移，但性质不变
ck("旧实现的高度比控件高出上下内边距之和",
   old_h == base_h + 2 * _LEGACY_PAD)
```

判据红了先问一句：**它钉的是性质还是字面量？**
「某函数名出现在某文件里」「某常量等于某值」「源码里有某段文本」——
这三类都是字面量，换一种正确实现就会让它们红。

### 元断言要有区分度

元断言是「装回缺陷版，那批断言必须变红」的验证。两个要点：

1. **老实现要在至少一批输入上露馅**，并指名道姓说是哪几条 —— 不是「每条都红」。
   老实现在某些输入上本来就安全，强行要求全红只会得到误报。
2. **必须有一条专门检查「替换是否生效」的元断言。** 替换没生效时，
   判据照样绿（因为它断言的东西恰好满足），完全看不出来是假货。

### 判据里不许有阻塞 I/O

判据谓词里做阻塞网络 I/O 会饿死事件循环，量到的是超时而不是行为。

### 造二进制基准用 `write_bytes`

Windows 上 `write_text` 会把 `\n` 翻译成 `\r\n`，造出来的基准和 Linux 上不一致。

### 「现象出现」≠「事情做完」

等收尾，不要等现象。

### 判据自己出错时

拿不准是产品的问题还是判据写错了，**先写一个独立探针复现一遍**再下结论。
直接改判据把它改绿是最坏的处理方式 —— 那等于把测量工具的刻度改了。

## 提交规范

- **一个提交做一件事。** 不要把功能、格式化、文档重排混在一起。
- **提交信息用祈使句**：`fix(ui): 关闭按钮在窄标签下被裁`。
- **破坏性变更**在信息末尾加一行 `BREAKING CHANGE: <说明>`。
- **不要提交无关产物**：`logs/`、`__pycache__/`、截图 `*.png`（除了
  `docs/img/` 下有意提交的发布截图）、构建输出。
- 如果你的分支里有多个提交，PR 描述里说明每一步为什么单独拆开。

## 文档

**交付一个完整功能，就在同一次改动里更新 README。** README 描述这个 app 做什么，
功能落地而 README 没跟上，README 就是错的。

分工是刻意的：

- **README** —— 这个 app 做什么、怎么用。面向使用者，逐个功能。
- **ARCHITECTURE.md** —— 为什么这么设计、量到了什么、否决过哪些方案。
- **`docs/FUNCTION-TEST-REPORT.md`** —— 功能域逐项核对的结果。

只描述**当下存在**的行为，不要写打算做的东西。

## 安全问题

**不要**用公开 Issue 报告安全问题。请按 [`SECURITY.md`](SECURITY.md) 的流程私下报告。

改动涉及以下几处时请格外小心，改动前先读
[`docs/SECURITY_REVIEW_2026-10-20.md`](docs/SECURITY_REVIEW_2026-10-20.md)
与 [`docs/SECURITY_FIX_PLAN.md`](docs/SECURITY_FIX_PLAN.md)：

| 区域 | 为什么敏感 |
|---|---|
| `sshclient/remote/hostkeys.py` | 主机密钥信任决策；信任策略写错就是 MITM |
| `sshclient/remote/connection.py` | 认证、算法偏好、异常分类 |
| `sshclient/core/lrzsz.py` | 远端命令拼接；拼错就是命令注入 |
| `sshclient/core/crypto.py` | 会话敏感字段落盘加密 |
| `sshclient/core/tunnel.py` | 监听地址与暴露面 |

## 行为准则

参与本项目即表示同意 [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)。
