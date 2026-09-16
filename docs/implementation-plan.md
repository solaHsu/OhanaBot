# 部署与验收

本文件描述引擎的部署步骤、配置检查项与验收清单。

## 1. 环境与依赖

- Python 3.11+；依赖 `pyyaml`、`jieba`；接入真实模型需要 DeepSeek API Key。
- `runtime/`（数据库、记忆快照）与本地配置（`*.local.yaml`）均为不提交内容。

## 2. 配置

完整模板见 `config/bot.example.yaml`；本地使用 `config/bot.local.yaml`。密钥支持“本地直接值”或“环境变量名”两种方式，直接值优先。

```yaml
mode: dry_run            # dry_run | live
database: runtime/ohana.sqlite3
admins: []               # 可发 /exit 等指令的账号；留空时回退 allowed_private_users

onebot:
  api_base: http://127.0.0.1:3000
  listen_host: 127.0.0.1
  listen_port: 8080
  event_path: /
  expected_self_id: null
  access_token_env: ONEBOT_ACCESS_TOKEN
  event_secret_env: ONEBOT_EVENT_SECRET
  access_token: ""       # 或直接填值（仅限不提交的本地文件）
  event_secret: ""
  allowed_groups: []     # 空表示不接收任何群
  allowed_private_users: []
  self_aliases: ["ohana", "欧哈娜"]

engine:
  send_enabled: false
  merge_window_seconds: 3
  replies_per_hour: 20
  max_reply_characters: 300
  max_images_per_call: 3
  image_max_dimension: 768

llm:
  model: deepseek-flash
  base_url: https://api.deepseek.com
  api_key: ""
  api_key_env: DEEPSEEK_API_KEY
```

`mode` 的语义：`dry_run` 接收白名单事件、完整走判定与生成，但只打草稿不发送；`live` 且 `engine.send_enabled: true` 才真正发送。启动为软校验：缺少身份 / 白名单 / 密钥时仅告警；白名单为空则忽略全部群消息。

## 3. 部署步骤

1. 填写 `config/bot.local.yaml`（参考模板 `config/bot.example.yaml`）。
2. 部署 NapCat（或 Lagrange）并登录 Ohana 账号；开启 HTTP 服务；在「网络配置 → HTTP 客户端」添加事件上报，URL 指向 `http://127.0.0.1:8080/`，密钥与配置的 `event_secret` 保持一致。
3. 初始化：`python -m bot.engine.manage init`（建库、建目录、建链接，可重复执行）。
4. 以 `mode: dry_run` 启动 `python -m bot.engine.main`，发测试消息，确认入库日志与草稿输出正常。
5. 切换 `mode: live` 并开启 `engine.send_enabled`，重启后在测试群验证真实收发。
6. 运行期维护：`manage update`（每会话满 200 条触发记忆压缩，`--dry-run` 可预览）、`manage export`（导出快照）。

## 4. 验收清单

| 场景 | 预期结果 |
| --- | --- |
| 事件鉴权 | 错误签名 / 错误 Bearer 返回 401；正确签名（含 chunked 传输）入库并返回 204 |
| 重复回调 | 同一条消息重复上报只入库一次 |
| 白名单 | 未授权群的消息被丢弃，不写入记录 |
| 硬触发 | 被 @ / 引用 / 叫名字进入参与态，3 秒批后生成回复（must_reply 生效） |
| 兴趣追踪 | 两次类目命中进入关注；关注期内命中会话兴趣集升级为参与，超时回到旁观 |
| 参与衰减 | 12 条消息或 15 分钟无互动后退出参与并重建兴趣集 |
| 管理指令 | 管理员 `/exit` 立即回到旁观；非管理员无效 |
| dry-run | 生成草稿但不调用发送接口 |
| 限额 | 达到每小时模型调用上限后跳过生成并记录 |
| 发送记录 | 发送成功后自消息入库（含平台 message_id） |
| 发送未知 | 发送结果未知时会话被冻结，不再自动发送 |
| 记忆更新 | 满 200 条后 `manage update` 更新画像 / 条目 / 日常记录并前移游标；失败不写库 |
| 快照 | `manage export` 只输出有变化的 Markdown 文件 |
| 跨会话隔离 | 一个群的消息与画像不出现在其他会话的上下文里 |

## 5. 故障处理

- 生成失败 / 输出解析失败：跳过本批并记录日志，不影响后续消息。
- 发送结果未知：会话冻结，人工核对平台记录后重启恢复，不得直接重发。
- 紧急停止：把 `engine.send_enabled` 改回 `false` 并重启（发送前检查立即生效）；已发出的 HTTP 请求不能撤销。
