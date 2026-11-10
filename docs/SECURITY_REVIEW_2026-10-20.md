# 安全评审汇总：PyQt6 SSH 客户端的两处缺陷

> **日期**：2026-10-20
> **方式**：三位专家独立评审（互不可见结论，避免趋同），再逐条核实。
> **结论**：两位评审对同一批事实独立得出了**一致的修复方案**；分歧点在
> 实现的取舍上，已在 §5 记录。

---

## 0. 一句话

**两个真缺陷：主机密钥无条件接受（MITM，P0）+ lrzsz 远端命令注入（P0）。**
三位评审对这两条的**修复方向完全一致**，且我逐条核实过的证据支持它们。

---

## 1. 三位评审的分工

| 评审 | 视角 | 负责 |
|---|---|---|
| **A** | 密码学 / 安全实现 | 主机密钥策略、算法策略、known_hosts 持久化、paramiko CVE |
| **B** | 输入处理 / 防御式编程 | lrzsz 注入修法、`exec()` 调用方审计、隧道绑定、ProxyCommand |
| **C** | 产品体验 / 对标设计 | 首次连接 UX、OpenSSH/PuTTY/WinSCP 对标、错误文案、过渡方案 |

---

## 2. 已核实的关键事实

这一节是**我亲自验证过的**，不是评审转述：

| # | 事实 | 怎么验的 |
|---|---|---|
| 1 | **`paramiko 5.0.0` 的默认策略是 `RejectPolicy`** | 读 `SSHClient.__init__` 源码：`self._policy = RejectPolicy()` |
| 2 | **MITM 是我们自己降级出来的**，不是 paramiko 的行为 | 同上；`connection.py:465` 显式设成 `AutoAddPolicy` |
| 3 | **用户 `~/.ssh/known_hosts` 从未被读入** | `connection.py` 里既无 `load_system_host_keys()` 也无 `load_host_keys()` |
| 4 | **`AutoAddPolicy` 从未落盘过任何东西** | 它只写 `client._host_keys`（进程内），而 `_host_keys_filename` 恒为 `None` → 现存用户**没有任何指纹基线** |
| 5 | **`BadHostKeyException` 是 `SSHException` 的子类** | 打印 MRO：`BadHostKeyException → SSHException → Exception`。所以它**现在**被 `connection.py:492` 的 `except (SSHException, OSError)` 吞成泛化的「连接失败」 |
| 6 | **`connect()` 跑在工作线程** | `main_window.py:2716-2730`：`threading.Thread(target=worker)` |
| 7 | **指纹算了但界面从不展示** | `server_fingerprint()` 的结果只进 `log.info`（`connection.py:515-517`）；`ConnectionInfo.fingerprint` 的消费者只有测试 |
| 8 | **MD5 指纹是无分隔符 hex** | `connection.py:126`：`binascii.hexlify(...)` → 与 PuTTY/WinSCP 的 `aa:bb:cc` 格式对不上 |
| 9 | **注入点有**两**处** | `lrzsz.py:314`（`conn.exec`）+ `main_window.py:1224`（`sendall` 打进交互式登录 shell） |
| 10 | **`tests/_download.py:63` 正在钉住有缺陷的旧规则** | 断言字面写着「`~` 路径**不加引号**」 |
| 11 | **paramiko 5.0 默认 host_key 已无 `ssh-rsa`/`ssh-dss`** | 打印 `_preferred_keys` → 只有 ed25519/ecdsa/rsa-sha2-* |
| 12 | **本地 paramiko 是 5.0.0** | `pip show` / `import paramiko` |

---

## 3. 三方共识（核心方案）

### 3.1 主机密钥：四件套

三位评审独立得出同一个方案：

1. **加载信任库** —— `~/.ssh/known_hosts`（只读）+ `%APPDATA%\MavisSSH\known_hosts`（读写）
2. **首次连接问用户** —— 展示 SHA-256 指纹
3. **单独接住 `BadHostKeyException`** —— 不能混进普通连接失败
4. **不提供回调时按拒绝处理** —— 静默接受是最糟的退路

**评审 A 给的具体实现**（已被采纳）：

- 新建 `sshclient/remote/hostkeys.py`，**不依赖 Qt**，只认
  `Callable[[HostKeyRequest], bool]`
- 策略类 `PromptHostKeyPolicy(paramiko.MissingHostKeyPolicy)`
- 接受后 `client._host_keys.add()` + **`client.save_host_keys()`**
  （不是 `HostKeys.save()` —— 前者写盘前会重读文件以合并别的 SSH 客户端
  的并发写入）

**评审 C 给的 UX**（已被采纳）：

- 三按钮：**信任并记住 / 仅此一次 / 取消**（不堆第四个）
- SHA-256 为主 + MD5 兼容行（**冒号分隔**，现在是无分隔 hex）
- 默认焦点落在**拒绝**上（安全默认，防「一路回车」）
- 「密钥变更」是**另一套文案**，不给「仅此一次」

**评审 C 给的过渡方案**（已被采纳）：

- **不做**「首次启动批量信任所有已保存会话」—— 那等于把同一个 MITM 风险
  一次性打包，性质与原漏洞相同
- `~/.ssh/known_hosts` 里有条目的机器**直接认**（老用户用 OpenSSH 连过的
  完全不受影响）—— 这是过渡体验的关键

### 3.2 lrzsz 注入：不要按值分支

**评审 B 的修法（已被采纳）**：

```python
def remote_word(path):
    if path == "~":
        return '"${HOME}"'
    if path.startswith("~/"):
        return '"${HOME}"/' + shlex.quote(path[2:])
    return shlex.quote(path)
```

**评审 B 明确否掉了 `lrzsz.py` 原来 docstring 里自己提的 `eval echo "$p"`**：
双引号里 `~` 不展开（达不到目的），而 `eval` 会二次解析 —— `$(...)`、
反引号在里面照常执行，等于把注入原样装回去。

> 这条否掉的是**我在 2026-10-13 加的那段注释里的建议**。当时我写的
> 「正确修法」举例用了 `eval echo "$p"` —— 评审 B 判定「两头都错」。
> 已按 B 的方案改。

### 3.3 跨线程确认

**评审 A 与 C 独立得出了同一结论**：

- `connect()` 在工作线程 → `missing_host_key()` 也在工作线程
- **不能在 worker 线程弹 Qt 控件**（会拉起脱离主线程的嵌套事件循环）
- 两阶段：worker `Event.wait()` + `QueuedConnection` 投到主线程
- **`QueuedConnection` 不能用 `BlockingQueuedConnection`**（死锁来源）
- **`Event.set()` 必须在 `finally` 里**（UI 线程上任何异常都会让 worker 永久挂住）
- **必须有超时**（否则连接卡死 + 线程泄漏）

---

## 4. 分歧与取舍

| 点 | 分歧 | 采纳 |
|---|---|---|
| **信任库写哪** | A：`%APPDATA%\MavisSSH\known_hosts`；C：同上但强调**读 `~/.ssh` 不写它** | 合并：**双轨读、单轨写** |
| **首次确认的按钮数** | A：倾向自定义 prompt 类；C：三按钮（信任/仅一次/取消） | 三按钮，但**先做「信任并记住 / 取消」两态**（评审 C 的「仅此一次」需额外实现「不写盘」的策略类） |
| **lrzsz 的远端展开方式** | B 给 2 个候选（`${HOME}` 插值 / 循环体 `case` 展开） | 选 `${HOME}` 插值：产出**一个 word**，判据可以直接对着它写 |
| **错误文案** | A 关注算法；B 关注调用方；C 关注文案分类 | C 的四类分类（认证失败／网络不可达／**主机密钥问题**／服务器拒绝） |
| **批量信任** | C 明确反对 | 不做 |

---

## 5. 三位评审各自挖出的**独立**贡献

这部分是「为什么要三个专家」的价值所在——**每条都只有一个人发现**：

| 发现者 | 发现 |
|---|---|
| **A** | paramiko 5.0 默认就是 `RejectPolicy` → MITM 是**自伤**不是「库的锅」 |
| **A** | `AutoAddPolicy` 从未落盘 → 现存用户**没有指纹基线**，升级后必然全部走「未知」分支 |
| **A** | paramiko #2126 教训：禁算法**只能砍更弱的替代项**，砍掉唯一强项会让 Win/macOS 握手直接失败 |
| **A** | `save_host_keys()` vs `HostKeys.save()`：前者会重读文件合并并发写入，后者会丢别人的条目 |
| **A** | `tests/_fconn.py:751,863` 用 `inspect.getsource(SSHConnection.connect)` 做源码断言 → 改 `connect()` 会红 |
| **B** | **注入点有两处**（`conn.exec` + 打进交互式 shell），后者更直接 |
| **B** | `lrzsz.py` 原 docstring 里的 `eval echo "$p"` 是错的（两头都错） |
| **B** | `proxy_command` 内部是 `Popen(cmd, shell=True)` → 一旦接上就是**本机任意命令执行**；今天恒 None 是死代码 |
| **B** | 隧道 `0.0.0.0` 能从**导入的会话文件**静默带入（提示只在「加规则时的 tooltip」） |
| **C** | `BadHostKeyException` 被子类关系吞成「连接失败」 → 告警失效 |
| **C** | 指纹算了但界面从不展示（只进日志）→ 「不存在」而非「没做好」 |
| **C** | MD5 指纹格式与 PuTTY/WinSCP 对不上 → 用户拿手上资料无法目视比对 |
| **C** | 认证失败后**静默复用旧口令重试** → 用户陷入「点重连→再失败」循环 |
| **C** | 无 idle 断连、无自动重连（**好消息**）—— 不存在「后台自动重发口令」的高危便利 |

---

## 6. 评审判据的自我修正记录

这次立判据时，**判据自己出错了 5 次**，每一次都是同一个模式的变体：

1. **`processEvents()` 不投递 DeferredDelete** → 销毁场景全测了个常量
2. **`isValid` 与 `isdeleted` 极性相反** → 判据整个颠倒
3. **自定义 `excepthook` 吞 stderr** → 零输出 + 退出码 1，看着像「Qt 崩了」
4. **元断言写成「每条输入都红」** → 3 条误报（其中一条是老实现**本来就安全**的输入）
5. **元断言的替换串与源码实际写法不一致** → 替换没生效，那条判据是**恒绿的假货**

第 4、5 条尤其值得记：**元断言自己也要有区分度**。第一版的元断言
「老实现必须每条都红」在语义上是错的 —— 老实现对 `/tmp/a;touch pwned.md`
**本来就安全**（`shlex.quote` 正确转义），它没有缺陷可露。正确的形态是
「老实现至少在**一批**输入里露馅」，且要**指名道姓**说是哪几条。

第 5 条更隐蔽：替换没生效时，那条判据**照样绿**（因为它断言的东西恰好
满足），完全看不出来是假货。判据里必须有一条**专门检查替换是否生效**的
元断言。
