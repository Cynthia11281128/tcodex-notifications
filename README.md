# tcodex-notification

## TClaude 分支：统一 Mac 监听

本分支保留 TCodex 功能，并增加 TClaude 通知及独立的原生 hook 自动继续。
`main` 和本分支的 `mac/listen.sh` 内容一致，从任一位置下载新版均可。
两端需要使用相同 channel；目录参数显式指定事件来源，不会自动搜索其他目录。

Mac 更新脚本后，只运行一个命令：

```bash
bash ~/Developer/tcodex-notification/mac/listen.sh \
  --tcodex-dir /absolute/path/to/tcodex-notification \
  --tclaude-dir /absolute/path/to/tclaude-notification \
  --channel default server-alias
```

该命令管理两条独立 SSH 流；游标、重连和去重分别处理，一边断线不影响另一边。
通知标题分别为 TCodex 和 TClaude。只传一个目录参数即可单独监听。
旧 `--server-dir` 保留为 TCodex 单来源用法，不能与新目录参数混用。
新目录参数也可通过 `.env.example` 中的环境变量设置，命令行覆盖对应环境变量。
双来源入口显示 `Unified listener v7`；兼容子监听器保留 v6 标识。

### 服务器启动 TClaude

```bash
TCLAUDE_NOTIFY_CHANNEL=default \
  bash /absolute/path/to/tclaude-notification/server/tclaude-notify.sh
```

在目标项目工作目录执行。所有附加参数传给 TClaude，例如 `--resume SESSION_UUID`。
启动入口只注入当前进程的 `--settings`，不修改共享配置、不改变模型或权限；
用户显式指定关闭 hooks 的模式会使通知和自动继续不可用。本入口拒绝附加
`--settings`，额外配置使用正常 user/project settings。首次使用检查 `/hooks` 和工作目录信任。
已经运行的 TClaude 不会自动接入；TCodex 原启动入口和队列保持不变。

`Stop` 和 `StopFailure` 表示回合结束，不表示成功；`AskUserQuestion` 的
`PreToolUse` 表示准备提问，不保证仍在等待回答。不处理审批提醒或子代理事件。
队列只保存编号、时间和 session 短哈希，不保存问题、选项、回答或错误内容。

完整链路的合成测试（需先连接 Mac）：

```bash
python3 -B server/tclaude-events.py --channel default --test complete
python3 -B server/tclaude-events.py --channel default --test input
```

### 独立 TClaude watcher

```bash
python3 -B server/watch-tclaude-retries.py --all-registered
python3 -B server/watch-tclaude-retries.py --session SESSION_UUID
python3 -B server/watch-tclaude-retries.py --all-registered --dry-run
```

先启动 watcher，再让通过本入口接入的 TClaude 执行任务。可以监督接入后已有及
未来活跃主会话；未加载 hook 的旧会话、headless、subagent 不在范围内。
watcher 不依赖 Mac，通知入口不自动启动 watcher。停止 watcher 不停止 TClaude。
`--once` 仅检查注册会话后退出，不等待未来失败。

默认固定等待 3 秒，连续最多 5 次，观察超时 30 秒；`--initial-delay`、
`--max-delay`、`--max-retries`、`--observe-timeout` 可配置。较大的 max-delay
启用指数退避。延迟及观察时间最大 300 秒，次数最大 100。
自定义 `--runtime-dir` 必须与会话环境 `TCLAUDE_NOTIFICATION_RUNTIME` 一致；
自定义 `--tmp-dir` 必须与会话 TMPDIR 一致，禁止 `/tmp`。
状态默认位于 `runtime/.tclaude-auto-continue`，socket 位于 workspace `tmp`，
dry-run 检查点单独保存，不消耗正式次数。

仅当最终 `StopFailure(error=rate_limit)` 对应 transcript 中最新 assistant
记录具有 `isApiErrorMessage=true`、整数 `apiErrorStatus=429` 时考虑重试。
watcher 检查同用户活跃 TUI 进程、失败记录和当前 prompt，持久化批准后通知
该失败回调通过 `asyncRewake` 唤醒原会话。送入的是固定 hook 反馈，不是新建
用户消息 `continue`；原上下文、模型和权限保留。手动提交、成功、其他错误或
退出取消待处理失败。批准不确定或观察超时停止序列，不重放；新的手动输入重新启用。
检查与 native wake 不是原子操作，最后的检查仍可能与手动输入竞争；不要在频繁改向的
会话中无人值守使用。已批准并进入 runtime 的唤醒无法通过停止 watcher 撤回。
runtime 内部格式和 asyncRewake 属于版本相关接口，升级后需重新验证。
本机已通过 Claude Code `2.1.295` 的隔离 PTY、本地模拟 429 和连续唤醒测试。
自动继续只接受可执行文件位于 `claude-code-2.1.295` 目录的 runtime；
其他版本拒绝自动继续，但通知不受此限制。测试不使用真实凭据或模型服务。

新增测试：

```bash
python3 -B tests/test_tclaude_notifications.py
python3 -B tests/test_tclaude_watch.py
# Optional: explicitly select the verified native binary, never the gateway wrapper.
TCLAUDE_TEST_RUNTIME=/absolute/path/to/claude-code-2.1.295/claude \
  python3 -B tests/test_tclaude_runtime.py
```

服务器运行 TCodex，Mac 通过 SSH 接收回合结束、准备提问事件并显示原生通知。
另有独立启动的服务器 watcher，在最终 HTTP 429 重试耗尽后向同一会话发送 `continue`。
两项功能独立运行，无需额外通知应用，不修改 TCodex 的安装或默认配置。

## 架构

```text
TCodex → notify 完成回调                    \
                                             → 服务器事件队列 → SSH → Mac osascript
TCodex → PreToolUse(request_user_input) hook /
会话 JSONL → 服务器 watcher → 退避等待 → 同一活跃会话收到 continue
```

`tcodex-notify.sh` 不启动 watcher；watcher 不发送通知，也不依赖 Mac 在线。
自动继续后的回合若正常完成，原会话配置的 notify hook 仍按原流程处理。

```text
server/
  tcodex-notify.sh       TCodex 启动入口
  notify-event.py        完成事件与测试入口
  notify-input.py        提问 hook 与测试入口
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

## 完成与提问通知

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

**4. 首次启用提问 hook 时，在 TCodex 内打开 `/hooks`**，检查并信任
匹配 `^request_user_input$`、执行本仓库 `server/notify-input.py --channel ...` 的 command hook。
启动脚本只注入当前会话配置，不修改共享配置，也不绕过 hook 信任检查。
更换脚本路径、Python 路径、channel 或 hook 定义后，可能需要重新审核。
未信任、被禁用或被管理策略禁止时，提问提醒不会执行，原有完成通知仍独立运行。

默认 channel 为 `default`。自定义时，服务器设置 `TCODEX_NOTIFY_CHANNEL`，Mac 使用同名 `--channel`。
Mac 也可用 `TCODEX_NOTIFY_SERVER_DIR` 环境变量代替 `--server-dir`，配置示例见 `.env.example`。
没有 SSH 别名时使用 `user@host`，自定义端口用监听参数 `--port`。

在 Mac 按 `Ctrl+C` 仅停止监听，不停止服务器任务。Mac 需在线且未休眠；完成通知表示回合结束，不代表任务成功。
首次启动跳过旧事件，运行时断线自动重连。不要在运行期间删除 `runtime/` 的锁和游标。

### 提问提醒与升级

已在 Codex 0.154.0 runtime 验证 `PreToolUse` 匹配 `request_user_input`。
此 hook 在工具执行前发出一次提醒，因此通知文案为 `TCodex is preparing a question`。
它表示“准备提问”，不保证通知到达时仍在等待回答：工具不可用、参数无效、
其他 hook 拦截、手动中断或已经回答，都可能使提醒与当前界面不一致。
仅覆盖结构化 `request_user_input`，不覆盖普通聊天中的问句、命令权限审批或其他输入形式。
启动脚本不改变 Plan/Default 模式，不额外开启 Default 模式提问功能。

同一调用按 session、turn、tool-call 标识去重；同一回合的不同调用分别提醒。
一个调用包含多道问题时仍只提醒一次。问题、选项、答案与工作目录不会写入通知队列，
Mac 只收到事件编号、时间、会话标识的短哈希，以及由协议行类型区分的事件类别。
hook 不返回许可决定，不修改工具参数，不代答；正常回调失败不会阻止 TCodex。
hook 配置超时为 3 秒，极端队列竞争时可能丢失提醒。

升级时重新下载 `mac/listen.sh`，停止旧监听器并重启，确认日志显示 `TCodex listener v6`。
新监听器兼容旧完成事件；旧监听器会忽略新的 `INPUT` 事件，不会将提问误报为完成。
服务器需要通过更新后的启动脚本新建或 `resume` 会话，已运行会话不会自动添加 hook。
SSH 重连仍会补发未读事件，可能补发已经回答的提问提醒；重启监听器则跳过历史。

Mac 监听连接成功后，可在服务器测试提问提醒的完整通知链路：

```bash
python3 -B server/notify-input.py --channel default --test
```

这里的 `--test` 直接写入合成事件，不验证 TCodex 是否加载或信任 hook。
使用自定义 channel 时替换 `--channel default`，并保持与 Mac 一致。

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
| `--initial-delay` | 3 秒 | 第一次自动继续前的等待时间 |
| `--max-delay` | 3 秒 | 等待时间上限，默认固定等待 |
| `--max-retries` | 5 | 连续自动继续次数上限 |
| `--poll-interval` | 1 秒 | 增量读取文件间隔 |
| `--discover-interval` | 10 秒 | 新会话发现与活跃清单刷新间隔 |
| `--command-timeout` | 15 秒 | 入队命令超时 |
| `--observe-timeout` | 30 秒 | 入队后等待观察新回合的时间 |
| `--runtime-dir` | 项目 `runtime/.auto-continue/` | 私有检查点与会话锁 |
| `--tmp-dir` | workspace `tmp/` | 子进程临时目录，禁止位于 `/tmp` |
| `--dry-run` | 关闭 | 观察新失败，不提交消息 |
| `--once` | 关闭 | 处理一批数据后退出 |

默认每次固定等待 3 秒，不递增。显式将 `--max-delay` 设为大于
`--initial-delay` 时，才会启用指数退避。
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
