# 聊天内核与应用

## 内核和应用的边界

`bot.core` 使用 Python 标准库。入口通过 `open` 建立多成员会话，通过 `post` 提交普通消息，通过 `history` 读取授权会话。需要处理的消息用 `submit` 显式指定已注册的处理器。处理器收到本轮消息与会话历史，返回文本回复。`ConversationStore` 是存储接口，随代码提供 SQLite 实现。

处理器可调用 `await turn.forward(Forward("web.search", query), Forward("db.lookup", query))`。示例名称需要由相应应用注册后才可使用。每路调用各建一个子会话，保存父会话和触发消息 ID。子处理器只收到该路选择转发的内容，回执按请求顺序返回，每路可以独立成功或失败。子处理器可以继续转发，默认允许四层子会话。父处理器负责如何合并回执。

进程内应用通过接口隔离，注册的处理器是可信 Python 代码。接入层负责给调用者验明身份，内核根据传入的身份检查会话成员关系。网络服务可以由处理器调用，平台凭证和业务权限由各应用管理。后续 QQ 聊天、网页和发帖可各自接入这套接口。

## 启动本地聊天终端

使用 Python 3.11 或以上，从仓库根目录启动。离线回显模式可以检查 CLI、ChatCore 和 SQLite 是否连通。

```powershell
python -m bot.apps.chat --echo --debug
```

连接模型时，配置 `OHANA_MODEL_URL`（兼容 Chat Completions 的完整地址）、`OHANA_MODEL`（模型名），以及服务要求的 `OHANA_MODEL_API_KEY`，再运行 `python -m bot.apps.chat`。

- 启动时会显示会话 ID。退出后可用 `--conversation <ID>` 恢复，交互中可用 `/history` 查看记录，`/exit` 退出。
- 会话默认存入 `runtime/local-chat.sqlite3`。用 `--db <路径>` 选择调试库，或用 `--db :memory:` 跑临时测试。
- `--debug` 将消息 ID、上下文条数和回复耗时写入标准错误。`--context-messages N` 设置发给模型的消息上限，默认 40 条。上下文由当前输入和能完整容纳的近期问答组成。失败的输入仍可用 `/history` 查看。
- `--once "你好"` 处理一条消息后退出。回复写入标准输出，会话 ID 写入标准错误，可搭配 `--echo`、`--debug` 和 `--conversation` 用于脚本和回归验证。
- Windows 下 CLI 的输入和输出使用 UTF-8。

## 桌面工作区

桌面版使用 Tauri 2。安装 Python 3.11+ 与 Windows 的 Rust、MSVC 和 WebView2 依赖，从仓库根目录启动。 [Tauri 平台先决条件](https://v2.tauri.app/start/prerequisites/)

```powershell
cargo run --manifest-path desktop/src-tauri/Cargo.toml
```

新对话、会话恢复和活动记录与终端使用同一套 ChatCore。开发模式使用 `runtime/local-chat.sqlite3`，直接打包安装时在应用的本地数据目录保存工作区。可设置 `OHANA_DESKTOP_DB` 的绝对路径以使用独立调试库。Python 默认从 `PATH` 查找，也可以设置 `OHANA_PYTHON` 指定解释器。模型使用和 CLI 相同的环境变量，缺少模型配置时进入离线回显。

QQ 和飞书有独立的群聊名单与私聊设置，目前作为本地配置保存，平台状态显示为未连接。活动记录来自实际的聊天执行、错误和本地配置变更。

没有 Rust 工具链时，可使用同一套界面和 ChatCore 在本机浏览器预览。

```powershell
python -m bot.apps.desktop --preview
```

浏览器访问 `http://127.0.0.1:8765`。预览仅监听本机，可用 `--db <路径>` 指定独立调试库。

## 飞书办公助手原型（暂缓接入）

使用 Python 3.11 或以上，驱动方式是本机终端。飞书侧以企业自建应用身份读取配置群的历史消息和发送消息。应用需要开通相应的权限，机器人需要加入目标群。 [获取会话历史消息](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/list) · [发送消息](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/create) · [获取应用凭证](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/tenant_access_token)

在本机环境中配置 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`。如需总结和提取待办，再配置 `OHANA_MODEL_URL`（兼容 Chat Completions 的完整 URL）、`OHANA_MODEL`（模型名）以及服务所需的 `OHANA_MODEL_API_KEY`。总结与待办会把本次读取的文本发给所配置的模型，并把输出写入本地会话库。

```powershell
python -m bot.apps.feishu --group "项目=oc_项目群ID" --group "运营=oc_运营群ID"
```

| 输入 | 行为 |
| --- | --- |
| `帮我总结一下今天的消息` | 并行读取配置群按北京时间的当日文本，逐群概括进展、决定、风险和后续行动。 |
| `今天有哪些工作要跟进` | 从相同范围提取待办、承诺、负责人和截止时间。 |
| `群发 项目,运营 \| 明早十点开会` | 逐群发送正文并返回每个群的消息 ID 或发送异常。 |

`--once "今日总结"` 处理一条请求后退出。退出码 `0` 表示助手返回了应答，逐群业务结果以应答为准。交互模式可用 `--conversation <ID>` 继续已有会话，默认会话库在 `runtime/chat.sqlite3`。当前日结覆盖配置群的顶层文本消息，待办按需提取，定时推送和飞书内对话入口尚未接入。
