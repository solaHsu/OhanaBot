# 发言引擎规范

本规范描述 `bot/engine/` 的当前实现行为：OneBot 接入、事件解析与入库、发言判定、上下文组装、生成与发送。

## 1. OneBot 11 接入

- 事件上报：在本机监听 HTTP POST（默认 `127.0.0.1:8080`，路径 `/`），接收 OneBot 11 事件。
- 调用平台：HTTP API（默认 `http://127.0.0.1:3000`，Bearer token），动作有 `get_login_info`、`send_group_msg`、`send_private_msg`。
- 事件鉴权：`X-Signature`（HMAC-SHA1，常量时间比较）或 `Authorization: Bearer`，任一通过即可。
- 请求体同时支持 `Content-Length` 与 `chunked` 两种传输（NapCat 的 HTTP 客户端使用分块传输）。
- 状态码：入库成功 204；鉴权失败 401；请求体解析失败 400；超过 1 MiB 返回 413；落库失败 503；`GET /healthz` 返回服务与存储状态。
- 凭据：API token 与事件密钥分开配置，支持本地直接值或环境变量名；启动时校验登录账号与配置是否一致，不一致仅告警。
- 白名单：仅处理 `allowed_groups` 中的群消息，其余在写入正文前丢弃。

## 2. 事件解析

`bot/engine/onebot/parser.py`：

- 处理 `post_type=message` 的群消息；所有 ID 以字符串保存，时间统一为 UTC。
- 消息段数组优先，兼容 CQ 字符串；`text` / `at` / `reply` 结构化保存；图片段保留 url / 本地路径引用（供多模态生成取图）；`json` / `xml` 卡片提取标题、摘要、来源等文本（提取失败回退 `[卡片]`），卡片跳转链接保留（最多 3 个，供网页抓取）；其余媒体转为类型占位（`[语音]` 等）。
- 引用块内的 `at` 不视为提及；`at all` 不算提及；私聊事件当前不进入引擎。

## 3. 消息入库

`bot/engine/ingest.py`：

- 流程：白名单 → 去重（会话 + 平台消息 ID）→ 写入 `messages` → 记录群昵称历史 → 交给判定运行时。
- 入库与判定运行时共用一把串行锁，数据库读写不会并发冲突；重复回调直接忽略。
- 统计计数（accepted / rejected / duplicated）供日志观察。

## 4. 发言判定

`bot/engine/policy/`：完全由代码判定，旁观与关注状态零模型调用。三态为旁观 → 关注 → 参与。

| 转移 | 条件 |
| --- | --- |
| 旁观 → 参与 | 硬触发：被 @ / 引用回复 Ohana / 文本命中称呼（群昵称历史 + `self_aliases` 配置的别名） |
| 旁观 → 关注 | 兴趣类目命中一次后，10 条他人消息内同一类目再次命中（两条消息各计一次） |
| 关注 → 参与 | 关注期内（默认 15 分钟，`policy.attend_window_minutes`）命中会话兴趣集任一项，或硬触发 |
| 旁观/关注 → 参与 | 幸运升级：每条他人消息按 `policy.lucky_engage_probability`（默认 0.003）概率直接升级（0 = 关闭） |
| 关注 → 旁观 | 关注期超时（默认 15 分钟）无命中 |
| 参与 → 关注 | 12 条消息或 15 分钟无“指向 Ohana”的互动；退出时重建会话兴趣集 |
| 任意 → 旁观 | 管理指令 `/exit`（仅管理员账号触发；旁观后仍响应硬触发） |

- 兴趣词表：`bot/memory/interests.md`，类目 + 成员词，靠前类目优先。
- 命中判定：消息分词（jieba，大小写归一）与类目成员词求交集。
- 会话兴趣集：每会话一个，容量 5；类目优先填充，动态关键词（参与退出时按词频提取，单条消息内每词最多计一次）补位。
- “指向 Ohana”的互动：被 @ / 引用回复 / 被叫名字 / 紧邻接话（Ohana 发言后的第一条他人消息且间隔 ≤ 90 秒）。
- 默认参数：批窗口 3 秒、追踪窗口 10 条、关注期 15 分钟（`policy.attend_window_minutes`）、幸运升级 0.003（`policy.lucky_engage_probability`）、参与衰减 12 条 / 15 分钟、兴趣集容量 5、每小时模型调用 ≤ 20；yaml `policy:` 段可覆盖对应字段。

## 5. 上下文组装

`bot/engine/context.py` 按固定顺序拼成一个提示词（模板 `bot/engine/prompts/chat_reply.txt`）：

1. 人格与风格基线（`bot/memory/soul.md`、`bot/memory/style.md`，启动时读取）；
2. 本会话最近 2 段日常记录；
3. 相关人物资料：本批发言人 + 本批被 @ 者 + 最近对话窗口内的发言者（基本画像 + 最近 5 条条目；最多 `context.MAX_PROFILES`（默认 30）人，本批相关者优先、其余按新→旧；没有任何资料的人不注入；不含 Ohana 自身——自我由 soul/style 基线定义，避免自画像回灌造成人格漂移）；
4. 群内最近 200 条对话（`engine.recent_messages` 可调，不含本批）；
5. 本批新消息，格式 `[时间] 昵称(QQ号): 内容`（业务时区 +8）；
6. 本批消息里的图片（最多 `engine.max_images_per_call` 张、单张 ≤ 10MB），由 `bot/engine/media.py` 取回并转 base64 随请求附带；取不到、格式不明或超限的跳过；长边超过 `engine.image_max_dimension`（默认 768）的静态图压缩为 JPEG 后附带；另记本会话最近 10 分钟内出现过的图片（内存记忆，旁观态也算；被本批消息引用（replies_to）的优先），携带一次后失效——图片晚于判定窗口到达或隔窗口被引用时仍能识别；
7. 链接内容：来源为本批卡片链接 + 最近 10 分钟内本会话出现过的卡片链接（内存记忆、去重；仅首次被携带时注入一次，随后失效），最多 `engine.max_links_per_call` 个，由 `bot/tools/web.py` 抓取标题与正文并注入；仅 http/https、跨主机重定向需过公网检查（防 SSRF）、2MB 上限、5 分钟缓存、失败静默跳过；
8. 知识库摘录（`knowledge.enabled`）：以本批消息文本检索（jieba 分词 + BM25，命中实体时沿关系图扩展 `knowledge.graph_hops` 跳），命中才注入，最多 `knowledge.top_k` 块、合计 `knowledge.max_chars` 字的压缩摘录；不命中不注入（零 token）；来源为调研产物自动入库与 `manage knowledge rebuild` 的历史资料。整合层（`manage knowledge consolidate` 生成，`knowledge.inject_summaries` 可关）：命中实体时优先注入该实体的「知识档案」（跨文档事实合并、矛盾标注），同一板块 ≥2 个成员命中时追加「主题综述」（连通分量 + LLM 综述，成员数超阈值的大杂烩板块不生成）；整合结果可用 `manage knowledge export` 导出为 Markdown 快照（`runtime/knowledge/实体档案.md`、`主题综述.md`，内容有变化才写）；检索命中时引擎打 INFO 日志（注入块数与标题），也可用 `manage knowledge query <文本>` 离线预览将注入的内容。

硬触发批次附加“必须回复”提示。

## 6. 生成与发送

`bot/engine/runtime.py`：

- 输出约定 `{"action": "reply" | "silence", "text": "..."}`（json 模式，容忍代码围栏）。
- 硬触发不允许沉默：模型返回 silence 时跳过并记录，不硬发。
- `mode: dry_run` 只生成草稿并打日志；`mode: live` 且 `engine.send_enabled: true` 才真正发送。
- 回复长度上限默认 300 字，超出直接截断。
- 发送成功（`status=ok` 且 `retcode=0`）后：自消息写入 `messages`，并刷新参与计时。
- 发送结果未知（超时 / 连接中断）：冻结该会话，禁止自动重发，人工核对后重启恢复。
- 一次事件只发一条纯文本消息，不构造可执行消息段。
- 重型任务分流：模型可返回 `action: "task"` + `task`（需实时信息、联网核实或多步调研的场景），由 `bot/tools/harness.py` 在后台线程执行，完成后基于结果再生成一条汇报，并把任务结果自动分类入库（知识类进知识库，新闻跳过；见 §5 第 8 条）；同一会话同时只跑一个任务，未启用或失败时跳过；任务工作目录为 `harness.workspace`（默认 `runtime/harness/`），产物统一落在该目录。执行后端按 `harness.backend` 选择（接口一致，引擎无感知）：
  - `cli`（默认）：子进程调用 `dsh --profile headless`（dsh 内置联网搜索与网页抓取，超时默认 240 秒，需安装 dsh）；
  - `web`：`bot/tools/web_harness.py` 用 Playwright 自动化 DeepSeek 网页版（深度思考 + 智能搜索，零 API token）；取回答优先点网页版「复制」按钮拿 Markdown 源文（保留标题/表格/列表，失败退回纯文本）；登录态存于 `harness.web.profile_dir`（首次 `manage harness-login`）；调研结果同时落成 Markdown 文档（`web-时间-任务.md`）供文案引用；同一进程内任务串行，未登录或依赖缺失时跳过。

## 7. 运行与配置

```text
python -m bot.engine.main
```

- 配置文件：`config/bot.local.yaml`（模板 `config/bot.example.yaml`），包含白名单群、批窗口与限额、模型与密钥、OneBot 凭据。
- 重型任务分流（`harness.backend: web`）首次使用先运行 `python -m bot.engine.manage harness-login` 完成网页版登录（有头窗口扫码；登录态保存在 `harness.web.profile_dir`，过期后重跑即可）；换账号用 `manage harness-login --reset`（旧登录态自动改名为时间戳备份，需要时改名回切）。
- 知识整合（`manage knowledge consolidate`）默认用网页版生成档案/综述（`knowledge.consolidate_backend: web/api`，零 API token；未登录或缺依赖自动回退 API；`--think` 开深度思考，`--force` 强制重生成）；生成与引擎共用浏览器登录态，建议错峰运行。
- 入库审核（`manage knowledge rebuild` 与任务完成后的自动入库）同样默认走网页版（`knowledge.classify_backend: web/api`）；自动入库在汇报发出后才执行（不影响群回复），网页版失败会自动回退 API 重试，避免丢知识。
- 日志不记录密钥与聊天正文；判定与发送行为记录原因码（如 `reply:mention`、`decay:engaged`、`exit:command`）。

## 8. 当前边界

- 私聊指令通道未接入，私聊事件直接忽略。
- 无持久化 outbox：发送限额为进程内记账，重启后重新计数；被冻结的会话需人工核对后重启恢复。
- 重启后判定状态全部回到旁观。
