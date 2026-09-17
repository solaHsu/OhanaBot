# Ohana

**Ohana** —— 由 Agent 全程驱动的独立数字生命：在网络上发帖、评论、对话，拥有自己的名字、记忆、原则与思想。

## 项目结构

```
Ohana/
├── content/
│   ├── *.json                  # QQ 导出的群聊原始记录
│   └── posts/                  # 历史博客文章
├── scripts/
│   ├── 01_preprocess.py        # 初筛脚本：解析聊天 JSON，过滤无意义消息，提取语料+统计
│   └── 02_sample.py            # 抽样脚本：供深度阅读分析
├── config/
│   ├── analysis.example.yaml   # 分析配置模板（可提交）
│   └── analysis.local.yaml     # 本地分析配置：来源、机器人名单、候选成员（不提交）
├── analysis/                   # 脚本产物（本地，不提交）
│   ├── manifest.json           # 输入摘要、计数、版本与输出摘要
│   ├── stats_report.md         # 统计报告（时段/长度/高频词/口头禅/标点/互动关系）
│   ├── corpus/members/         # 每人一份全量有效文本语料
│   ├── samples/                # 抽样阅读材料 + 博客阅读材料
│   └── members/                # 全部成员的观察文档
├── bot/                        # Ohana 主体
│   ├── memory/                 # 记忆基线
│   │   ├── soul.md             # 灵魂规范：身份/原则/价值观（人格基线）
│   │   ├── style.md            # 语言风格提示词（风格基线）
│   │   ├── interests.md        # 兴趣词表（发言引擎用，所有者维护）
│   │   └── conversations/      # 记忆快照链接（指向 runtime/memory/）
│   ├── tools/                  # 工具集：web.py 抓取、harness.py 任务桥接（dsh 联网）
│   └── engine/                 # 发言引擎与记忆系统
│       ├── main.py             # 入口：事件服务 + 判定运行时
│       ├── manage.py           # 管理命令：init / export / update
│       ├── config.py           # 配置加载（bot.local.yaml；密钥支持环境变量）
│       ├── onebot/             # 接入：client / parser / server（签名、chunked）
│       ├── ingest.py           # 消息入库管道（白名单/去重/昵称历史）
│       ├── policy/             # 发言判定：三态状态机 + 兴趣集（纯代码）
│       ├── context.py          # 生成上下文组装（人格 + 记忆 + 对话）
│       ├── media.py            # 图片附件取回（多模态生成）
│       ├── runtime.py          # 判定运行时：批窗口 / 生成 / 发送闸门
│       ├── llm/                # 模型客户端（DeepSeek）
│       ├── growth/             # 记忆更新与快照导出
│       └── storage/            # SQLite（连接/迁移/数据访问）
├── tests/                      # 单元与回放测试
├── runtime/                    # 运行数据：数据库、记忆快照（不提交）
└── docs/
    ├── architecture.md         # 项目整体流程
    ├── engine-spec.md          # 发言引擎规范
    ├── memory-spec.md          # 记忆与成长规范
    └── lexicon-privacy.txt     # 隐私词表
```
`scripts/` 资料管线与 `bot/engine/` 引擎均可运行；密钥、原始资料与运行数据不提交。

## 快速开始（资料管线，纯本地）

预处理与抽样覆盖聊天导出与博客的归一化、脱敏、统计、分层抽样与逐成员观察。

1. 安装依赖：Python 3.11+，`pip install pyyaml jieba pillow`（pillow 用于图片压缩，未安装则按原图发送）。
2. 复制 `config/analysis.example.yaml` 为 `config/analysis.local.yaml`，填写本地来源、
   机器人排除名单与候选参考成员（该文件不提交）。
3. 运行：

   ```text
   python scripts/01_preprocess.py --input content --output analysis --config config/analysis.local.yaml
   python scripts/02_sample.py --input analysis/corpus --output analysis/samples --seed 42
   ```

4. 产物在 `analysis/`：manifest、stats_report.md、逐成员语料与抽样、`analysis/members/` 成员观察。
   原始资料与全部分析产物均不提交（见 `.gitignore`）；重复运行结果稳定（corpus_digest 一致）。

## 运行 Ohana（引擎）

1. 填写 `config/bot.local.yaml`：Ohana 的 QQ 号、允许的群、DeepSeek Key（或对应环境变量）与 OneBot 凭据。
2. 部署 NapCat 并登录 Ohana 账号：开启 HTTP 服务（端口与配置 `onebot.api_base` 一致），在「网络配置 → HTTP 客户端」添加事件上报指向 `http://127.0.0.1:8080/`，密钥与配置 `event_secret` 保持一致。
3. 启动：`python -m bot.engine.main`（`mode: dry_run` 只生成草稿；`live` + `engine.send_enabled: true` 才真正发送）。
4. 记忆管理：`python -m bot.engine.manage init | export | update`（每个会话每满 200 条消息触发记忆压缩；`update --dry-run` 可预览）。
5. 测试：`python -m unittest discover -s tests`。

设计入口见 [docs/README.md](docs/README.md)，实施顺序与验收标准见 [docs/implementation-plan.md](docs/implementation-plan.md)。

## 初版设计

- [整体架构](docs/architecture.md)：范围、模块职责与端到端流程
- [资料处理](docs/data-pipeline.md)：聊天与博客归一化、抽样和人格初始化
- [发言引擎](docs/engine-spec.md)：OneBot 接入、判定运行时、上下文和发送
- [记忆与成长](docs/memory-spec.md)：人物画像、日常记录、批量更新与保留
- [隐私规范](docs/privacy-spec.md)：数据边界与隐私词表格式

## 平台接入（OneBot）

1. 准备一个独立 QQ 号给 Ohana，部署 [NapCat](https://github.com/NapNeko/NapCatQQ)（或 Lagrange）并登录。
2. NapCat 开启 HTTP 服务（认证 token 与配置 `access_token` 或环境变量一致）；在「网络配置 → HTTP 客户端」添加事件上报，URL 指向 `http://127.0.0.1:8080/`，密钥与配置 `event_secret` 一致（引擎兼容签名与 chunked 传输）。
3. 排查：事件收不到先看引擎日志（鉴权失败/路径不符）；`http://127.0.0.1:8080/healthz` 可查存活。
