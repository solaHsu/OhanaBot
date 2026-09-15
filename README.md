# Ohana

**Ohana** —— 由 Agent 全程驱动的独立数字生命：在网络上发帖、评论、对话，拥有自己的名字、记忆、原则与思想。

## 项目结构

```
Ohana/
├── content/
│   ├── *.json                  # QQ 导出的群聊原始记录
│   └── posts/                  # 历史博客文章
├── scripts/
│   ├── 01_preprocess.py        # 初筛脚本：解析聊天 JSON，过滤无意义消息，提取语料+统计（已实现）
│   └── 02_sample.py            # 抽样脚本：供深度阅读分析（已实现）
├── config/
│   ├── analysis.example.yaml   # 分析配置模板（可提交）
│   └── analysis.local.yaml     # 本地分析配置：来源、机器人名单、候选成员（不提交）
├── analysis/                   # 脚本产物（本地，不提交）
│   ├── manifest.json           # 输入摘要、计数、版本与输出摘要
│   ├── stats_report.md         # 统计报告（时段/长度/高频词/口头禅/标点/互动关系）
│   ├── corpus/members/         # 每人一份全量有效文本语料
│   ├── samples/                # 抽样阅读材料 + 博客阅读材料
│   └── members/                # 全部成员的观察文档（P1 分析产物）
├── bot/                        # Ohana 主体
│   ├── memory/                 # 记忆系统
│   │   ├── soul.md             # 灵魂规范：身份/原则/价值观（v1，已冻结）
│   │   ├── style.md            # 语言风格与输出约束（v1，已冻结）
│   │   └── conversations/      # 记忆：people.md 人物画像 + daily 日简述归档（规划）
│   └── engine/                 # 发言引擎（规划）
|       ├── context             # 上下文组装器
|       ├── onebot              # OneBot 11 接入：HTTP API 客户端 + 事件服务器 + 消息解析
|       ├── growth              # 成长：记忆固化，人格成长
|       └── TODO
└── docs/
    ├── architecture.md         # 项目整体流程
    ├── engine-spec.md          # 发言引擎规范
    └── lexicon-privacy.txt     # 隐私词表
```
上方项目结构中 `scripts/`、`config/`、`analysis/` 已可运行（P1 资料管线）；`bot/memory/soul.md` 与 `bot/memory/style.md` 已冻结为 v1（人格与风格基线）；其余 `bot/` 与运行相关的接口、配置仍属待实现的规划约定。

## 快速开始（P1 资料管线，纯本地）

预处理与抽样已实现：聊天导出与博客的归一化、脱敏、统计、分层抽样与逐成员观察。

1. 安装依赖：Python 3.11+，`pip install pyyaml jieba`。
2. 复制 `config/analysis.example.yaml` 为 `config/analysis.local.yaml`，填写本地来源、
   机器人排除名单与候选参考成员（该文件不提交）。
3. 运行：

   ```text
   python scripts/01_preprocess.py --input content --output analysis --config config/analysis.local.yaml
   python scripts/02_sample.py --input analysis/corpus --output analysis/samples --seed 42
   ```

4. 产物在 `analysis/`：manifest、stats_report.md、逐成员语料与抽样、`analysis/members/` 成员观察。
   原始资料与全部分析产物均不提交（见 `.gitignore`）；重复运行结果稳定（corpus_digest 一致）。

发言引擎与记忆系统尚未实现，设计入口见 [docs/README.md](docs/README.md)，实施顺序与验收标准见 [docs/implementation-plan.md](docs/implementation-plan.md)。

## 初版设计

- [整体架构](docs/architecture.md)：范围、模块职责与端到端流程
- [资料处理](docs/data-pipeline.md)：聊天与博客归一化、抽样和人格初始化
- [发言引擎](docs/engine-spec.md)：OneBot 接入、触发策略、上下文和可靠发送
- [记忆与成长](docs/memory-spec.md)：分层记忆、固化、版本和回滚
- [隐私规范](docs/privacy-spec.md)：数据边界与隐私词表格式

上方项目结构包含规划中的目录和文件；文档内的接口及配置属于待实现约定。

## 平台接入（OneBot）

1. 准备一个独立 QQ 号给 Ohana，部署 [NapCat](https://github.com/NapNeko/NapCatQQ)（或 Lagrange）并登录
2. NapCat 开启 HTTP 服务（默认 `http://127.0.0.1:3000`）与 HTTP POST 事件上报（指向 `http://127.0.0.1:8080`）
