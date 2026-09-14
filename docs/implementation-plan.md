# 首版实施与验收

## 1. 阶段与交付物

| 阶段 | 工作 | 完成门槛 |
| --- | --- | --- |
| P0 工程基础 | 依赖锁定、配置校验、忽略规则、匿名测试夹具、SQLite 迁移、基础日志 | 本地启动和迁移可重复执行，缺密钥不产生发送 |
| P1 资料管线 | 两个 README 规划脚本、归一化、脱敏、统计、抽样、manifest | 数量可核对；重复运行稳定；草稿、撤回和媒体正确分区 |
| P2 人格基线 | 参考成员配置、分层阅读、候选人格、风格与例句、版本导入 | 每项结论有证据；独立身份成立；初始化版本确认 |
| P3 离线引擎 | 事件归一化、策略、上下文、模型抽象、输出校验、dry-run | 固定回放场景通过；无网络发送副作用 |
| P4 平台闭环 | OneBot 鉴权、持久化队列、outbox、限额、故障恢复 | 白名单测试会话完成真实收发；重复和不确定发送受控 |
| P5 记忆与成长 | 摘要、画像、检索、到期、删除、提案、版本回滚 | 无跨会话泄漏；可删除派生记录；成长失败保持旧版本 |
| P6 受限运行 | 小范围持续运行与指标观察，按群启用主动参与 | 观察至少 7 天，无未处理的 unknown、隐私或重复发送问题 |

每阶段只声称已通过的能力。发帖、评论与其他平台安排在首版闭环稳定之后，通过新的平台适配器复用决策、记忆和 outbox，并单独定义发布权限与幂等策略。

## 2. 配置基线

以下是待实现的 `config/bot.example.yaml` 结构，非现有配置文件。密钥从环境变量读取，示例配置不包含真实账号。

```yaml
mode: dry_run
timezone: Asia/Shanghai
onebot:
  api_base: http://127.0.0.1:3000
  listen_host: 127.0.0.1
  listen_port: 8080
  event_path: /
  expected_self_id: null
  access_token_env: ONEBOT_ACCESS_TOKEN
  event_secret_env: ONEBOT_EVENT_SECRET
  allowed_groups: []
  allowed_private_users: []
llm:
  provider: null
  model: null
  api_key_env: OHANA_LLM_API_KEY
  outbound_policy: redacted_only
  max_context_tokens: 8192
  max_output_tokens: 512
  timeout_seconds: 30
  max_retries: 1
  daily_token_budget: 100000
engine:
  send_enabled: false
  max_concurrency: 2
  merge_window_seconds: 2
  event_max_age_seconds: 120
  reply_cooldown_seconds: 15
  replies_per_hour: 20
  max_reply_characters: 300
  proactive_enabled: false
  proactive_cooldown_seconds: 600
  proactive_per_hour: 3
  quiet_hours: ["00:00", "08:00"]
storage:
  database: runtime/ohana.sqlite3
  event_retention_days: 30
  daily_retention_days: 180
  person_retention_days: 90
  backup_retention_days: 7
privacy:
  public_lexicon: docs/lexicon-privacy.txt
  local_lexicon: runtime/privacy/lexicon.local.txt
```

`mode` 可取 `replay`、`dry_run`、`live`。`replay` 使用固定夹具与默认 mock 模型，无平台联网；`dry_run` 可接收白名单实时事件并生成脱敏草稿，但 sender 被程序禁用；`live` 还必须显式启用 `send_enabled`。

正式运行缺少账号身份、白名单、有效人格版本、本地隐私规则或模型配置时启动失败。回放允许使用虚构人格和词表。每日预算覆盖决策、生成、修复、摘要和成长所有模型调用，按业务时区换日；事务预留估算 token，响应后按实际用量结算，失败且用量不明时保守保留预留。

## 3. 上线步骤

当前没有可运行入口，以下是实现完成后的操作顺序：

1. 配置 Python 环境与锁定依赖，建立本地配置、密钥和忽略规则。
2. 执行资料预处理与抽样，核对统计与脱敏效果，确认参考成员和人格初始版本。
3. 使用匿名固定夹具回放，完成权限隔离、发送状态机和记忆删除验收。
4. 准备独立 QQ 账号，部署 NapCat 或 Lagrange，启用 HTTP API 与 HTTP POST 上报，分别配置 token 与上报认证。
5. 上报地址设为 Ohana 的 `http://127.0.0.1:8080/`，API 地址默认为 `http://127.0.0.1:3000`。分容器或跨主机时根据网络拓扑修改，不能把容器内回环地址当成宿主机地址。
6. 以 dry-run 接入一个测试群，确认账号身份、事件签名、时区和日志脱敏。
7. 显式切换 live 并开启发送，先验收直接提及和私聊，再按群开启主动参与。
8. 验证停发、备份恢复及 unknown 处理流程，观察指标并进入 P6。

紧急停止首先关闭全局发送开关，使发送前检查立即生效；已发出的 HTTP 请求不能承诺撤销。随后暂停 worker 并保存状态。重启前核对所有 `sending/unknown` 记录；不得清空数据库“解决”重复或未知发送。

## 4. 验收矩阵

使用虚构的小型 JSON、Markdown 和 OneBot 回调夹具，自动测试不依赖真实群聊原文。模型默认 mock；供应商联网和平台实发作为独立集成验收。

| 场景 | 预期结果 |
| --- | --- |
| 超过安全整数范围的消息 ID；秒与毫秒时间 | ID 无精度损失，统一时间准确 |
| 重复导出、正常复读、撤回、系统消息、纯媒体 | 去重不误删正常复读；过滤原因与数量准确 |
| 博客草稿、TODO、代码、失效图片引用 | 草稿隔离、空内容排除，代码不污染聊天风格 |
| 相同输入、配置和随机种子重复处理 | 语料集合、统计和样本稳定 |
| 非法签名、超大请求、非白名单消息 | 拒绝或忽略，不保存正文，不调用模型 |
| 同一消息重复回调、worker 崩溃后恢复 | 一份事件和任务，不重复确认发送 |
| 持久化后尚未返回 2xx 时崩溃 | 回调重试命中唯一键，任务仍可恢复 |
| 自身消息、at all、引用内提及、普通群消息 | 不错误触发直接回复 |
| 多条消息合并、迟到事件、生成期间新消息 | 串行决策，草稿时效符合规范 |
| 超长上下文与注入式聊天/博客 | 固定原则保留，引用不提升为系统指令 |
| 模型返回非法 JSON、越权 reply_to、CQ 指令 | 修复次数有上限，越权阻断，输出不执行 CQ |
| 词表变体、号码、隐私输出、错误日志 | 脱敏或阻断，日志不暴露敏感原文 |
| 群 A 资料被群 B 或同人私聊请求 | 权限过滤后不可检索 |
| 429、超时、平台明确拒绝、发送结果未知 | 依规范重试或暂停，不无限重试、不重复发言 |
| 多会话并发与预算边界 | 全局并发、预算预留和每会话限额不超额 |
| dry-run、关闭发送或发送前移出白名单 | sender 不调用平台发送接口 |
| 摘要任务重复、画像冲突、迟到消息 | 幂等更新，冲突留作候选，新版替代旧版 |
| 成长越界、回放失败、版本回滚 | 核心原则不变，失败保持旧版本，回滚可追溯 |
| 删除人物、重导入资料、恢复旧备份 | 派生内容清除，墓碑阻止恢复与再次导入 |

## 5. 后续扩展条件

仅在检索质量评估证明关键词检索不足时引入向量检索；仅在风格回放与资料质量成熟后评估微调。多平台扩展必须保持来源与会话权限隔离，公开发布不得直接复用私聊事实记忆。自主定时发帖需另行设计话题来源、重复检查、发布预算及平台错误恢复。
