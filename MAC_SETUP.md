# Mac 通知配置

Mac 使用本地监听脚本，通过 SSH 接收服务器上的 TCodex 和 TClaude 事件，
再通过系统自带的 `osascript` 显示通知。无需安装额外的通知应用。

本文只使用通用示例，不包含个人路径、真实主机地址、用户名或个人 SSH 别名。
示例中的 `/path/to/infra` 是服务器目录占位符，必须替换为实际绝对路径；
`server-alias` 必须替换为 Mac 已配置的 SSH 别名或 `user@host`。
不要将替换后的个人配置提交到仓库。

## 1. 准备

- Mac 能通过 SSH 登录目标服务器，并可运行 `bash`、`ssh` 和 `osascript`。
- 服务器已安装需要使用的 `tcodex` 或 `tclaude`，并提供 Python 3.9+。
- 服务器上的目录分别为 `tcodex-notifications` 和 `tclaude-notifications`。
- Mac 和服务器必须使用相同 channel。本文统一使用 `default`。

仅收通知不需要启动自动继续 watcher，两项功能独立。

## 2. Mac 下载监听脚本

以下命令在 **Mac 本地终端**执行，不要先 SSH 登录服务器：

```bash
SSH_DEST=server-alias
REMOTE_INFRA=/path/to/infra
MAC_DIR="$HOME/Developer/agent-notifications"

mkdir -p "$MAC_DIR/mac"
scp -O "$SSH_DEST:$REMOTE_INFRA/tcodex-notifications/mac/listen.sh" \
  "$MAC_DIR/mac/listen.sh"
```

两个项目中的新版 `mac/listen.sh` 支持统一监听，只需下载其中一份。
`scp -O` 使用传统 SCP 协议；服务器支持 SFTP 时也可以省略 `-O`。
自定义 SSH 端口时，给 `scp` 增加 `-P PORT`。

首次使用或更新脚本后，测试 Mac 本地通知：

```bash
bash "$MAC_DIR/mac/listen.sh" --test-notification
```

等待 5 秒。若没有弹窗：

- 在 Mac 系统设置的通知页面检查对应通知来源，允许通知并启用合适的提醒样式。
- 检查专注模式是否屏蔽通知。
- 确认命令在 Mac 本地执行，且没有 `osascript` 错误。

脚本提示通知请求已发送，不代表 macOS 一定展示了提醒。

## 3. Mac 启动监听

在同一个 Mac 本地终端中，同时监听两个来源：

```bash
bash "$MAC_DIR/mac/listen.sh" \
  --tcodex-dir "$REMOTE_INFRA/tcodex-notifications" \
  --tclaude-dir "$REMOTE_INFRA/tclaude-notifications" \
  --channel default \
  "$SSH_DEST"
```

应看到 `Unified listener v7`，以及 TCodex、TClaude 各自的
`Notification stream connected`。保持此终端运行，可以切换到其他窗口。
双来源监听会建立两条独立 SSH 流，一边断线不影响另一边。

只需要一个来源时，保留对应的 `--tcodex-dir` 或 `--tclaude-dir` 即可。
自定义 SSH 端口时，给监听命令增加 `--port PORT`。
不要将旧的 `--server-dir` 与这两个目录参数混用。

上述变量仅在当前终端有效。新开终端时需重新设置，或将配置保存在 Mac 本地。
若已有旧监听进程，先在旧窗口按 `Ctrl+C`，再启动新版。

## 4. 服务器测试完整链路

先确认 Mac 已连接，再在 **服务器终端**设置服务器目录并发送测试事件：

```bash
REMOTE_INFRA=/path/to/infra

python3 -B "$REMOTE_INFRA/tcodex-notifications/server/notify-event.py" \
  --channel default --test
python3 -B "$REMOTE_INFRA/tcodex-notifications/server/notify-input.py" \
  --channel default --test

python3 -B "$REMOTE_INFRA/tclaude-notifications/server/tclaude-events.py" \
  --channel default --test complete
python3 -B "$REMOTE_INFRA/tclaude-notifications/server/tclaude-events.py" \
  --channel default --test input
```

只使用一个来源时，只运行对应的两条测试命令。
测试不调用模型，只验证事件队列、SSH 和 Mac 通知链路；
它不验证真实会话是否加载或信任 hook。
Mac 监听窗口应显示 `Received event ...`，并出现相应通知。

## 5. 服务器启动实际任务

在 **服务器终端**先进入要工作的项目目录，再按需执行对应入口。
入口不切换工作目录。

TCodex：

```bash
TCODEX_NOTIFY_CHANNEL=default \
  bash "$REMOTE_INFRA/tcodex-notifications/server/tcodex-notify.sh"
```

TClaude：

```bash
TCLAUDE_NOTIFY_CHANNEL=default \
  bash "$REMOTE_INFRA/tclaude-notifications/server/tclaude-notify.sh"
```

附加参数直接放在命令末尾，例如 TCodex 的 `resume` 或 TClaude 的
`--resume SESSION_UUID`。TClaude 通知入口不接受额外的 `--settings`；
其他设置通过正常的 user/project settings 配置。

首次使用检查 `/hooks` 和项目目录信任状态。TCodex 提问通知需要审核并信任
匹配 `^request_user_input$` 的 hook；TClaude 提问 hook 匹配 `^AskUserQuestion$`。
禁用或未信任相关 hook 时，对应通知可能无法触发。

直接运行 `tcodex` 或 `tclaude`，以及之前已启动的会话，不会自动接入通知。
通过通知入口新建或恢复自己的会话，不要中断其他人的任务。

## 6. 日常使用与限制

- 日常只需启动 Mac 监听，并在服务器通过通知入口启动任务，无需每天下载或测试。
- Mac 必须在线且未休眠。SSH 断线会自动重连；认证仍由现有 SSH 配置负责。
- Mac 按 `Ctrl+C` 只停止监听，不会停止服务器任务。
- 首次启动监听会跳过历史事件；运行期间断线重连会补发未读事件。
- 回合结束通知不代表任务成功，需要回到原任务窗口检查结果。
- 提问通知表示准备调用结构化提问工具，不保证收到时仍在等待回答。
- 不覆盖普通聊天问句、命令权限审批或子代理提醒。
- 不要在运行期间删除服务器 `runtime/` 中的锁、队列或游标。
- 自定义 channel 时，修改 Mac 的 `--channel` 和服务器对应环境变量，保持一致。
- Channel 仅用于分流，不是权限隔离。

## 7. 排查

| 现象 | 检查项 |
| --- | --- |
| 提示 `osascript is unavailable` 或不能在 SSH 会话运行 | 回到 Mac 本地终端执行监听 |
| 提示远端脚本不存在 | 检查服务器绝对路径，目录名是复数 `*-notifications` |
| 反复 SSH 重连，没有 connected 日志 | 先单独验证 SSH 登录、端口和远端 Python |
| 只有一个来源 connected | 检查另一个目录和对应 SSH 流的错误输出 |
| connected，但没有 `Received event` | 确认 channel 一致，并在连接后发送测试事件 |
| 有 `Received event`，但没有弹窗 | 检查 Mac 通知权限、专注模式和 `osascript` 错误 |
| 合成测试正常，实际任务没有通知 | 确认通过通知入口启动，并检查 hook 加载与信任 |

## 8. 分享配置

只分享通用源码和本说明。个人 SSH 别名、地址、用户名、绝对路径和启动配置
应留在本机配置或 Git 忽略的 `local/` 中。
分享源码压缩包时排除 `local/`、`runtime/`、`.env`、本机配置和 `.git/`；
`.gitignore` 不会自动过滤压缩包内容。
