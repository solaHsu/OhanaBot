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
├── analysis/                   # 脚本产物
│   ├── stats_report.md         # 统计报告（时段/长度/高频词/口头禅/标点/互动关系）
│   ├── corpus/members/         # 每人一份全量有效文本语料
│   └── samples/                # 抽样阅读材料
├── bot/                        # Ohana 主体
│   ├── memory/                 # 记忆系统
│   │   ├── soul/               # 灵魂层：性格/原则/风格规范
│   │   ├── style/              # 语言风格
│   │   └── conversations/      # 记忆：people.md 人物画像 + daily 日简述归档
│   └── engine/                 # 发言引擎
|       ├── context             # 上下文组装器
|       ├── onebot              # OneBot 11 接入：HTTP API 客户端 + 事件服务器 + 消息解析
|       ├── growth              # 成长：记忆固化，人格成长
|       └── TODO
└── docs/
    ├── architecture.md         # 项目整体流程
    ├── engine-spec.md          # 发言引擎规范
    └── lexicon-privacy.txt     # 隐私词表
```
## 快速开始

当前仓库处于资料准备与初版设计阶段，预处理脚本和运行引擎尚未实现。设计文档入口见 [docs/README.md](docs/README.md)，实施顺序与验收标准见 [docs/implementation-plan.md](docs/implementation-plan.md)。

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
