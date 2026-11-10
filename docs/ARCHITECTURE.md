# 架构与关键决策

记录那些**代码本身看不出来**的决策:踩过的坑、为什么这么选、以及什么条件下
应该改回去。

---

## 跨平台:macOS / Linux 兼容

目标平台是 Windows / macOS / Linux 三家。可移植性上最危险的**不是崩溃,而是
静默失效**——代码在别的系统上照样 import 成功、跑起来也不报错,只是某个功能
悄悄没了,而这类问题只有在用户换机器那天才会暴露。

因此 `tests/_portable.py` 专门守这些点,并且**不模拟**别的系统:这台机器上
造出来的假平台绿灯是没有意义的。要守的是"这些表和路径在三家上都成立"这件事本身。

### 日志目录必须有多级退路

日志默认写 `<CWD>/logs`,但 **macOS 从 `.app` 双击启动时 CWD 是 `/`**,
`mkdir /logs` 直接权限拒绝。更糟的是那行代码抛在 `QApplication` 建好之前,
异常一路冒到 `main()` 外面——**整个应用起不来,连报错窗口都没有**。

所以 `_writable_dir()` 逐个试:CWD → 配置目录 → 系统临时目录,并且每一档都
先做一次真实的写入探针再认它可用。显式传 `base` 时按原路径返回(那是测试用的,
不该偷偷跑去写用户配置目录)。

> 教训:这类"启动早期"的失败路径要单独想一遍。UI 起来之后崩溃至少有弹窗,
> 启动阶段崩溃是黑屏。

### 字体目录与 CJK 兜底

`tests/fonts.py` 原先写死 `C:\Windows\Fonts`。在 macOS / Linux 上那个目录
不存在,`load_system_fonts()` 加载不到任何字体,`QFontDatabase.families()`
返回 **0**——于是**所有**基于"渲染出来有没有墨迹"的断言都变成恒真。
一套悄悄什么都没测的测试,比没有测试更糟。改成按平台枚举
(macOS 的 `/System/Library/Fonts` + `~/Library/Fonts`,Linux 的
`/usr/share/fonts` + `~/.fonts` + `~/.local/share/fonts`),并且递归匹配——
Linux 的字体常常按厂商分层,不在顶层。

`CJK_FALLBACK_FAMILIES` 同理:它原本只有 `黑体/SimHei/NSimSun/新宋体` 这几个
**Windows** 中文字体,在 macOS(PingFang/Hiragino)和 Linux(Noto CJK/文泉驿)
上一个都不存在,`cjk_fallback_families()` 恒返回空列表——**缺字兜底整个功能
静默消失**。现在三家都有代表,黑体仍排在最前以保持 Windows 上的既有选择。

界面表(`PingFang SC`/`Noto Sans`/`DejaVu Sans`)和终端表
(`Menlo`/`DejaVu Sans Mono`/`Consolas`)本来就覆盖了三家,没动。

### lrzsz:POSIX 上不能执行仓库里的 .exe

`bin/rz.exe` 是 Windows PE 可执行文件,但它在**任何** checkout 里都躺在
`bin/` 下。原来的候选名表在 POSIX 分支里也列了 `.exe`,于是 macOS/Linux 会
优先选中它,然后 `subprocess` 报 Exec format error——**而不是**报"没装
lrzsz",把一个配置问题伪装成了二进制损坏。POSIX 分支现在只认 `lrz`/`rz`,
找不到就落到 `PATH`。

### 测试里的平台 API

`_stress.py` 在模块顶层就 `ctypes.WinDLL("kernel32")` 和 `import
ctypes.wintypes`,这两样在非 Windows 上都不存在,import 就 AttributeError。
现在按平台分流:Windows 走 `GetProcessHandleCount`,POSIX 数 `/proc/self/fd`
(Linux)或 `/dev/fd`(macOS)。`handles_are_measurable()` 这个自校验在三家
上都保留——它证明计数器真的会动,否则一个坏掉的计数器看起来和干净进程一模一样。

### 还没验证的部分

本机只有 Windows,以上三条平台分支**都是靠代码审读 + 结构性断言**成立的,
没有在真实 macOS / Linux 上跑过。`docs/ARCHITECTURE.md` 的"尚未验证"一节
同样如实记录这一点:在真机上第一次跑 `tests/_portable.py` 之前,不要把
"跨平台可用"当成已验证的事实。

---

## 敏感信息的落盘加密

`sessions.json` 里的主机、端口、账号、口令、私钥路径与私钥口令,逐字段加密
(`enc:v1:` 前缀 + Fernet)。密钥是一把随机生成的 Fernet 密钥,存在配置目录下的
`secret.key`。

### 为什么是「本地密钥文件」而不是系统钥匙串

三个选项都实际评估过:

| 方案 | 防得住 | 代价 |
|---|---|---|
| **本地密钥文件**(已选) | 随手打开 `sessions.json` 看到明文密码 | 密钥和数据同目录,**同账户的其他进程照样能读** |
| Windows DPAPI 钥匙串 | 同上,且同账户的其他进程也读不到 | 数据与当前 Windows 账户绑定,换机器/换账户读不出来;要 ctypes 包 `CryptProtectData` |
| 每次启动输口令 | 最强,密钥不落盘 | 每次启动都要输;忘口令 = 数据永久丢失 |

选本地密钥文件是因为它**零配置、跨机器可移植**,而这个 app 的实际威胁模型是
"配置目录被顺手翻到 / 被同步盘带走",不是"同账户下的恶意进程"。**如果你要防的是
后者,现在这个方案是不够的**,应该换 DPAPI——那时候记得把 `secret.key` 一起
纳入备份清单,因为它现在就是数据的全部。

### 三条降级路径,一条都不许丢数据

加密最大的风险不是被破解,是**加了密之后自己打不开**。所以:

1. **密钥和密文对不上** → 抛 `CryptoUnavailable`,`SessionStore` 把
   `load_error` 填上、**一个字都不改原文件、并且拒绝再次写入**。
   静默当成空列表是最坏的结果:下一次保存就把用户所有会话连同密码抹掉。
   留着原样,用户把密钥文件找回来、重启一次就全好了。
2. **拿不到密钥(首次创建失败等)** → 退回**明文**保存,并大声记日志。
   明文不理想,但没有密钥的密文是永久读不出来的。保住数据优先于保住机密。
3. **文件真的损坏** → 沿用原来的改名保留成 `.json.bak`,不删。

### 为什么加解密放在 `SessionStore` 而不是 `SavedSession`

`SavedSession` 保持纯数据类,`to_dict()` / `from_dict()` 就是字段的忠实映射。
加解密只发生在 `SessionStore.save()` / `reload()` 这两个 I/O 关口上。这样
"这个字段在内存里是明文、在盘上是密文"这条边界是显式的,也只有一处。

### 哪些字段**没有**加密,以及为什么

`name` / `notes` / `id` / `auth_method` / `tunnels` 仍是明文:

- `id` 是查找键,密文就没法按 id 定位。
- `name` / `notes` 保持明文是为了**排障时能直接看懂这个文件**。整个文件变成
  一段 base64 之后,出问题时什么都看不见。
- `tunnels` 里的 `remote_host` **确实是内网地址,但这次没加密**——用户列举的
  是连接目标,转发规则不在其中。要一并加密的话,动的是 `TunnelSpec` 的
  序列化,会波及 111 项隧道测试,应当单独做一次并单独说明。

### 明文存量不需要迁移脚本

`decrypt()` 看到没有 `enc:v1:` 前缀的值就原样返回,所以旧的明文
`sessions.json` 读起来完全正常,**下一次保存时自然变成密文**。不需要专门
的迁移步骤,也没有"迁移到一半崩了"这种状态。

---

## 日志

写当前工作目录下的 `logs/mavisssh.log`,2 MB × 5 轮转。

放 CWD 而不是 `%APPDATA%`,是因为这个 app 在开发机上直接 `python main.py` 跑,
日志跟着项目目录走最好找。代价是从别的目录启动会写到别处——所以启动第一行
就把绝对路径打进日志。

**口令不进日志**,这条是硬要求而不是风格问题:日志通常比配置文件更容易被翻到、
被同步、被贴进 issue,一旦密码进去,加密存储就白做了。连接层只记
`谁@哪:端口 + 认证方式 + 主机指纹`,`AuthConfig` 的密码与私钥口令字段
从头到尾不进任何日志调用。`tests/_logs.py` 的 E 段做真连一次失败来验证,
不是只测 `redact()`。

`delay=True`:只是 import 了模块却没记录任何东西的脚本,不该在项目里凭空
多出一个空的 `logs/` 目录。

---

## 字体

终端默认 **Ubuntu Mono**,UI 保持 **Segoe UI Variable Text**。

本机的 Ubuntu Mono 是**按用户安装**的,不在系统级位置:

```
%LOCALAPPDATA%\Microsoft\Windows\Fonts\UbuntuMono-*.ttf
注册于 HKCU\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts
```

`C:\Windows\Fonts` 和系统级 `HKLM` 字体注册表里都查不到它。排查时容易被
"系统里明明装了"误导。

UI 字体**故意不用** Ubuntu Mono:它是等宽体,全应用使用会让每个标签和按钮
都像代码编辑器。项目早期版本全应用 Consolas 就是这个问题。

### 缺字兜底:为什么是黑体,而不是"让 Qt 自己挑"

Qt 的 `QFont.setFamilies([mono, 黑体])` 会逐字形兜底,度量只取第一个字体,所以
列网格不受影响 —— 这条路一开始就被否掉了(见下),后来又成了最终方案。真正的
理由是**默认值不对**:不指名兜底时 Qt 挑的是宋体。

这一点是量出来的,不是看出来的:把渲染位图取 `bits()` 做 sha1,`QFont("Monaco")`
画出的 `中` 与 `宋体`/`SimSun`/`NSimSun`/`新宋体` **逐字节相同**。宋体是文档字体,
细衬线挂在终端的无衬线拉丁旁边明显不对路。

> 这一条最初被我判错过。我看了张截图就断定"`setFamilies` 不生效、中文全是方框",
> 据此写了 130 行逐字符手工换字体的实现。后来做位图哈希才发现图上根本不是方框,
> 手工实现也被整段删掉。**先看图猜结论、再写"实测"的注释,是错判的主要来源。**

被否掉的方案,连同否掉的原因:

- **逐字符自己换字体**(`QRawFont.supportsCharacter` 判覆盖,逐字切 QFont)。
  能work,但绕过了 Qt 的 shaping;而且黑体同样没有 emoji,缺字会再退化成方框 ——
  `setFamilies` 会继续往后找 emoji 字体。
- **手工把宽字符缩到正好两格宽**。实测所有字号下黑体的汉字 advance 都 ≤ 两格,
  根本不需要;真要缩,box-drawing 那类只有一格的字符会被压到 6.37pt,比方框还糟。

判定"某张脸有没有这个字"要用 `QRawFont.supportsCharacter`,**不要**用
`QFontMetrics.inFont` —— 后者问的是整条 fallback 链,Monaco 单独对 `中` 也返回
`True`,是个恒真的废判据。

**离屏只影响"截图类"脚本,不影响测试。** `_runall.py` 不设
`QT_QPA_PLATFORM`,所以套件跑在真实的 `windows` 插件下,`QFontDatabase.families()`
能正常返回本机那 400 多个族 —— `_glyphfb.py` 断言黑体兜底时正是靠这个。

真正走离屏的是**调用了 `sshclient._testfonts.boot()` 的脚本**(如 `_smoke.py`)。
它里面有 `os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')`,再手工
`addApplicationFont` 把 `C:\Windows\Fonts` 灌进去 —— 离屏插件自己不建立字体库,
不灌的话任何"渲染出来没有"的断言都是空的。代价是 stderr 会多出 Qt 自己的
`Cannot find font directory` 警告,以及 `propagateSizeHints()` 不受支持。

所以:**要看字体清单用 `_fontcheck.py`,不要靠离屏脚本的 stderr**。

```powershell
.\.venv\Scripts\python.exe _fontcheck.py
```

---

## 终端解析吞吐是硬天花板,不是待优化的点

实测(`_bench.py`):pyte 的 `Screen.draw` 逐字符纯 Python 循环,约
**0.45–0.80 MiB/s**(1200–1750 ns/字符)。三个反直觉但重要的结论:

- **与屏幕尺寸无关** — 80x24 和 300x80 几乎相同,所以不是缓冲区大小问题
- **合并 chunk 无效** — 1 KiB 到 64 KiB 四档完全持平,限流合并不是解法
- **转义序列不比纯文本贵** — 成本在 `draw()` 而非解析状态机

交互使用完全没问题(一次提示符循环 70–140 µs,比 60fps 帧预算快 100 倍
以上),只有输出洪流会卡:`cat` 10 MB 文件滞后 13 秒。

**为什么不重写成 Go/Rust:** 这个墙是终端的固有属性。kitty / alacritty 用
Rust/C++ 也一样渲染不完 50 MB 输出,它们的做法是 **scrollback 环形缓冲 +
跳过不可见内容**,而不是把解析器写快。真实终端从不需要渲染每一个字节。

若确实需要,先做 scrollback + 限流(约 100 行),而不是重写。

其余路径都不是瓶颈:交互延迟 70–140 µs、paint 单元格扫描 0.9–1.9 ms/帧、
zmodem 嗅探 ~50 MiB/s、启动 434 ms。

---

## Tab 补全

策略:**远端优先(300 ms 超时),远端静默才由本地 SFTP 兜底**。

### 不要用 `os.path.expanduser` 展开 `~`

`_guess_cwd` 从提示符解析 cwd 后,曾调用 `os.path.expanduser(path)`。在
Windows 上它把远端的 `~` 解析成 `C:\Users\xxx`,于是本地 SFTP 在**本地
文件系统**上补全,用户看到的是本地文件列表而非远程内容。

现在 `~` 原样传给远程 SFTP,由服务端解析。

### 远端路径一律用 `posixpath`

`os.path.join("C:/Users/me", "/etc")` 在 Windows 上得到 `C:/etc`,会破坏
任何绝对远程路径的补全。

---

## SFTP 面板

### 一个 `SFTPClient` 不能给多个线程用

`SSHConnection.open_sftp()` 缓存并复用同一个 `SFTPClient`,它同时被三处访问:
文件面板两侧的 GUI 线程、上传/下载的工作线程、终端 Tab 补全。而
`SFTPClient` **不是线程安全的**:

- 它的 `self._lock`(`sftp_client.py:118`)**只**保护 `request_number` 记账,
  不保护通道 I/O(`_send_packet` 直接走 `self.sock.send`)。两个线程各发各的,
  报文头会交错写坏 → `SFTPError("Garbage packet received")`。
- 响应包由**读到它的那个线程**消费。`_read_response`(884-919)读到不是自己
  那个编号的响应时,若是同步请求(`fileobj` 就是 `type(None)`)就**静默丢弃**
  (914 行的守卫),等的那一方永远挂在 `recv` 上。

所以 `RemoteFileSystem` 用一把 `RLock` 把所有远程访问串起来。
用 `RLock` 而不是 `Lock` 是为了容忍重入:`download` 内部还要 `stat`,
将来某个方法转调同类方法也不会自己锁死自己。

Tab 补全的 `list_names` / `is_dir` 走的是**尽力而为**分支:抢不到锁就
抛 `TimeoutError` 由调用方放弃(补全本来就是"远端 300 ms 不响应才启用"的
兜底)。传输期间没有补全可以接受,但为了补全把界面卡住不行。

### 传输期间不能碰控件

修复前 `_run_transfer` 在 `threading.Thread` 里直接调 `progress.setValue()`
和 `QMessageBox.warning(self, ...)`。跨线程建控件是 Qt 明令禁止的,其中
模态框会跑一个**嵌套事件循环**:界面就此卡死、Windows 把鼠标画成转圈,
而真正的异常(`Garbage packet received`)反而没人看见。

现在进度、刷新后的列表一律以信号发回 GUI 线程,工作线程一个控件都不碰;
错误也不再走弹框,而是回到 GUI 线程后交给状态条的失败态(见下)。

### 工作线程是 `QObject` + daemon 线程,不是 `QThread`

`SftpPanel` 是标签页的**子控件**:关闭标签时它是被父级一起析构掉的,压根收不到
`closeEvent`,也就没有地方去 `wait()`。而一个仍在运行的 `QThread` 在析构时
是直接 abort 的(`QThread: Destroyed while thread is still running`)——
那等于把"界面转圈"换成了"关标签就崩"。daemon 线程在进程退出时干净了事,
剩下的问题只是"面板没了还在发信号",由 `_emit` 吞掉 `RuntimeError` 兜住。

`tests/_sftpsync.py` 的检查 6 专门守这条:传输刚开始就把面板析构,然后继续
转事件循环,确认工作线程自己退出了、进程没有 abort。

### `FilePanel` 要拆成"取数"和"填控件"两段

刷新远程目录是一次阻塞的 SFTP 调用。放在 GUI 线程上,传输期间一按刷新就卡;
放在工作线程上,`QTreeView` 就不能一起搬过去。所以 `fetch_async()`(只在工作
线程跑,返回 `list[FileEntry]`)与 `populate_from()`(只在 GUI 线程跑)分开,
由 `_TransferWorker` 在传输结束后取数、发信号、回到 GUI 线程填。

### 一批传输 = 一个线程,不是每文件一个

修复前 `_upload` 给每个文件起一个线程。它们共用同一个 `SFTPClient`,
互相把报文写坏。串行跑同时让进度条有确定的总进度。

进度框的"取消"只停掉进度更新并关掉框:paramiko 没有中途放弃一个 `put` 的
办法(回调返回 `False` 只是停止写,远端会留下半个文件),所以不假装能中止。

### 传输状态条:为什么不用 `QProgressDialog`

原先是 `QProgressDialog` —— 一个模态顶层窗。它抢焦点、盖住文件列表,而一次
后台传输本来就不该打断用户;观感上像是从上个时代掉进来的。现在换成贴在面板
底部的常驻状态条(`sshclient/ui/transfer_strip.py`):空闲时整个收起,传输时
出现,期间上半部分照常可用。

四件刻意的选择:

**自绘 5px 圆角胶囊,不用 `QProgressBar`。** 要的是圆角 + 渐变 + 宽度过渡;
QSS 只能给方角块,而 `::chunk` 在深浅两套主题下各要写一遍圆角和渐变。
自绘两个 `drawRoundedRect` 就够,也不用去和 QSS 抢样式表。
(踩到的:PyQt6 **没有** `drawRoundedRect(x, y, w, h, ...)` 的 float 重载,
只能给 `QRectF`。)

**首字节之前是不定态。** 点上传到 paramiko 第一次回调之间隔着连远端、开
文件、握手,可能好几秒。一个卡在 0% 的确定态条子看起来像死了,所以还没收到
第一个真实进度时走一条来回滑的亮带。

**失败停在红色失败态,不弹模态框。** 一次失败没人看见等于没报,而模态框会
抢焦点、盖住列表。状态条停在那里等人点关闭,完整原因放 tooltip。
`tests/_sftpsync.py` 检查 7 专门守这条 —— 状态条要是把错误吞了,那就只是
"拿没报错换了个好看"。

**取消是第三种收尾,不能混进"完成"。** 用户点了取消之后工作循环 `break`
出去,状态条还停在 `RUNNING`,于是收尾那段很容易顺手就写成 `succeed` ——
没传完却报"上传完成"是撒谎。所以工作线程记一个 `cancelled` 标志,由它决定
报 `cancelled` 还是 `success`。取消用灰色而不是红色:取消是用户的正常选择,
不是出事了。`tests/_sftpsync.py` 检查 8 守这条。

**宽度过渡 180ms,不用瞬变。** 百分比本来就每几百毫秒跳一次,再叠一个瞬变
看起来像卡顿。

状态条的 `format_bytes` 与 `FilePanel._format_size` **故意不是同一个格式**:
状态条要 `2.0 MB`,表格列要 `2.0M`。合成一个函数就得加 `gap`/`units` 参数去
伺候两处,反而更难读。

### 两条传输路线的编排层是合一的,协议层不合

文件 tab 走 SFTP(参数通道),工具栏 ↑↓ 走 ZMODEM(shell 通道上的裸字节复用)。
**协议层不可能合成一个实现** —— 而且 ZMODEM 传输期间终端是不可用的,因为它占用
着那同一条通道。

但它们**编排**的部分完全一样:挑一批文件、报一个(已传, 总量, 速度)、在成功/
失败/取消里挑一种收尾、留一行日志。所以抽了
`sshclient/ui/transfer_controller.py`:独占那条状态条、独占一个忙时闸门、
独占三种收尾语义。两个引擎各自只回答"怎么把字节弄过去"。

不这么做的时候,工具栏那条路是没有进度、没有取消、没有忙时守卫的 —— 于是可以在
同一次会话里先点文件 tab 的上传、再点工具栏的上传,两个传输抢同一条 shell 通道,
谁都传不完。`tests/_zmodem.py` 检查 8 守的就是这个闸门。

进度在这里归一成一个三元组 `(done, total, bps)`。SFTP 从 paramiko 回调喂进来,
ZMODEM 从"pump 自己搬过的字节数"喂进来;状态条是纯 UI,不该知道底下是哪种协议。
引擎**不再拼任何界面文案** —— 那是 `TransferReport` 的活。

**整批百分比按文件个数等权**,不按字节加权:一个 1KB 的文件和 1GB 的文件各占
1/n,这样"传到了第 3 个"和"传到了 37%"不会互相矛盾。字节数在 detail 那行里照给。

**忙时直接拒,不排队也不弹框。** 状态条就明晃晃停在"正在上传",用户看得见;
再弹一个框把它盖住,等于让人以为刚才那一下没点上。

### ZMODEM:绝对路径是静默失效,不是"传不了"

`sz` 只吃**纯文件名**加一个指向它所在目录的 `cwd`。传绝对路径会发生最坏的一种
故障 —— 实测(仓库自带的 `bin/sz.exe`,Windows,把 sz 的 stdout 直接接进 rz 的
stdin):

| 传法 | sz 退出码 | rz 退出码 | 落地 |
|---|---|---|---|
| basename + cwd=所在目录 | 0 | 0 | ✅ 收齐 |
| basename × 2(同目录) | 0 | 0 | ✅ 两个都收齐 |
| **绝对路径** | **0** | **0** | ❌ **一个都没有** |

**双方都报成功,界面显示"传输完成",远端什么都没有。** 所以这个坑被封在
`lrzsz.plan_local_upload()` 里:它吃本地绝对路径,吐 `(cwd, basenames, skipped)`,
调用方拿到的就一定是能用的形式。`ZmodemPump` 内部再挡一道(纯路径之外一律不起
进程),因为传错的代价是静默的。`tests/_zmodem.py` 检查 1 和 12 守这两道。

跨目录的文件一次送不完:远端只有一次 `rz` 在等,而 `sz` 送完自己那批就退出了,
要再送下一批得让远端重新敲一次 `rz`。所以只取第一个文件的目录,其余如实标为
"已跳过",措辞和 SFTP 面板跳过目录时保持一致。

### ZMODEM 之前先问远端有没有 lrzsz,没有就回退 SFTP

工具栏那对上传/下载按钮走的是 ZMODEM —— 往通道里写一个 `rz` / `sz`，等远端回
ZMODEM 握手。**但远端不一定装了 lrzsz。** 没装的话：远端回一句
`sz: command not found`，用户看到的是终端里一行看不懂的报错，而按钮这边
毫无反馈，状态条也不会动。

所以 `SessionTab._ensure_lrzsz()` 在**往通道写任何东西之前**先探一次
（`lrzsz.probe_remote()`），没装就弹框：给出安装命令，并让用户在
「改用 SFTP 传输」和「先不传，我去装 lrzsz」之间选。

三个关键取舍：

1. **「先不传」不能偷偷继续走 ZMODEM。** 用户说要去装，这时还发 `sz` 出去，
   只会换回第二句 `command not found`。所以这一步是**取消**，不是"试一下"。

2. **探测失败 ≠ 没装。** 远端是台机器，探测完全可能因为超时、通道半死、
   shell 非 POSIX 而失败。把一次问不出来的结果说成"没装"，会白拦一次本来好好的
   传输。所以 `RemoteProbe.ok` 和 `sz`/`rz` 分开：`ok=False` 时 `usable` 返回
   **真**，照旧走 ZMODEM。输出末尾那个 `MAVISZ done` 标记就是干这个的——
   没收到它就当作这次没问出结果。

3. **回退是"替他做完"，不是"把活儿甩回文件面板"。** 用户点了按钮、已经在
   文件框里选好了文件（或在输入框里敲好了路径），让 TA 再去文件树里点一遍是倒退。
   所以 `SftpPanel` 多了 `upload_paths()` / `download_paths()`，收显式路径，
   复用同一套任务表和状态条。

   **语义必须对齐**：ZMODEM 上传是"传到 shell 所在的那一层"，SFTP 上传是
   "传到面板当前目录"。回退时先从**提示符**里解析出 CWD（终端补全早就靠
   `_guess_cwd()` 在做同一件事），把远端面板也指过去，否则文件会落到用户没预期的
   地方。下载同理——远端是绝对路径，必须按 `basename` 落盘，拼出去就是在本地
   建出一串子目录。

   > **交互 shell 的 CWD 只能从提示符里拿。** `conn.exec("pwd")` 开的是**另一个**
   > shell（paramiko 走登录 shell），它的 CWD 永远是家目录——拿它判断交互 shell
   > 停在哪，实测会把文件传到 `~/` 而不是用户当时所在的那一层。这个坑本文件
   > 下面「对着真服务写联调测试」那节早就记过一次，这里还是踩了。
   >
   > 而且提示符**未必带尾随空格**：用户刚 `cd` 完、光标就贴在 `$` 上，真机实测
   > 就是 `deploy@host:/tmp/x$`。`_split_prompt()` 的 `[$#]\s` 要求后面有
   > 空白，这时它会把整行当成"没有提示符"，于是 `_guess_cwd("")` 给出 `~`，文件
   > 又掉回家目录。要用 `_guess_cwd()` 自己的 `\s*$`（零个空格也收）。

探测只发**一次** exec（`for t in sz rz; do ... done`），不为了两个命令各问一趟。
结果按标签缓存，`_reset_zmodem_state()` 里清掉——换了通道就不能沿用上一次的结论。

`tests/_lrzprobe.py` 覆盖，并且判据**走 `tab._on_upload()` / `tab._on_download()`
这两个真实入口**，量的是通道上真正写了哪些字节 + SFTP 排出来的任务表。
真机验过三项：装了 / 掐掉远端 `PATH`（真 SSH exec，不是喂假输出）/ 只有 `sz`。

### ZMODEM 的进度不能从 lrzsz 的人读输出里拿

那些 `Bytes Sent:%7ld/%7ld BPS:...` / `Bytes received: ...` 格式串确实躺在
二进制里(可以用 `strings` 翻出来),但那是**连着真终端时**的交互输出路径。
我们是用管道拉起子进程的,走不到那条路。实测:

```
sz 不加 -v : stderr 整个是 '\r\r',一个字都没有
sz 加  -v  : 'Sending: a.txt\rSending: b.txt\r\r\nTransfer complete\n'
rz         : stderr 永远是 'rz waiting to receive.'
    rz 加  -v  : 没用（人读输出只有 rz waiting to receive. 一句）
```

所以进度是**自己数字节** —— pump 搬过去的每一个字节都数得出来,总量在上传方向
就是本地文件大小之和(`os.path.getsize`)。这条路反而更稳:不依赖 lrzsz 的措辞、
语言版本,也不依赖它愿不愿意打印。逐文件信号则来自 `-v` 的 `Sending: <名>`。

`parse_progress()` 保留着,只当兜底(万一某个平台的构建在管道里也照打那几行)。
`tests/_zmodem.py` 检查 7 把"管道里没有 `Bytes Sent` 行"这件事钉死 —— 哪天换了
构建,那条会红,提示要重新评估这层兜底还值不值得留。

`sz` 跑在管道里时 `read1(4096)` 给的块既可能是半行、也可能含好几行,而且 lrzsz
用 `\r` 刷新同一行,所以 stderr 必须自己攒着按 `[\r\n]+` 切;拿整块去匹配正则
解析不出来。

### ZMODEM 的取消和 SFTP 不一样,文案也不能一样

SFTP 没有中途放弃一个 `put` 的办法(回调返回 `False` 只是停止写,远端会留下半个
文件),所以取消只让**后续**文件不再开始。

ZMODEM 那批文件是同一个 `sz` 进程连着送的,没有"只停下一个"的开关,
`request_cancel` 只能终止进程 —— 代价是远端可能留半个文件。因此它的收尾文案是
"已停止,当前文件可能不完整",**不能**报"完成"。两种引擎的取消语义不同,但都归到
同一个 `TransferStrip.CANCELLED` 态。

### 终端尺寸要同步到远端 PTY,节流而不是防抖

`TerminalWidget.set_resize_handler` 留了接口,`_recompute_grid` 也会在格子数
变化时调它——但**全项目从来没有调用过 `set_resize_handler`**。于是 README 里
「窗口尺寸变化会同步到远端 PTY」写了很多年,远端拿到的永远是
`open_shell()` 里写死的 100x30。截图里看不出来,只有真去拖窗口、
在远端开 `less`/`vim`/`top` 才会发现它们按错的宽度重排。

接上之后有两个细节:

**节流,不是防抖。** 拖动窗口边时 `resizeEvent` 每秒来几十次,逐次发
window-change 会把通道塞满;而纯防抖(等到停下来才发)又让整个拖动过程中远端
一直停在旧尺寸,`less`/`vim` 会持续按错的宽度重排。所以是**最多每 80 ms
发一次、每次发最新的那个尺寸**——拖动过程中远端也在跟着走,停下来时一定
是最终尺寸。

**重复尺寸不重发。** 记一份 `_sent_pty_size`,和上一次相同就直接跳过。
字体加载完导致的指标变化、布局微调都会重新算一遍格子数,逐次发纯属浪费。

**断开后摘掉回调。** `Channel.resize_pty` 带 `@open_only`,通道已关时抛异常;
而 PyQt6 里槽抛异常是整个进程 fast-fail(0xC0000409),不是只打一行日志。
`_on_channel_closed` 里 `set_resize_handler(None)`。同理,断连诊断日志里
那些通道属性一律用 `getattr` 取——测试用的假 channel 只实现了 `recv`/`closed`,
直接摸属性就会崩掉整个测试进程。

`tests/_ptyresize.py` 守这三条:真的缩放要发出 `resize_pty`、连甩 40 次要
被压到远少于 40 次、断开之后一次都不许再发。

### 这条竞态是非确定性的,守卫要测不变量

**去掉锁之后,并发 `upload` + `listdir` 有时报 `Garbage packet received`,
有时全过。** 拿"跑一次看会不会报错"当守卫就是个会偶发红的测试。

`tests/_sftpsync.py` 因此直接量那条不变量本身 —— 同时进入远程通道的最大
线程数:有锁时必须恰好是 1,去掉锁时必须 > 1(实测 3)。两条都是确定性的。
它用进程内起一个最小 SFTP v3 服务端、`socket.socketpair` 直连、中间跑**真的**
`paramiko.SFTPClient`,所以看到的是 paramiko 的真实行为,不是桩件的产物。

---

## 端口转发

### 为什么不用 `paramiko.Transport.request_port_forward` / `accept`

`Transport.accept()` 把**所有**本地转发端口共用一个队列,而且返回的
channel 的 `getpeername()` 给的是**主连接地址**而非本地监听端口,无法反查
命中的是哪条规则。

改为每条规则自绑本地监听套接字(`tunnel.Forward`),规则 → socket → channel
的映射完全确定,错误信息和 UI 才拿得到正确的规则名。

### 平台相关的 bind 选项

| 平台 | 选项 | 原因 |
|---|---|---|
| Windows | `SO_EXCLUSIVEADDRUSE` | `SO_REUSEADDR` 在 Windows 上允许**两个 socket 绑同一端口**,后绑的抢走流量 —— 两个 MavisSSH 实例能同时"监听" 13306 |
| POSIX | `SO_REUSEADDR` | 仅用于重绑处于 TIME_WAIT 的端口,不允许多个监听者 |

### 半关闭用 `shutdown(SHUT_WR)` 而非 `close()`

`_pump` 在读到 EOF 时对另一侧 `shutdown(SHUT_WR)`,这样对端看到干净的
EOF,而读方向仍可收完剩余数据。用 `close()` 会掐断返回路径,
MySQL / Redis / HTTP 客户端这类"发完请求就半关闭"的协议会拿不到响应。

### 握手期间的客户端断开要静默

端口扫描器和健康检查会不断"连上就断"。这类 `ConnectionError` 直接丢弃,
只有**协议违规**和**目标不可达**才上报为错误。

### 开关不直接写模型

隧道开关先问连接层,成功后才刷新表格。端口被占导致开启失败时,开关必须
弹回关闭状态,而不是显示成已开启。`refresh()` 每次从连接的真实状态重建
整张表,所以 UI 不可能与实际状态漂移。

---

## Windows 特有的坑

- **`SO_REUSEADDR` 语义与 POSIX 相反**(见上)
- **QSocketNotifier 与焦点链**:`TerminalWidget.event()` 必须在 Qt 内部
  `focusNextPrevChild()` **之前**拦截 Tab,否则 Tab 被用于切换控件焦点,
  永远到不了远端 shell
- **一个 channel 只能有一个 notifier**:两个 `QSocketNotifier` 抢同一个
  socket 时,输的那个读到 `b""` 并误报断开。`bind_channel(manage_notifier=
  False)` 让拥有者独占
- **离屏测试看不到系统字体**:见上文字体章节

## 无边框窗口与自绘标题栏

Windows 的原生标题栏是整套界面里唯一不受控的部分:底色由系统主题决定,
深色应用配上去就是一条突兀的亮条。所以窗口改成 `FramelessWindowHint` +
自绘标题栏(`sshclient/ui/titlebar.py`)。

**只在 Windows 生效。** macOS 的红绿灯按钮由系统绘制并带原生行为,去掉比
留着原生标题栏更糟;Linux 各家 WM 差异更大。`frameless_enabled()` 三个平台
里只对 win32 返回 True,其余保持原生边框。

**主窗口和两个对话框共用 `TitleBar`**,对话框传 `kinds=(CLOSE,)` 就是只有
关闭按钮。标题栏既然承担了"窗口叫什么",对话框内容里那个重复的大标题就
删掉了——留着是同一句话说两遍(设置框原本是"设置"对"设置",连接框是
"新建连接"对"连接配置")。副标题留着,它说的是标题栏没说的事。

`TitleBar` 用 `kinds` 决定建哪些按钮,`buttons` 列表是命中测试的唯一依据。
**不要在别处硬写 `btn_min`/`btn_max`/`btn_close`**——对话框里前两个是
`None`,写死就是一个 `AttributeError` 躺在没人走到的分支里。

### PyQt6 的 `nativeEvent` 有三个坑,每个都是直接崩进程

全部实测于 2026-10-01,现象都是**窗口一 `show()` 就 `0xC000041D`
(access violation),连 traceback 都来不及打**:

1. **不能调 `super().nativeEvent(...)`。** 一个只写
   `return super().nativeEvent(et, message)` 的空实现照样崩。不归我们管的
   消息必须 `return False, 0`,让 Qt 走它自己那条路。
2. **不能对所有消息都 `return True, 0`。** 那等于告诉系统"全部已处理",
   窗口再也不响应——实测 `show()` 直接卡死,不动。
3. **不能把定义了 `nativeEvent` 的纯 Python mixin 塞进 `QMainWindow` 的基类。**
   它是 Qt 的虚方法,sip 算不出正确的 vtable 槽位,一调就跳野指针。虚方法
   必须写在真正的 Qt 子类上,逻辑本身再挪到模块级函数。

排查这三条时踩了个岔路:一度用「猴补 `MainWindow.nativeEvent = noop`」来二分,
**结果五个变体崩得一模一样**,差点得出"和 nativeEvent 无关"的错误结论。
给建好的 sip 类猴补虚方法本身就会破坏 vtable 派发。**要二分就改源码重跑,
不要猴补**。

`ctypes.windll.user32` 那套还有一条老坑:`MonitorFromWindow` 的默认
`restype` 是 `c_int`,64 位下把 `HMONITOR` 句柄截断成 32 位,
`GetMonitorInfoW` 拿着垃圾句柄写内存 —— **必须显式声明 `argtypes`/`restype`**。

### 命中测试的判定顺序:按钮 -> 缩放边框 -> 标题栏 -> 客户区

和原生窗口一致,顺序反过来会坏掉三处:

- 标题栏占着最上面 38px,**先判它的话 `HTTOP`/`HTTOPLEFT` 永远轮不到**,
  用户没法从顶边拉窗口。
- 右上角的按钮又必须压过缩放边框,否则最右 6px 变成"拉窗口",关闭按钮点不中。
- **三个按钮都得单独判。** 漏判最小化或关闭,那个按钮就落进 `HTCAPTION`,
  被系统当成标题栏:位置和样子都对,但**点不动、也拖不动**。这条特别阴——
  直接调 `btn.click()` 的测试是绿的(它绕过了命中测试),只有真去点才发现。

还有一个坐标系陷阱:按钮的 `rect()` 是**按钮自己**的 `(0,0,w,h)`,`geometry()`
才是相对父部件的。用 `rect()` 去 `contains()` 标题栏坐标,判定区会安到标题栏
**左上角**——贴边分屏浮窗跑错地方,而"标题栏左侧可拖"那条判据照样全绿。

### 已知取舍

- **右上角让出最外侧 `CORNER_GRAB`(4px)见方给缩放角**,这样四个角都能拖。
  原来这里是"按钮优先于缩放边框",代价是四角里少一个(右上角永远
  `HTCLIENT`)。让出的尺寸**刻意小于 `RESIZE_MARGIN`(6px)**:只让角上那一小块,
  按钮本体(中心附近)仍然照旧是 `HTCLIENT`,点得中。
- **缩放光标必须自己设**。本窗口样式实测 `0x960B0000`,**没有
  `WS_THICKFRAME`**,而 DefWindowProc 只在带 thickframe 的窗口上才给
  `HTLEFT` 这类命中码套 resize 光标——和"系统不起缩放循环"是同一个根因。
  所以在 `WM_SETCURSOR` 里按上一次 `WM_NCHITTEST` 的结果设光标。
  **只接管缩放光标,其余一律放行**:终端里是 IBeam、按钮上是手型,全靠 Qt
  自己那套 `setCursor`,一律接管就把整个界面的光标都夺走了。
- **最大化按钮走 `HTMAXBUTTON`** 才有 Win11 贴边分屏浮窗;代价是它的悬停和
  点击落在 `WM_NCMOUSEMOVE`/`WM_NCLBUTTONUP` 上而不是 Qt 事件,这两个分支
  必须成对存在,少一个就是"按钮彻底没反应"。

## 弹窗统一走 `msgbox.py`

自绘标题栏装到主窗口和两个对话框上之后,剩下的消息框/文件框/输入框还是
Qt 原生那条由系统主题决定的标题栏——深色界面配一条浅灰亮条,正是这一轮
要消掉的东西。

**不能用 `QMessageBox.warning(...)` 这类静态便捷方法。** 它们在内部自己
new 一个 `QMessageBox`,没有地方插标题栏。这些调用散落在五个文件里二十
多处,逐个改成实例写法既啰嗦又**必然会漏**,漏掉的那个就是一条又白又黑
的原生栏。所以 `sshclient/ui/msgbox.py` 包一层:名字不变,行为不变(返回值、
按钮角色、`exec()` 语义都照旧),只是多带一条标题栏。

**配色不靠调用点传参数。** 那正是会漏的第二个地方——弹窗是临时造出来的,
没有构造参数能塞 palette。由 `MainWindow.apply_theme` 经 `qss.set_current()`
记一份,弹窗自己去取(`qss.current()`,没设过时回退深色,崩溃兜底框就靠这个)。

### QGridLayout 插不进去,只能"贴顶 + 顶下去"

`QMessageBox` / `QFileDialog` / `QInputDialog` 内部的 `QGridLayout` 在
PyQt6 里**一个 `insert*` 方法都没暴露**(`QLayout.insertRow` 是 protected,
Python 调不到)。所以标题栏的实现是:贴在 `(0,0)`,再把布局的顶边距加上
`TITLEBAR_H` 把内容整体顶下去。`QBoxLayout` 那种自己建的对话框
(设置框、连接框、转发框)则直接 `insertWidget(0, tb)`,干净得多。

**顶边距必须在 `showEvent` 里设,不能在构造时设。** `setText` / `setIcon`
会让 `QMessageBox` 重排布局,之前设好的边距被冲掉。症状是正文被标题栏
盖掉一半,看着像"内容被裁了",其实是没顶下去。`showEvent` 在正文设完之后
才跑,时机正好。

### 弹窗要比主窗口亮一档

弹窗和主窗口如果同色,弹出来就是"窗口里糊了一块",用户看不出哪儿是哪儿。所以
调色板里多了一个 `bg_dialog`,弹窗那一层单独用它:深色 `#1e2431`(主窗口
`#0d0f14`),浅色 `#ffffff`(主窗口 `#f4f6f9`)。两套主题的色差方向相反
(深色变浅、浅色变白),承诺是同一条:**弹窗不能和窗口一个颜色**。

值是**对着渲染图挑的**,不是随手取的。深色主题整个色阶只有
`#0d0f14 → #1b1f29` 这 20 个值,而按钮底色就是 `bg_elevated` `#1b1f29`——
弹窗底色撞上按钮，按钮就消失了。所以弹窗只能落在 `bg_elevated` 以下。
`bg_surface` `#14171f` 偏暗、太看不出来,`#1e2431` 刚好:一眼分得出,按钮仍然
浮在上面。

`build_qss(p, dialog=True)` 换掉的是**整张表的根底色**,不只是给 `QDialog`
加一条规则。因为 QSS 里 `QWidget { background-color: … }` 是兜底规则,Qt 的
类型选择器会匹配到每一个控件——只改 `QDialog` 的话,弹窗里每个 QLabel 底下
还是旧的 `bg_base`,深色主题下就是一块块更暗的补丁。实测这个补丁不小:
设置框里 16 万个像素。

> 判据上对应的一条:别写"整个弹窗必须是同一个颜色"。文件框里的文件列表
> 本来就该是 `bg_surface`。要钉的是"**弹窗里一个主窗口底色的像素都不该有**",
> 那才是兜底规则漏换的样子。

### 挂上 `window.titlebar` 是拖动的前提,漏了不报错

`titlebar.native_event()` 靠 `getattr(window, "titlebar", None)` 找标题栏,
找不到就**静悄悄返回 `(False, 0)`**。于是对话框的标题栏照样画得出来,可
拖动和边缘缩放整个是死的,而且一点报错都没有——**截图完全看不出来**。

所以 `TitleBar.__init__` 里直接 `window.titlebar = self`,而不是让每个调用点
自己挂。`inject_chrome` 有三个调用方,逐个挂就是逐个可能漏。

> 判据上对应的一条教训:凡是"用户能拖/能点 X"的断言,量 `nativeEvent` 有没有
> 真的把消息交给 `handle_native`,不要只量标题栏画出来了。**画得出来不等于
> 点得动。**

---

## 测试

全部是独立可执行的脚本,不引入 pytest 依赖,便于在没装开发环境的机器上复跑。
**平时不要单跑某一套,跑 `_runall.py`** —— 它给每套独立进程和独立期限,单跑
看不出"这套把另一套搞挂了"。

```powershell
python _runall.py
```

单独复跑某一套(最慢的两套是隧道与压力):

```powershell
.\.venv\Scripts\python.exe _tunnel.py    # 端口转发,111 项
.\.venv\Scripts\python.exe _stress.py    # 压力与泄漏,29 项
.\.venv\Scripts\python.exe _bench.py     # 热点剖析
.\.venv\Scripts\python.exe _fontcheck.py # 字体核对(必须真实平台)
```

只做测量的脚本刻意**不在**回归里:`_bench.py`、`_fontcheck.py`、`_shots.py`
的结论只在真实 `windows` 平台下成立,塞进套件里等于让回归结果依赖本机状态。

### 测量本身要先自校验

`_stress.py` 的句柄计数第一版返回恒为 0 —— ctypes 默认用 `c_int` 传参,
64 位伪句柄被截断,`GetProcessHandleCount` 静默失败。结果所有"无句柄泄漏"
的断言都**空洞通过**。

现在计数前有一道闸门:先开 40 个 socket 确认计数真的动了、关掉又回落,
才允许跑泄漏断言;不可测量就整体退出,不让结果冒充通过。

任何"资源没泄漏"的结论,都要求测量工具本身先被证明有效。

### 不许用固定 sleep 等异步事件

由 `QSocketNotifier` 驱动的东西,固定 `spin(200)` 是时序脆弱的——同一个
测试隔离跑通过、批量跑失败。改用 `wait_for(pred)` 轮询到条件成立或超时。

---

## 命令片段库

### 需求不是「命令历史」,而是「不想背那些长命令」

评审阶段就把一个伪需求否掉了:**命令历史已经有了**。`↑` / `↓` 是原样转发给
远端 readline 的(`terminal_widget.py` 里没有本地历史),而远端 shell 的历史
本来就比客户端全。真正缺的是另一件事:有些命令**太长、背不住、每次都要查** ——
`ss -tulpn`、`journalctl -n 100 --no-pager` 这类。片段库解决的是"记不住",
不是"忘了刚才敲过什么"。

被否掉的另外两个方向,理由一并记在这儿:

- **终端分屏**:风险最高、收益最低。同一 session 的 channel 数量本来就受
  `open_channel` 的竞争约束,再加一路并发读会直接踩到 SFTP 那条已知竞态。
- **跨会话全局搜索**:N 条 channel 同时去问远端,撞的是同一个
  `open_channel` 竞争;而且用户搜的多半是"这条命令以前在哪台机器上敲过",
  那是 shell history 的活。

### 唯一不可让步的一条:只插入,不执行

片段写进的是**远端的行编辑缓冲**,插入之后远端什么都没发生 —— 用户可以退格、
Ctrl+U、Ctrl+C。加一个 `\r` 进去就把"我想看看这条命令会干什么"变成了
"它已经干了",在生产机上不可逆。所以 `tests/_snippets.py` 在**字节层面**断言:
发出去的东西必须恰好等于那条命令,`\r` / `\n` / Tab 一个都不许多。

**同理不清行。** 本地并不掌握远端行缓冲的真实内容 —— readline 补全过的、
含多字节宽字符的、用户自己退格过的。硬猜着发 N 个退格键只会删错字符,而且
删错是不可见的(多一个少一个退格,肉眼未必发现)。面板底部常驻一句"想换命令
先按 Ctrl+U"。

### 传输期间必须禁用,而不是"插了大概率没事"

ZMODEM 跑的时候,`SessionTab` 那个 `QSocketNotifier` 是通道的**唯一**读者,
`self._zmodem_pump is not None` 分支会把每一批字节直接喂给 pump。这时候从
界面写进去的字符会被当成 ZMODEM 载荷,结果是传输流里多了一段乱码。

判据用 `TransferController.busyChanged` 而不是面板自己猜。信号**只在真的翻转
时发一次**:`begin()` 被闸门拒掉不发、`_finish()` 撞上"没有在传的批次"提前
return 不发、`reset()` 在本来就空闲时也不发。少了这个约束,面板会被无意义地
禁用又启用,调用方还得自己去比对上一个值。

双击列表这条路**不经过按钮**,所以 `_on_insert` 里还要再挡一道闸门 ——
只有按钮的 `setEnabled` 是不够的。

### 面板不抢焦点

`Qt.Tool` + `WA_ShowWithoutActivating`:叫出面板时键盘焦点**留在终端上**。
用户正在敲命令的时候弹个面板把键盘夺走,打一半的字就得到别处去了。

真去点搜索框/列表时焦点才过去,而那时终端**没有**焦点,按键自然漏不过去 ——
不需要再写一层"按键要不要转发"的判断。(`WA_ShowWithoutActivating` 只影响
`show()` 那一下;之后用户点面板会正常激活它,`setFocus()` 对未激活的窗口无效,
所以测试里要显式 `activateWindow()` 才模拟得上真实点击。)

`Esc` 在搜索框里被 `eventFilter` 接管:先清空搜索词,再按才关窗。`QLineEdit`
默认就把 Esc 吞了(什么都不做),窗口级的 Esc 又要关窗,两个撞在一起会让用户
按一次没反应。

### 片段库是**整个进程**一份,不是每个标签、也不是每个窗口一份

`SessionTab.snippet_store` 走 `self.window().snippet_store`,和 `attach()` 里
`self._settings` 同一个路子。**不兜默认值**:无 parent 的 `SessionTab()` 在测试
里到处都是,兜一个 `SnippetStore()` 就会造出第二份,两边各写各的文件,用户的
片段看起来"存了又没了"。

`window() is self` 那个判断不能省:没有窗口祖先时 Qt 的 `window()` 返回自己,
`getattr(self, "snippet_store")` 于是又回到这个 property,**无限递归**。

再往上一层,`MainWindow` 也不自己 new,而是取模块级的 `app_snippet_store()`。
一窗口一份的话,两个窗口同时开着时 w1 刚加的片段会被 w2 的 `add()` 静默覆盖 ——
磁盘上只剩 w2 那份,而 w1 内存里还以为自己有。单窗口是常态,但这条不该只在
常态下成立。

`MainWindow.__init__` 的 `snippet_store=` 参数是给测试用的:缺省那个指向真实的
`%APPDATA%`,测试直接 `MainWindow(st, store)` 再 `.add()` 一条,就是每跑一次
往用户自己的配置里塞一条垃圾。**踩过,用户的文件里一度积了 15 条同名片段。**

### 闸门的起点比忙时闸门更早

判据是 `SessionTab._snippet_block_reason()`,**比 `TransferController.busy` 宽**。
忙时闸门在 `begin()` 那一刻才锁,但下载方向上按下按钮之后 `sz` 命令**已经
发出**了,本地 `rz` 还要等 500ms 才起。那半秒里 `busy` 还是 False、按钮是
使能的、`_insert_snippet` 返回 True,插进去的字节被 `sz`/`rz` 当成传输载荷
吃掉。上传方向更糟 —— 等 ZRQINIT 的窗口**没有上限**,远端 `rz` 可以一直等。

按钮状态和 `_insert_snippet` 共用这一个谓词,免得"按钮禁了但双击还能插"。
`_zmodem_pending_direction` 做成 property 而不是裸字段:它有 11 处赋值点,
挨个补一句刷新通知是典型的"漏一处就静默失效"。

### 闸门要同时关住终端,不只是片段面板

上面那个判据**三处共用**:片段面板能不能插、终端能不能收按键、传输能不能开始。
它们是同一个物理约束的三面 —— 通道同时只能属于一边。

只做片段面板是不够的。评审实跑:传输期间面板按钮已经禁了,终端那边照收 ——
按字母、Tab、↑ 全部写进了 ZMODEM 的载荷流。`TerminalWidget._send` 现在是
**唯一收口**(`set_input_blocked`),按键、粘贴、补全、退格全都从这里过。
一个收口比 N 个补丁可靠,新增输入路径不会漏。

`_on_tab` 原来有个 `if self._zmodem_pump is not None: send(b"\t"); return`
的分支,内容**和下一行完全一样** —— 它什么都没挡住,只是让人以为挡住了。

### 模态框不能开在 socket 槽里

`QSocketNotifier` 是**电平触发**的。`_start_zmodem_upload` 里原本有个
`QFileDialog`,一弹就进嵌套事件循环,通道里那批还没读完的字节会**再次**触发
同一个槽 —— 实测叠出 2 层文件对话框,同一批 ZRQINIT 往终端画了 2 遍。
推迟一个事件循环(而不是加标志位)让 notifier 先把这一批读完。

### 通道归属的四条改道,以及为什么它们共用一个判据

- 闸门起点比忙时闸门更早(见上)
- `_break_remote_wait` **不得**打断一个已经在传的 ZMODEM:那条 `sz`/`rz`
  的载荷流正占着通道,插进去的 `\x03\r\n` 会被当成传输数据,远端那半个文件
  就此报废
- 下载/上传的早退检查要在**往通道写东西之前**问,而不是发出去之后再补一个
  Ctrl+C —— 那个 Ctrl+C 本身就是往载荷流里塞字节
- `TerminalWidget.set_input_blocked` 收口所有输入

存储规矩照搬 sessions.json 那次事故的教训,并在复审后又加了两条:

- 独立 `snippets.json`,读用 `encoding="utf-8-sig"`;
- **`load()` 之后绝不调 `save()`**。写入只发生在真的改了东西的方法里;
- 写盘用 `tempfile.mkstemp` + `os.replace`:中途崩了留下的是完整的旧文件
  而不是被截断的新文件,而且**临时文件名每次唯一**——固定名的话两个进程
  各写各的,读者会在 replace 之前读到别人写到一半的内容(实测 4 进程 × 40 次
  × 400KB,读方撞到 61 次非法 JSON)。原子写只对崩溃成立,对并发不成立;
- **解码和解析两步都要兜**。`UnicodeDecodeError` 是 `ValueError` 的子类、
  **不是** `OSError`:用 `read_text() + except OSError` 的话它会一路穿透
  `SnippetStore.__init__` 冒到 `MainWindow.__init__`,而那里没有 try ——
  一个 PowerShell 5.1 `Out-File` 写出来的 UTF-16 文件就足以让**整个应用
  起不来**。`utf-8-sig` 只挡 BOM,挡不住编码本身;
- 读失败时**改名隔离成 `snippets.json.bad-N`**,不是留在原地等下一次 save
  覆盖。只做到"构造时不覆盖"是不够的——用户下一次 add 就盖掉了,那还是
  永久丢失。隔离之后原字节还在,用户救得回来,新文件能从内置集干净重建。

### 命令里的换行要在入库时拒,不是发送时绕

插入是逐字节原样发出去的,命令里一个 `\n` 就等于客户端替用户按了回车,
把后半截执行掉。所以 `add()` / `update()` / `from_dict()` 都在**入库时**拒收
含 CR/LF/VT/FF/NEL/U+2028/U+2029 的命令。首尾的空白照常 strip 掉(粘贴时
带个尾换行是最常见的),危险的只是**中间**的换行。

**不可见字符同理,而且标题也要查。** U+202E(RIGHT-TO-LEFT OVERRIDE)能让
`rm -rf /tmp/x` 在界面上显示成完全不同的样子,而双击执行的是屏幕上那串字符 ——
**列表里显示的正是标题**,所以 RLO 出现在标题里的危害比出现在命令里更大。
零宽字符(U+200B-200F)、bidi isolate(U+2066-2069)、U+FEFF 一起挡在入库口。

判据本身也有个坑:测试里**必须用 `chr(0x202E)` 显式构造**,不能写字面量 ——
字面量会被编辑器、格式化、复制粘贴悄悄规范化掉。我第一版就是这么写的,
九个用例里有五个的判据里根本没有那个字符,断言恒真。测试里为此加了一条
「判据里真的有那个字符」的元断言。

**这一条第一版写反了**:测试里断言「命令里本来就有换行时如实发出(不吞不改)」,
把 bug 钉成了正确行为。复审拿真实 Ctrl+V 粘贴三行复现:一次点击执行了三条
命令,正是这个功能声明"不可让步"的那件事。

### 内置集为什么只有只读命令

内置的十条全是只读、不需要参数的(`df -h`、`free -h`、`ss -tulpn` …),每条
注明它回答什么问题。需要参数的做法成不了"点一下就好",用户还得去改;带 `>`
的更是事故。

`audit_builtins()` 只守**出厂时塞进去的东西** —— 用户自己写 `rm -rf` 是他自己
的事,面板不拦。**第一版这个检查几乎没用**:它只 `lstrip("|&;")` 削**开头**、
只把 `>` 当独立 token 判,16 种危险写法里放过去 12 种,包括 `ls;rm -rf …`、
`$(rm -rf …)`、`curl … | sh`、`find . -delete`,以及 `echo x >/etc/hosts`
(**没空格**的重定向,直接违反本文件顶上那条"不重定向"的硬规矩)。现在的判据
是「整条命令里搜危险模式」+ **子命令表**(`git clean -fdx` 的第一个词是无害的
`git`,危险全在第二个词上),并且先把 `2>/dev/null`、`2>&1` 抹掉——不抹的话
它会把我们自己那条 `du -sh /* 2>/dev/null | sort -h` 判成违规,而一个天天
误报的检查总有一天被人直接关掉。

测试必须往表里塞回这些写法证明自检会报,否则一个什么都不检查的函数和这个检查
长得一模一样。**同理**要有"正常命令不被误伤"那几条,否则收紧判据的代价看不出来。

## 协议选择:GPL-3.0-only 不是偏好,是被依赖钉死的

项目发布为 **GPL-3.0-only**(`LICENSE`)。这是一条**约束**而不是选择:直接依赖的
PyQt6 发行包元数据是 `License-Expression: GPL-3.0-only`,另有 Riverbank 商业
授权;Qt 的 LGPL 条款只给 PySide6。任何宽松协议(MIT / Apache-2.0)都会与 PyQt6
的条款冲突。

**改协议之前必须先改依赖**,顺序不能反:

1. 迁移到 PySide6(评估见 `PYSIDE6_MIGRATION.md`,当前结论是不迁),或
2. 买 Riverbank 商业授权。

反过来——先改协议再想依赖——就是发一个侵权的包,而且**它在测试里完全正常**,
所以 `tests/_readme.py` 里钉了几条:LICENSE 真的是 GPLv3 全文、README 真的是
GPL-3.0-only、`LICENSES/` 里没有一份没人引用的协议全文。

### 随包二进制是义务的来源,不是顺手的赠品

`bin/` 下的 `rz.exe` / `sz.exe`(lrzsz-win32,GPL-2.0-or-later)与
`msys-2.0.dll`(MSYS2 runtime,GPL-3.0-or-later)让 Windows 用户开箱就能做
ZMODEM;代价是分发时必须附对应源码的获取途径与两份协议全文。已经落地的是:
`LICENSE` / `LICENSES/*.txt` / `THIRD_PARTY_NOTICES.md` 既进安装向导的许可页,
也进安装目录与打包产物(见 `installer/mavisssh.nsi` 与 `mavisssh.spec`)。

**想撤掉这份义务**:删掉整个 `bin/` 即可,程序会改从 `PATH` 找 `rz`/`sz`,
找不到时明确提示。macOS / Linux 本来就走系统包管理器,不受影响。

## 尚未验证

### PyQt6 生命周期：close() 不等于销毁

评审实测:开 6 个标签(每个带一个片段面板)再全部关掉,`SessionTab` 和面板
**原样存活**。`QWidget.close()` 只发 `closeEvent`,`removeTab()` 只从布局里摘掉。
上一版的"修复"只是 `panel.close()` —— 那等于把窗口藏起来,而提交信息写的是
"实测漏 20 个已修"。**说得比做的多,是这类提交最坏的一种。**
现在 `_on_tab_close` 在 `removeTab` 之后显式 `deleteLater()`,并用 `sip.isdeleted()`
逐个核对。

顺带一条 PyQt 事实:在测试里**判断对象有没有被销毁要用 `sip.isdeleted()`**,
不是看内存曲线,也不是"看着没了"。

### 测试脚本不收尾 = 退出时偶发 `0xC0000005`

有几套脚本建了 `MainWindow`、`SessionTab`、一对 socket,甚至一个模块级
`QTimer`,然后直接 `sys.exit()`。Qt 对象的寿命于是比 `QApplication` 长,
退出过程中只要有一个待触发的回调摸到正在销毁的 C++ 对象,整个进程就是
`0xC0000005`(access violation)。

**它最难缠的地方是"看起来通过了"**:崩的那一次,断言全绿、结论行照打
(`RESULT: ... OK` 已经打印出来了),只是进程在最后一步死了。而
`_runall` 只看退出码,所以它被记成一条 FAIL——**看起来像回归,其实是收尾
问题**。实测 `_tabwire.py` 和 `_tabclose.py` 各 30 次崩 1 次。

**收尾范式**(和 `_on_tab_close` 那边一致):

```python
ch.close()                    # socket / channel
mw.close(); host.close()      # 只发 closeEvent,不回收
mw.deleteLater(); host.deleteLater()
QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
check('真的被销毁了', sip.isdeleted(mw))
```

**判据要写成"对象真的没了",不能写成"跑了 N 次没崩"。** 后者只在这一轮
成立,而且 0/30 对 1/30 在统计上根本不显著(p≈0.37)。`sip.isdeleted()` 是
确定性的,而且下次任何人不小心把 `sendPostedEvents` 删了,它当场就红——
已实测过这个负向对照。

> 同一条家族教训:**每三十轮误报一次的套件,迟早被人当成"偶发,不看"。**
> 一旦某个失败被归类成"偶发",它就会一直在那儿红,直到没人再相信门禁。

### 终端绘制:空屏和满屏一样慢,是因为在画空格

用户报"敲几条命令之后光标闪烁变慢、响应也慢"。查下来的结论和直觉相反:**每帧
的绘制成本是恒定的**,不随命令数增长。真正的问题是**单帧太贵,队列排起来了**。

在**真实 Windows 平台**上量 `paintEvent`(不是 offscreen——那是软件光栅化,
数字会骗人):

| | 改之前 | 改之后 |
|---|---|---|
| 空屏 | 62.8 ms | 15.9 ms |
| 满屏 | 55.0 ms | 15.9 ms |
| 满屏 + 彩色 | 58.3 ms | 31.7 ms |

**空屏和满屏一样慢**——这一条就是线索。原因是 pyte 把空白格的 `data` 填成
`" "`,而 `if data:` 对空格是真值,所以原来**整屏每一个空格都在 `drawText`**。
一屏 119x35 = 4165 格,就是 4165 次"画一个什么都看不见的字符"。

一次重绘 60ms 意味着事件循环每秒只能服务 16 次重绘,光标闪烁定时器和按键事件
全排在后面——**"多敲几条命令"不是每条变慢了,是攒的输出要画的帧变多了,队列
越积越深**。

改了三处,都不改变"画出来是什么":

1. **空格不画**(带下划线的空格例外,它下面那道线看得见)。
2. **颜色解析和 `QColor` 构造都查表**。`_color_to_hex` 原来每格调两次,一屏
   八千多次 Python 层字符串处理;取值空间就那么几十条(16 ANSI + default +
   偶发 #rrggbb × bold),缓存之后塌成字典查找。**换主题必须清空**——缓存键里
   没有 ansi 调色板,而它是随主题变的。
3. **颜色没变就不重设 pen**,原来是每格无条件 `p.setPen(fg)`;QFont 的 8 种组合
   也缓存,原来每格新建一个,还每格调一次 `p.font() != base_font`。

验"没变":同一帧内容(16 色 / 粗斜下划线 / **带下划线的空格** / 反色 /
**反色里的空格** / 中文宽字符 / 256 色 / 行尾空格)在改前改后
**逐像素指纹完全相同**。

> **量的时候踩了两个坑，都记在这儿。**
> 一是拿 `widget.grab()` 当绘制耗时的度量——它的成本和屏幕内容**无关**
> (空屏满屏一样),根本不是用户看到的那条路径,量了三次才发现。要直接给
> `paintEvent` 计时,走真实的 `update()` + 事件循环。
> 二是先在 offscreen 平台上量——那也是软件光栅化,拿它的数字去说用户"卡"是不
> 成立的。**要在用户真正用的那个平台上量。**
>
> 这条修完还剩约 15ms/帧(大头是 paintEvent 自己的 Python 循环和 pyte 的
> `render`)。还能再快一截的做法是**把同属性的连续字符合成一次 `drawText`**,
> 但那会改变字形定位方式、有画歪的风险,这次没做。也没有为此加计时断言——
> **刚给 `_tunnel` 修完一个 4 轮红 1 轮的竞态,转头再往门禁里塞一个计时测试是
> 自相矛盾**。机制记在这儿,比一个会误报的断言管用。

### 定时器会让测试整轮死掉,而且不带 traceback

`_on_download` 排了一个 500ms 的 `QTimer.singleShot`。测试里走完这条路径
就把 tab 放开了,那一拍会在**后面某一组**触发,那时 tab 可能已被 GC ——
表现是整个测试进程 abort,**连一行 traceback 都没有**(0xC0000409)。
写这类用例之后必须把事件循环排空(`settle(app, 700)`),或者显式持有引用。

### 探针自己也会骗人（这一轮踩了三次）

- `import sip` 在 PyQt6 里不存在(要 `from PyQt6 import sip`),而 `2>&1 |
  Select-Object -Last N` 会把 traceback 截掉,于是"进程 abort 无输出"这个
  判断是错的。
- 拿 bound method 比身份要用 `__func__`/`__self__`,**`is` 永远 False** ——
  每次取 `self._x` 都是新对象。探针因此报"没有推迟到事件循环外",而生产
  代码明明推迟了。
- 构造了 `MainWindow(...)` 不留变量引用,下一行就被 GC 回收,Qt 连带析构
  子控件,报 `wrapped C/C++ object has been deleted`。和「`deleteLater()`
  不真的析构」正好是一对。

**macOS 与 Linux 上一次都没真机跑过。** 本节开头那几条跨平台改动——日志目录
退路、字体目录枚举、CJK 兜底表、`_stress.py` 的 fd 计数——全部是**代码审读 +
结构性断言**成立的,不是实测。在真机上第一次跑 `tests/_portable.py` 之前,
不要把"跨平台可用"当成已验证的事实。真机上最可能先炸的三处按嫌疑排序:

1. `tests/fonts.py` 的字体目录——macOS 的 `/System/Library/Fonts/Supplemental`
   字体数量远超 `addApplicationFont` 的合理处理范围,`boot()` 可能明显变慢。
2. `logs/` 落在 CWD 时,macOS 沙箱可能连项目目录都不可写(退路会兜住,但用户
   找不到日志会在别处)。
3. `QFontDatabase.families()` 在各家返回的族名大小写与别名不同,策展表里的
   名字可能匹配不上——比如 macOS 的 `Menlo` 在某些版本叫 `Menlo Regular`。

**真实 paramiko `direct-tcpip` 通道上的中继路径没有端到端跑过。** 测试里
`open_channel` 注入的是本地 socket。paramiko.Channel 提供
`recv`/`sendall`/`close`/`settimeout`/`shutdown`/`fileno`,socket 全部满足,
契约一致,但这仍是代理验证。

需要连真实服务器验证,最能暴露问题的是半关闭场景:用 MySQL 客户端或
`curl` 经隧道访问,它们都会在发完请求后半关闭写方向。

**SFTP 传输没有对真实服务器端到端验证过。** `tests/_sftpsync.py` 用的是进程内
的最小 SFTP v3 服务端 —— 协议报文是真的,paramiko 客户端也是真的,但服务端不是
OpenSSH。缺的是真实服务端在并发/大文件/慢链路下的时序,以及真实服务端对
`stat` 确认(`put` 结束时会 `stat` 比对大小)的响应。首次对真机验证时,最值得
盯的是大文件上传中途在远端侧另开一个 SFTP 连接 —— 那是最容易撞上通道竞争的
用法,而它属于**服务端**行为,本文件测不到。

### 焦点组是这个套件里唯一会随机崩的地方（已修，但原理要留着）

拿未改动的 HEAD 跑 `tests/_snippets.py` **15 轮，4 轮是红的**，其中两种表现：

1. `QTest.keyClick(app.focusWidget(), …)`。焦点没落在任何控件上时它是
   `None`，而 `keyClick(None, …)` 触发的是 **Qt 自己的 assert**
   （`qtestkeyboard.h:194` 的 `"widget"`）—— 整个进程 `0xC0000409` 死掉，
   **一行 traceback 都没有**。崩比 FAIL 更糟：它把这一组后面所有断言一起带走，
   看上去像"整组坏了"，其实只是没拿到焦点。
2. 焦点没抢到还继续断言"按键发到了 X"，红的是**环境**不是代码。而一个
   每三四轮误报一次的测试，迟早被人当成"偶发，不看"。

`setFocus()` 是个**请求**不是命令：`Qt.Tool` 窗口在 Windows 上没真正激活时它
经常不生效。修法是两头一起上——发键前复检焦点（漂了就记成"跳过"而不是失败），
并且把跳过**单独打印出来**，因为"跳过"和"通过"在输出里长得几乎一样。

顺带挖出一个**真的产品 bug**（不是测试问题）：`_escape()` 原来按固定顺序扫
`(搜索框, 标题框, 命令框)`，于是搜索框里有残留、而你正在命令框里打字时，
按一次 Esc 清掉的是搜索框——离手最近的那个反而没反应。中间一版改用
`hasFocus()` 反推，方向对了但焦点是抢来的，还是不稳。**现在由事件本身
告诉我们键发到了哪儿**：三个输入框都装了 `eventFilter`，`_escape(where)`
直接用 `where`。判据要落在**确定的东西**上，不要去反推一个抢来的状态。

### 终审挖出来的几条（5 位专家实跑复核，0 票无条件同意）

这一节记的是**评审实证过、而代码当时确实错**的部分。写成"已修"没有意义，
有意义的是**它们为什么能溜过去**。

1. **「把每个选项都当成带值」比「只看第一个词」更危险。** 剥前缀链的第一版
   按"包装词会带值"来判断，于是 `sudo -n rm -rf` 里的 `rm` 被当成 `-n` 的值
   吃掉——而 `-n`（non-interactive）根本不带值。35 条对抗用例漏 11 条，
   **全是这一个根因**，而且会连坐：`sudo -n git clean -fdx` 的子命令判据
   一起被打穿。正确的判据是**按选项列**（`_WRAPPER_VALUE_OPTS`），外加三条
   规则：`--` 终止解析、附着式（`-n5`）不再吃、`--user=root` 不再吃。
2. **头词必须从原始段取，不能从 `shlex` 的结果取。** `shlex` 在 POSIX 模式下
   把反斜杠当转义符，`C:\Windows\…\busybox.exe` 会被吃成 `C:W…busybox.exe`，
   之后再切分隔符也切不开。**判定在两个平台上不对称，等于跨平台守卫没覆盖到它。**
   再加一条：`.exe` 后缀不剥的话 `busybox.exe` 压根不等于 `busybox`。
3. **一个「括号包起来的字符串」加了个尾逗号，就从字符串变成了 1 元组。**
   `_INVISIBLES` 原本写成若干相邻字面量（无逗号），`_has_invisible` 里的
   `any(c in command for c in _INVISIBLES)` 对字符串和元组都成立，所以两种
   写法都"能跑"。直到给 `_INVISIBLES` 加 `\t` 时顺手补了尾逗号——
   `c in <1 元组>` 变成"元素相等"而不是"子串包含"，**整张表静默失效，
   而没有一行语法错误**。现在它是带逗号的真元组，注释里也写了这件事。
4. **"退出编辑态只有两条路"这句话被证伪了三次。** 只堵了「保存」和「取消」
   两个按钮，结果评审从**三条不同的路**各找到一次：关窗（`main_window` 复用
   同一个面板实例，重开后命令框回车**静默覆盖**已有片段还报「已保存」）、
   隐藏、点「删除」。现在 `closeEvent`/`hideEvent` 都会退出编辑态，而且
   编辑态下插入/删除/编辑/恢复内置/搜索框全禁。
5. **「把失败改成跳过」会把真回归一起跳掉。** 焦点抢不到时记成"跳过"是对的，
   但**跳过不进退出码**就等于没记：评审实测「只坏焦点」和「焦点坏 + Esc 逻辑
   也真坏」两种情况输出**完全一样**，而退出码只看 `FAIL` —— 真的回归被静默
   跳过，整套测试还报成功。现在跳过返回 **exit 2**，`_runall.py` 单列一类，
   最后也让它 exit 1。同一批还修了 offscreen 早退：原来那里 `return`，
   第 13~30 组、汇总、退出码全都不跑，而退出码是 0。
6. **元断言只能覆盖它自己替换掉的那部分。** 第 29 组的"装回缺陷版"替换的是
   `_resolve_head`，所以「绝对路径 rm」（量的是路径剥离）和 `git -C clean`
   （量的是 `_skip_global_opts`）**本来就不该跟着变红**。把它们硬塞进去，
   阈值只能被迫写松——评审实测阈值 5 时，7/12 条失效那条元断言**照样是绿的**。
   现在两处都拆出去了，阈值要求**全部**变红。

### 真机联调:WSL2 上跑通的那一次(2026-10-01)

`tests/_live.py` 是这个仓里**唯一一个**对着真服务器跑的脚本(其余全套都是
离线的)。连接信息只从环境变量读,没给就 `exit 2`(跳过),而且**故意不接进
`_runall.py`** —— 它依赖一台外部服务器,塞进回归门槛里,网络抖一下就把门禁
弄红。

一次真跑的实测环境:**WSL2 Ubuntu 24.04.5 / OpenSSH 9.6p1 / GNU lrzsz**
(`/usr/bin/sz`、`/usr/bin/rz` 都在,响应里能看到 `**B0100` 那个 ZRQINIT,
是真的 ZMODEM 实现)。客户端仍是 Windows。

**这一趟验掉的(之前全在「尚未验证」里):**

1. **ZMODEM 端到端跑通了,两个方向都通。** 远端 GNU lrzsz ↔ 本地仓库自带的
   trzsz。下载方向文件落地且**逐字节一致**(含中文);上传方向两个文件都到了
   远端。这条之前只在管道里对灌过。
2. **跨线程收尾在真机上是对的。** 之前只在测试里用 `_on_proc_exit` +
   `processEvents()` 驱动过。实测:文件落地之后本地 `rz.exe` **还要再花 4~5
   秒**走完 over-and-out、等进程退出(rc=0)、再把 `_settle` 从 pump 的等待
   线程排回 GUI 线程,闸门才放开。收尾之后 `busy=False`、`strip.state` 是
   `success`、状态条退场、`_zmodem_pump` 清空、通道判据归空、终端又能发字节。
   **测这条时不能一看到文件落地就去断言 `busy`** —— 早一步量到的必然是
   True,而产品一点问题都没有(第一次就是这么误报的)。
3. **PTY 尺寸同步是真的。** 判据是远端 `stty size` 报出来的数字,不是"我们发过
   `resize_pty`"。真机实测远端 `行 列` 与本地格子数一致。
4. **SFTP 全部真跑通**:建目录/上传/下载/重命名/800KB 大文件/删除,含中文的
   内容逐字节一致,行尾没有被改成 CRLF。
5. **端口转发真的转了字节**:本地端口 → SSH → 远端 `python3 -m http.server`,
   经隧道取到了远端的目录页;开关、SOCKS5 握手(`05 00`)、重复登记同端口被拒
   都成立。

**这一趟挖出来的缺陷(都已修):**

- **宽字符在每次缩放后被永久撑开。** pyte 把宽字符写成两格(头格放字,紧跟那格
  `data=""` 当占位),而 `_capture()` 把占位格当成了空格 —— 偏偏
  `_resize_screen()` 是拿 `_capture()` 的结果**回灌**进 pyte 的,于是
  **每缩放一次,汉字之间就多一个空格**:1 次 → `中 文 宽 度 测 试`,4 次 →
  `中    文    宽    度    测    试`。绘制循环是同一个根因,所以屏幕上每个
  汉字右边也一直有一格空白。选中复制走的是 pyte 自己的 `display`,本来就没这
  问题,所以只改了两处。回归在 `tests/_e2e.py` 14b,负向对照直接用修复前的
  原始实现。
- **主机指纹一直是空的。** `paramiko 5.0` 里 `paramiko.util.hexlify` 没了,那句
  抛 `AttributeError` 又被 `except: pass` 吞掉,于是日志里长期打「主机指纹=未知」
  而没有任何报错。现在走 `key.fingerprint`(`SHA256:...`),老版本的回退用
  `binascii.hexlify`(`paramiko.util.hexlify` 没了之后那条回退在真机上同样是死
  的,只是被 except 盖住看不出来)。抽成 `server_fingerprint()` 是为了能离线测
  —— 这条过去唯一"证明"它活着的就是那行日志,而那行日志恰恰是它坏掉时唯一
  没变的东西。回归在 `tests/_e2e.py` 14c。

**答案,不是缺陷:`sz -v` 在 GNU lrzsz 上不打 `Sending:`。** 文档里悬了很久的
那条("逐文件信号只在 trzsz 上实测过")现在有答案了:实测这台机器的 `sz -v`
输出里**没有** `Sending:`。后果就是第 1 条描述的那个 —— `index` 推不动,多文件
进度条停在 1/n 直到进程退出才跳完成。**功能不坏,只是百分比难看**,而且这是
远端工具的行为,客户端改不了。真要在别的平台验,看的是那边的 `sz` 认不认
`-v`、打不打 `Sending:`。

**仍然没验到的(本机量不了):** macOS 真机、Linux **客户端**(这次是 Windows 连
WSL,不是 Linux 客户端)、Wayland 下 `Qt.Tool` + `WA_ShowWithoutActivating`、
SMB/非 NTFS 上 `os.replace` 的原子性、以及 `download_dir()` 在 macOS
(CWD=`/`)上的退路。

### ZMODEM 这条路仍未验证的项

1. **多文件逐文件进度在真机上推不动。** 见上面那条:`sz -v` 不打 `Sending:`
   ⇒ `index` 停在 0 ⇒ 进度条停在 1/n。`transfer_controller.py` 的整批百分比是
   **按文件个数等权**的,所以最终会跳到 100%,只是过程难看。**这是远端工具的
   行为,不是客户端 bug。**
2. **`tests/_zmodem.py` 从未在 POSIX 上跑过。** 它现在按平台守卫分组:真二进制
   对灌那几组([1][2][6][7][14])只在 Windows 上跑,后面那些纯逻辑组
   (`plan_local_upload` / `split_remote_paths` / `TransferReport` / 忙时闸门 /
   取消语义)在哪台机器上都跑。**在非 Windows 上跑一次是这个守卫的第一次真正
   验证**,在那之前它只是"写对了"。注意:真机联调验的是"Windows 客户端能不能
   和 POSIX 远端配合",**不等于**这段测试脚本本身在 POSIX 上跑过。
3. **下载方向落盘目录沿用 `os.getcwd()`。** macOS 从 `.app` 双击启动时 CWD 是
   `/`,本地 `rz` 会尝试往 `/` 写而权限拒绝。**这条已经修了**:新增
   `lrzsz.download_dir()`,按 CWD → 配置目录 → 临时目录逐档试,判据是真落一个
   探针文件进去再删掉(`mkdir(exist_ok=True)` 对已存在的只读目录也会成功,所以
   可写与否完全由探针说了算)。回归在 `tests/_zmodem.py` 第 20 组。**修法本身
   在 macOS 上还没验过。**
4. **macOS 上 PTY 的 `QSocketNotifier` 可靠性没验证。** ZMODEM 的字节流靠它
   驱动;若不可靠,表现是进度卡住而不是挂死(`_wait_proc` 仍会回调收尾),
   上传方向的进度来自 proc→channel 也不依赖它。属于**卡进度**而非**功能中断**,
   但没真机量过。
5. **`SSHConnection.exec()` 遇到「后台化」的命令会挂到超时。** 实测
   `conn.exec("cmd & echo started")` 必然超时(后台进程攥着 channel,stdout 永远
   不 EOF,而 `exec` 读的是 stdout);前台命令 0.68 秒就回来了。**目前只有资源
   监控(`monitor_panel.py`)一处调 `exec`**,用的是固定的前台命令,所以正常
   使用碰不到。**没修**:改 `exec` 的语义要连带动监控那条路,收益不抵风险,
   记在这里备查。

### 片段面板新增的未验证项

1. **macOS 上 `Qt.Tool` + `WA_ShowWithoutActivating` 的实际表现没量过。**
   焦点留在主窗这条在 Windows 上是实测的(测试 [12] 量的是
   `QApplication.focusWidget()`);macOS 的窗口激活模型不同,需要 `raise_()`
   配合,而且系统级的 Cmd+Space / 全局快捷键仍然抢得到。这条**本机无法验证**,
   和上面 macOS 那几条一起列入真机首跑清单。
2. **面板定位没做持久化。** 它每次都出现在默认位置,不做"记住上次打开在哪"。
   这是刻意的:要记住就得处理多显示器、分辨率变化、以及窗口关掉时那块屏幕
   已经拔了的情况,收益不值当。工具栏按钮是兜底入口,找不到面板总能再点一次。
3. **远端 line discipline 的极端情况没覆盖。** 面板只往通道写 UTF-8 字节,
   不管远端是不是 `stty` 过、当前是不是在某个全屏程序里(那这时插进去的字符
   会进那个程序的输入)。这是**有意的**:清行/退出全屏程序这类操作需要判断
   远端状态,而客户端没有可靠办法判断,猜错的代价比"插错地方"更大。
4. **`snippets.json` 在网络盘 / 非 NTFS 上的原子写没验证过。** `mkstemp` +
   `os.replace` 在本地 NTFS 上成立;SMB 共享、某些 Linux 文件系统(以及 Windows
   容器挂载层)上 `os.replace` 是否真原子、是否会被杀毒软件打断,本机测不出来。
   失败时 `save()` 返回 False、`_flash` 提示"保存失败",但**内存里的改动已经
   生效** —— 重开应用会看到不一致。这是可接受的降级,不是数据损坏。
5. **`audit_builtins` 挡不住所有危险写法。** 它守的是"出厂内置集"这一个受控
   表，不是用户输入的沙箱。第一版只查每段的 `words[0]`，实测 19 种危险写法里放过去
   12 种（`sudo rm`、`env rm`、`FOO=1 rm`、`busybox rm`、`xargs rm`、
   `mkfs.ext4`、`awk '{system("rm -rf /")}'`、`python -c "os.system(…)"`、
   `cleanup() { rm -rf …; }`）。第二版换成**剥前缀链 + 认点号变体 + 抽嵌套载荷**，
   `tests/_snippets.py` 第 29 组量的是它：19 条必抓写法、12 条良性负向对照，
   外加"把 `_resolve_head` 装回只看 `words[0]` 的缺陷版，上面那批断言必须变红"
   这条元断言——不装回缺陷版，这组测试和恒真的版本长得一模一样。
   **但仍然可以被 `${IFS}` 展开、别名、`base64 -d | sh`、或二进制直写绕过。**
   这是有意的取舍：真正的沙箱要把整条命令送进 `bash -n` 或 AST 解析器，代价远超
   "别把 `rm` 塞进默认列表"这个需求。
   **另一个独立的空白：这些正则只在 Windows 上验过"不误触发"，从没在真实 POSIX
   shell 上验过"能抓到"。** 评审实测过 9 条 glob/正则（`ls *.py`、
   `grep '[0-9]'`、`find -name '*.py[0-9]'` 等）零误报——那证明的是
   `_LIST_PAYLOAD` 要求 `(` 紧邻 `[`、而 glob 里没有括号。但"在真实 POSIX 片段里
   能抓到 `subprocess.run(["rm", ...])`"这件事**一次都没测过**，在真机首跑之前
   它只是"正则看着对"。
   另有一处**有意的误报**：`systemctl status nginx` 会被报成违规，因为 `systemctl`
   整词在黑名单里而没给只读子命令开口子——内置集里没有 systemctl，不值得为它松。
6. **Wayland 下 `Qt.Tool` + `WA_ShowWithoutActivating` 一点没覆盖。** 本 app 在
   Linux 上最可能落到的就是 Wayland 会话，而面板"不抢焦点"这条承诺
   （`tests/_snippets.py` 第 12 组量的是 `QApplication.focusWidget()`）整个建立在
   X11 的焦点模型上。Wayland 下 Qt 走 XDG-Activation，`WA_ShowWithoutActivating`
   的语义**不是** X11 那个：常见结果是 `show()` 之后焦点照样被抢走，或者反过来
   面板弹出来却排在所有窗口后面。**除本条外全仓没有第二处提到 Wayland**，
   也就是说这一条连"我们知道它没测"都是刚补上的。**本机无法验证**，和上面
   macOS 那几条一起列入真机首跑清单：第一次在 Wayland 上点工具栏按钮，先确认
   键盘焦点**还在终端上**。
