# 记忆与成长规范

本规范描述 `bot/engine/storage/`、`bot/engine/growth/` 与 `bot/engine/manage.py` 的当前实现行为。

## 1. 分层与索引

| 层 | 载体 | 内容 | 修改方式 |
| --- | --- | --- | --- |
| 灵魂 | `bot/memory/soul.md` | 名字、独立身份、原则、稳定性格 | 显式审阅后更新 |
| 风格 | `bot/memory/style.md` | 分场景表达习惯与输出约束 | 显式审阅后更新 |
| 人物画像 | SQLite + 快照 `runtime/memory/persons/` | 基本画像、称呼、条目 | 批量更新（覆盖 / 追加） |
| 日常记录 | SQLite + 快照 `runtime/memory/logs/` | 按消息量滚动压缩的摘要 | 批量生成 |
| 兴趣词表 | `bot/memory/interests.md` | 发言判定的参与兴趣类目 | 人工维护 |

索引使用平台原生不可变 ID：会话 = `平台:类型:原生会话ID`（如 `qq:group:123456`）；人 = `平台:原生用户ID`（如 `qq:10001`）。昵称与群名只作显示缓存，永不作为索引。一切条目以会话为最小作用域：同一人在不同会话各存一份，互不可见。当前仅支持群聊。

## 2. 人物画像（三块）

- 基本画像：固定维度——性格 / 说话风格 / 兴趣与擅长 / 偏好 / 群内关系 / 其他（兜底）；每维 1-2 句；整段覆盖，不留历史版本；没信息的维度留空。
- 称呼 · 群昵称历史：程序从消息数据自动记录；与最新一条相同则刷新时间，变更即追加，保留全部历史。
- 称呼 · 绰号：群友常叫的绰号/别称（含本人认可的自称），由模型识别后追加，重复跳过。
- 条目：具体的事——互动、玩笑、争论，提到/被提到的事实；只追加，重复文本跳过。

## 3. 日常记录（滚动压缩）

- 每个会话每满 200 条消息压缩成一段（批大小可用 `--batch-size` 调整）。
- 库中为每个会话维护游标（已压缩到的消息位置）；攒够触发，处理完游标前移；同一会话串行执行。
- 一段记录写简短摘要：发生了什么、谁和谁互动、有无未决事项。

## 4. 更新机制（每满一批触发）

1. 触发：会话待压缩消息 ≥ 批大小（默认 200）。
2. 上下文：本批消息 + 涉及人物资料（发言者 + 被 @ 的人，含基本画像 / 称呼 / 已有条目）+ 最近 3 段日常记录（`--recent-logs` 可调）。
3. 模型：DeepSeek（默认 `deepseek-flash`，json 输出模式），提示词见 `bot/engine/growth/prompts/memory_update.txt`。
4. 写入：单事务提交——基本画像覆盖、绰号 / 条目追加（去重）、追加记录段、游标前移；失败整体回滚，游标不前进，可安全重跑。
5. 命令：

```text
python -m bot.engine.manage update [--conversation qq:group:123456] [--batch-size 200]
                                   [--recent-logs 3] [--dry-run] [--model 模型] [--api-key Key]
```

`--dry-run` 只调用模型预览，不写库；Key 默认读环境变量 `DEEPSEEK_API_KEY`。

## 5. 存储

- 权威库：`runtime/ohana.sqlite3`（WAL）。表：`conversations`（含游标）、`messages`、`persons`、`names`、`entries`、`logs`；结构变更按 `PRAGMA user_version` 版本化迁移。
- 快照：自动导出、只读、原子替换（内容无变化不重写）：人物一人一份 `runtime/memory/persons/<平台_类型_会话ID>/<用户ID>.md`；日常记录一会话一份 `runtime/memory/logs/<平台_类型_会话ID>.md`。
- `bot/memory/conversations/` 下保留目录链接（symlink / junction）指向 `runtime/memory/`，保证 bot/ 可导航；链接与数据不提交。
- 管理命令：

```text
python -m bot.engine.manage init     # 建库、建目录、建链接（可重复执行）
python -m bot.engine.manage export   # 导出全部 Markdown 快照
python -m bot.engine.manage update   # 对攒满一批消息的会话执行记忆更新
```

## 6. 保留与当前边界

- 目标保留策略：消息正文 30 天、人物条目 90 天、日常记录 180 天、灵魂与风格长期保留。
- 上述清理任务与删除传播尚未接入：数据库当前不做自动清理，按人 / 会话删除的入口也未实现。

## 7. 成长

风格成长提案（周期性从会话中提炼、只允许调整白名单风格参数、可审阅回滚）尚未实现。人物画像与条目的更新不会改动 `soul.md` / `style.md` 基线。
