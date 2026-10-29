# tcodex-notification

支持 Mac 本地运行 TCodex 后直接显示原生通知，也支持服务器运行 TCodex、Mac 通过 SSH 接收通知。无需额外通知应用，不修改 TCodex 的安装、用户配置或 shell 配置。

## 架构

```text
本地：TCodex → notify hook → Mac osascript → 系统通知
远程：
TCodex → notify hook → 服务器事件队列 → SSH → Mac osascript → 系统通知
```

```text
server/
  tcodex-notify.sh       TCodex 启动入口
  notify-event.py        完成事件与测试入口
  notify_queue.py        队列、去重和保留限制
  stream-events.py     SSH 事件流
mac/
  tcodex-notify.sh       Mac 本地 TCodex 启动入口
  notify-event.py        Mac 本地通知 hook 与测试入口
  listen.sh             Mac 本地监听与通知
tests/
  test_notifications.py
.env.example            可选配置示例
runtime/                自动生成，Git 忽略
```

## Mac 本地使用

需要 macOS、已可用的 `tcodex`、Bash、Python 3.9+ 和系统自带的 `osascript`。
在 Mac 本地终端（包括 VS Code terminal）操作，不需要 SSH、监听窗口或事件队列。
以下 `/absolute/path/to/tcodex-notifications` 替换为本机仓库绝对路径。

先测试通知：

```bash
python3 -B /absolute/path/to/tcodex-notifications/mac/notify-event.py --test
```

命令成功只表示系统接受了通知请求。确认实际显示了横幅或通知中心记录；必要时检查通知权限与专注模式。

在需要处理的项目目录中，通过包装器启动新会话：

```bash
bash /absolute/path/to/tcodex-notifications/mac/tcodex-notify.sh
```

恢复会话或传入其他 TCodex 参数时直接追加：

```bash
bash /absolute/path/to/tcodex-notifications/mac/tcodex-notify.sh resume
```

每个回合结束时请求一条标题为 `TCodex`、正文为 `TCodex turn ended (local)` 的通知，提示音为 `Glass`。
通知表示回合结束，不代表任务成功。不判断终端是否在前台；通知失败不重试，也不影响 TCodex 任务。
不展示或保存提示词、回复和工作目录。

只有通过此包装器启动的会话启用本地通知。直接运行 `tcodex` 不会接入，已运行的会话需退出后通过包装器重新启动。
包装器关闭本次会话的 TUI 通知以避免重复提醒，且不改变工作目录。通知脚本随仓库保留，不要在会话运行期间移动仓库。

## 远程安装

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

## 远程使用

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

## 测试

```bash
python3 -B -m unittest discover -s tests
bash -n mac/tcodex-notify.sh mac/listen.sh server/tcodex-notify.sh
```
