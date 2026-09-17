# Ohana 设计文档

文档与当前实现保持一致；目录、配置与行为以 `bot/` 代码为准。

## 阅读顺序

| 文档 | 内容 |
| --- | --- |
| [architecture.md](architecture.md) | 定位、范围、端到端流程、模块目录与技术选择 |
| [data-pipeline.md](data-pipeline.md) | 聊天与博客处理、抽样、统计、人格初始化 |
| [engine-spec.md](engine-spec.md) | OneBot 接入、事件入库、发言判定、上下文、生成与发送 |
| [memory-spec.md](memory-spec.md) | 人格基线、人物画像、日常记录、更新机制与存储 |
| [privacy-spec.md](privacy-spec.md) | 数据分区、模型边界、隐私词表与处理流程 |
| [implementation-plan.md](implementation-plan.md) | 环境、配置、部署步骤与验收清单 |
| [lexicon-privacy.txt](lexicon-privacy.txt) | 可提交的隐私词表格式及占位示例 |

## 基本约定

- Ohana 是独立身份。历史材料用于提取表达习惯、兴趣和交流方式，不能把原作者经历写成 Ohana 的亲身经历。
- 当前闭环：QQ 群聊文本交流、受限主动参与（三态判定 + 兴趣词）、可追溯记忆；私聊与网络发帖为后续能力。
- Python 单进程服务、OneBot 11 HTTP API + HTTP POST、SQLite 权威记录 + Markdown 快照；关键依赖 pyyaml、jieba 与 pillow（图片压缩）。
- 模型使用 DeepSeek（`deepseek-flash`）；参考成员映射与运行群名单只在本地配置中维护。
- 文档内所有编号与人物示例均为虚构；实际映射、原始资料及运行数据留在本地。

## 关键文件速查

| 项目 | 位置 |
| --- | --- |
| 人格与风格基线 | `bot/memory/soul.md`、`bot/memory/style.md` |
| 兴趣词表 | `bot/memory/interests.md` |
| 网页抓取 | `bot/tools/web.py` |
| 重度任务桥接 | `bot/tools/harness.py`（cli=dsh headless）与 `bot/tools/web_harness.py`（web=DeepSeek 网页版）；产物在 runtime/harness/ |
| 引擎入口 | `python -m bot.engine.main`（配置 `config/bot.local.yaml`） |
| 管理命令 | `python -m bot.engine.manage init / export / update / harness-login` |
| 权威数据与快照 | `runtime/ohana.sqlite3`、`runtime/memory/` |
| 测试 | `python -m unittest discover -s tests` |
