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
  max_links_per_call: 2

llm:
  model: deepseek-flash
  base_url: https://api.deepseek.com
  api_key: ""
  api_key_env: DEEPSEEK_API_KEY
  reasoning_effort: high # 思考档位 none/minimal/low/medium/high（思考 token 计入 max_tokens，实测 high 约 1200）
  max_tokens: 8192 # 上限须覆盖思考 token + 回复正文（仅作保护，不实际扣费）
  retries: 2 # 可重试失败（空输出/限流/网络抖动）自动重试次数；截断与鉴权不重试

harness:
  enabled: false # 重型任务分流（action=task）
  backend: cli # cli = dsh headless；web = DeepSeek 网页版自动化（Playwright）
  command: dsh # 仅 cli
  timeout_seconds: 240
  model: "" # 仅 cli
  workspace: runtime/harness
  web: # 仅 web：首次先运行 manage harness-login 完成网页版登录
    profile_dir: runtime/harness/web_profile
    headless: true
    deep_think: true
    search: true
    save_documents: true

knowledge:
  enabled: true # 知识库（GraphRAG-lite）：入库 + 检索注入
  auto_ingest: true # 调研产物自动分类：知识入库、新闻跳过
  top_k: 2 # 每次注入知识块上限
  max_chars: 600 # 注入文本预算（字符）
  min_matches: 2 # 词法命中词数下限
  graph_hops: 1 # 实体图扩展跳数（0 = 关闭）
  inject_summaries: true # 命中实体时优先注入实体档案/主题综述
  consolidate_backend: web # 档案/综述生成后端：web = 网页版（零 API token）；api = DeepSeek API
  classify_backend: web # 入库审核后端（rebuild 与自动入库）：web / api
```

`mode` 的语义：`dry_run` 接收白名单事件、完整走判定与生成，但只打草稿不发送；`live` 且 `engine.send_enabled: true` 才真正发送。启动为软校验：缺少身份 / 白名单 / 密钥时仅告警；白名单为空则忽略全部群消息。

## 3. 部署步骤

1. 填写 `config/bot.local.yaml`（参考模板 `config/bot.example.yaml`）。
2. 部署 NapCat（或 Lagrange）并登录 Ohana 账号；开启 HTTP 服务；在「网络配置 → HTTP 客户端」添加事件上报，URL 指向 `http://127.0.0.1:8080/`，密钥与配置的 `event_secret` 保持一致。
3. 初始化：`python -m bot.engine.manage init`（建库、建目录、建链接，可重复执行）。
4. 以 `mode: dry_run` 启动 `python -m bot.engine.main`，发测试消息，确认入库日志与草稿输出正常。
5. 切换 `mode: live` 并开启 `engine.send_enabled`，重启后在测试群验证真实收发。
6. 运行期维护：`manage update`（每会话满 200 条触发记忆压缩，`--dry-run` 可预览）、`manage export`（导出快照）。
7. 重型任务分流选 `harness.backend: web` 时：先 `pip install playwright` 与 `python -m playwright install chromium`，再 `python -m bot.engine.manage harness-login` 完成一次有头登录；调研产物（Markdown 文档）落在 `runtime/harness/`。
8. 知识库：`python -m bot.engine.manage knowledge rebuild` 将历史调研资料分类入库（新闻自动跳过，`--dry-run` 预览）；`knowledge consolidate` 生成实体档案与主题综述（整合层，未变化自动跳过，默认网页版后端、零 API token）；`knowledge stats` 查看统计。

## 4. 验收清单

| 场景 | 预期结果 |
| --- | --- |
| 事件鉴权 | 错误签名 / 错误 Bearer 返回 401；正确签名（含 chunked 传输）入库并返回 204 |
| 重复回调 | 同一条消息重复上报只入库一次 |
| 白名单 | 未授权群的消息被丢弃，不写入记录 |
| 硬触发 | 被 @ / 引用 / 叫名字进入参与态，3 秒批后生成回复（must_reply 生效） |
| 兴趣追踪 | 两次类目命中进入关注；关注期（默认 15 分钟，可配）内命中会话兴趣集升级为参与，超时回到旁观；另有 0.3% 概率（可配）幸运升级直接进入参与 |
| 参与衰减 | 12 条消息或 15 分钟无互动后退出参与并重建兴趣集 |
| 管理指令 | 管理员 `/exit` 立即回到旁观；非管理员无效 |
| dry-run | 生成草稿但不调用发送接口 |
| 限额 | 达到每小时模型调用上限后跳过生成并记录 |
| 发送记录 | 发送成功后自消息入库（含平台 message_id） |
| 发送未知 | 发送结果未知时会话被冻结，不再自动发送 |
| 记忆更新 | 满 200 条后 `manage update` 更新画像 / 条目 / 日常记录并前移游标；失败不写库 |
| 快照 | `manage export` 只输出有变化的 Markdown 文件 |
| 跨会话隔离 | 一个群的消息与画像不出现在其他会话的上下文里 |
| 链接抓取 | 分享卡片（含随后被 @ 或提及）后正文注入生成；内网 / 非 http(s) 地址被拒绝 |
| 任务分流 | 需实时信息或复杂调研的请求交给执行后端（cli=dsh headless / web=DeepSeek 网页版）并把结果带回群；依赖缺失或未登录时跳过不报错；web 后端结果同时落成 Markdown 文档 |
| 知识库 | 调研产物自动分类：知识入库（含实体关系）、新闻跳过；命中话题时上下文注入摘录，实体档案/主题综述优先（不相关不注入）；`manage knowledge rebuild/consolidate/stats` 可用 |

## 5. 故障处理

- 生成失败 / 输出解析失败：跳过本批并记录日志，不影响后续消息。可重试类失败（空输出 / 限流 429 / 5xx / 网络抖动）由客户端自动退避重试（`llm.retries`，默认 2 次）；输出截断（`finish_reason=length`）说明思考 token 挤占了 `llm.max_tokens` 配额：调大 `llm.max_tokens`（参考 8192）或降低 `llm.reasoning_effort`。
- 发送结果未知：会话冻结，人工核对平台记录后重启恢复，不得直接重发。
- 紧急停止：把 `engine.send_enabled` 改回 `false` 并重启（发送前检查立即生效）；已发出的 HTTP 请求不能撤销。
