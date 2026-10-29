# tcodex-notification

服务器运行 TCodex，Mac 通过 SSH 接收回合结束事件并显示原生通知。
另有独立启动的服务器 watcher，在最终 HTTP 429 重试耗尽后向同一会话发送 `continue`。
两项功能独立运行，无需额外通知应用，不修改 TCodex 的安装或默认配置。

## 架构

```text
TCodex → notify hook → 服务器事件队列 → SSH → Mac osascript → 系统通知
会话 JSONL → 服务器 watcher → 退避等待 → 同一活跃会话收到 continue
```

`tcodex-notify.sh` 不启动 watcher；watcher 不发送通知，也不依赖 Mac 在线。
自动继续后的回合若正常完成，原会话配置的 notify hook 仍按原流程处理。

```text
server/
  tcodex-notify.sh       TCodex 启动入口
  notify-event.py        完成事件与测试入口
  notify_queue.py        队列、去重和保留限制
  stream-events.py       SSH 事件流
  watch-retries.py       独立启动的 429 自动继续 watcher
mac/
  listen.sh             Mac 本地监听与通知
tests/
  test_notifications.py
  test_watch.py
.env.example            可选配置示例
runtime/                自动生成，Git 忽略
  .auto-continue/        watcher 检查点与锁，独立于通知 channel
```

## 安装

服务器需要已可用的 `tcodex`、Bash 和 Python 3.9+；Mac 使用系统自带的 Bash、SSH 和 `osascript`。
将仓库放在服务器自己的目录。以下 `server-alias` 和 `/absolute/path/to/tcodex-notification` 分别替换为 SSH 别名和服务器仓库绝对路径。

在 **Mac 本地终端**下载监听脚本：

```bash
mkdir -p ~/Developer/tcodex-notification/mac
scp server-alias:/absolute/path/to/tcodex-notification/mac/listen.sh \
  ~/Developer/tcodex-notification/mac/listen.sh
```

服务器不支持 SFTP 时，可给 `scp` 增加 `-O`。更新脚本时重复下载即可。

测试本地通知，等待 5 秒：

```bash
bash ~/Developer/tcodex-notification/mac/listen.sh --test-notification
```

确认 Mac 允许对应来源的通知，专注模式没有屏蔽提醒。

## 完成通知

**1. Mac 本地启动监听**，等待 `Notification stream connected`，保持窗口开启：

```bash
bash ~/Developer/tcodex-notification/mac/listen.sh \
  --server-dir /absolute/path/to/tcodex-notification server-alias
```

**2. 首次使用先在服务器测试完整链路**：

```bash
cd /absolute/path/to/tcodex-notification
python3 -B server/notify-event.py --test
```

Mac 应出现通知，监听窗口显示 `Received event ...`。

**3. 在服务器的工作终端启动 TCodex**：

```bash
bash /absolute/path/to/tcodex-notification/server/tcodex-notify.sh
```

在需要处理的项目目录中执行上述命令，然后正常提交任务。附加参数直接追加，例如 `resume`。
直接运行 `tcodex` 或已运行的会话不会自动接入通知。

默认 channel 为 `default`。自定义时，服务器设置 `TCODEX_NOTIFY_CHANNEL`，Mac 使用同名 `--channel`。
Mac 也可用 `TCODEX_NOTIFY_SERVER_DIR` 环境变量代替 `--server-dir`，配置示例见 `.env.example`。
没有 SSH 别名时使用 `user@host`，自定义端口用监听参数 `--port`。

在 Mac 按 `Ctrl+C` 仅停止监听，不停止服务器任务。Mac 需在线且未休眠；通知表示回合结束，不代表任务成功。
首次启动跳过旧事件，运行时断线自动重连。不要在运行期间删除 `runtime/` 的锁和游标。

## 429 自动继续

### 要求

- Linux、Python 3.9+，且 `/proc/locks` 可读。
- 已验证的 Codex 0.154.0 runtime，支持 `queue`、会话 JSONL、
  `thread_history_1.sqlite` 和 `queue_1.sqlite`。
- 与目标交互式 TCodex 进程使用相同用户。

watcher 使用原会话进程的 Codex 可执行文件和选定的 `CODEX_HOME`。
它不启动 Tencent gateway，不直接发起模型请求，不注入终端按键，也不另开 `resume` 进程。
`continue` 在原对话内开始一个新回合，使用原会话的模型、权限、工具和上下文。

### 启动

在服务器另一个终端启动一次 watcher，并保持目标 TCodex 会话开启。
监督所选 `CODEX_HOME` 的所有当前及未来主要 CLI 会话：

```bash
python3 -B /absolute/path/to/tcodex-notification/server/watch-retries.py \
  --all-cli
```

仅监督指定会话：

```bash
python3 -B /absolute/path/to/tcodex-notification/server/watch-retries.py \
  --thread SESSION_UUID
```

可重复 `--thread` 指定多个会话；必须显式选择 `--thread` 或 `--all-cli`。
会话 UUID 是 rollout 文件第一行 `session_meta.payload.id`，文件名也以该 UUID 结尾。
其他部署位置请替换命令中的项目路径。

`--codex-home /absolute/path` 可选择其他安装，默认是 `$CODEX_HOME`，未设置时使用 `~/.tcodex`。
`--all-cli` 不按当前工作目录或通知 channel 筛选；已在 watcher 启动前打开的会话也会纳入，
无需通过 `tcodex-notify.sh` 启动。关闭的历史会话不会被自动打开。

启动日志区分注册文件和活跃交互式会话，例如：

```text
Registered session files: 12; live interactive sessions: 2; inactive session files: 10.
```

此示例中只有 2 个会话当前有活跃交互式进程，仅这些会话逐条列出。
历史文件保持注册，以便检测重新打开的会话；注册不代表会话仍在运行或可以自动发送。
清单按发现间隔刷新，仅在变化时输出。
活跃包括空闲等待输入、错误后等待输入，以及仍在运行的 detached tmux 中的会话。

首次启动跳过已有文件中的历史；运行期间新建的会话文件从头读取，包括发现前快速发生的失败。
请在下一次失败前启动 watcher。重启后从 `runtime/.auto-continue/` 恢复游标、冷却时间和重试次数，
不会重放已处理的失败。按 `Ctrl+C` 只停止 watcher，不停止 TCodex，也不撤回已入队的消息。

需要脱离终端运行时，可单独使用 tmux：

```bash
tmux new-session -s tcodex-auto-continue \
  'python3 -B /absolute/path/to/tcodex-notification/server/watch-retries.py --all-cli'
```

### 默认值与控制

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `--initial-delay` | 60 秒 | 第一次自动继续前的等待时间 |
| `--max-delay` | 300 秒 | 退避上限 |
| `--max-retries` | 5 | 连续自动继续次数上限 |
| `--poll-interval` | 1 秒 | 增量读取文件间隔 |
| `--discover-interval` | 10 秒 | 新会话发现与活跃清单刷新间隔 |
| `--command-timeout` | 15 秒 | 入队命令超时 |
| `--observe-timeout` | 30 秒 | 入队后等待观察新回合的时间 |
| `--runtime-dir` | 项目 `runtime/.auto-continue/` | 私有检查点与会话锁 |
| `--tmp-dir` | workspace `tmp/` | 子进程临时目录，禁止位于 `/tmp` |
| `--dry-run` | 关闭 | 观察新失败，不提交消息 |
| `--once` | 关闭 | 处理一批数据后退出 |

默认等待时间为 60、120、240、300、300 秒。
成功、其他类型的错误、手动中断或手动开始新任务会结束当前重试序列。
达到上限后停止自动继续，直到开始新的手动回合。
自动 `continue` 可能使 agent 在原会话权限下继续执行命令。

dry-run 检查点位于选定状态目录的 `dry-run/` 子目录，
默认是 `runtime/.auto-continue/dry-run/`，不消耗正式 watcher 的重试次数。
状态目录必须由当前用户拥有且权限为 0700。

### 安全检查与限制

仅匹配以下最终结构化事件：

```text
event_msg
  payload.type = task_complete
  payload.error.codex_error_info.response_too_many_failed_attempts.http_status_code = 429
```

中间 HTTP 429、普通命令失败、认证错误、手动中断、subagent 和 headless 会话不触发发送。
发送前确认原交互式进程仍持有会话 writer lock、数据库最新回合仍是该失败回合、
没有排队输入，且 rollout 已读完。等待期间手动继续会取消待发送消息。
同一状态目录下两个 watcher 不能同时拥有同一会话；请持续使用同一个状态目录，
更换目录会绕过共享锁与检查点去重。

文件监督方案没有原子式“检查并提交”接口，最终检查后的短暂窗口仍可能与手动输入或进程退出竞争。
不要在频繁手动改向的会话上无人值守使用。原 CLI 在提交期间退出时，
已经接受的消息可能留在队列，直到会话以后重新打开。

提交前持久化 dispatch 检查点。进程崩溃、命令超时或结果不确定时不会再次发送；
超过观察时间仍未看到新回合，也会停止该重试序列。这优先避免重复消息，不保证送达。
文件被截断或替换、JSON 无效或过大、数据库不可用、无法确认进程归属时不发送。
解决原因后通过新的手动回合重新启用；损坏或替换的 rollout 检查点需要人工检查，不自动重放。

会话文件、数据库和 writer lock 属于版本相关的内部格式，升级 TCodex 后需重新验证。
安装、通知启动入口及 Mac 监听器均不会自动启动 watcher。

### 从独立项目迁移

原 `infra/tcodex-auto-continue` 已合并至此，不再提供旧启动入口。
现有部署迁移时，先停止旧 watcher 并确认检查点锁未被持有。
将旧 `runtime/` 整体移动到本项目 `runtime/.auto-continue/`，保留权限、文件和检查点内容；
若目标已存在则停止，不覆盖或自动合并。
迁移不应修改通知 channel 数据，也无需停止通知事件流。
原来显式指定了 `--runtime-dir` 的部署可继续使用同一自定义目录。

## 测试

两个测试套件及其子进程均将临时文件限制在 workspace `tmp`，不使用真实会话或 Mac 通知：

```bash
python3 -B /absolute/path/to/tcodex-notification/tests/test_watch.py
python3 -B /absolute/path/to/tcodex-notification/tests/test_notifications.py
```

## 分享注意事项

个人路径、SSH 别名及本机启动说明应只保存在 Git 忽略的 `local/` 或本机配置中。
`runtime/` 包含会话标识、实际路径和运行状态，也已被忽略。
`.gitignore` 只影响 Git，不会自动过滤目录打包；分享源码压缩包时排除 `local/`、`runtime/`、
`.env`、本机配置及 `.git/`。Git 仓库的提交历史仍包含作者身份，不会因源码路径清理而匿名化。
