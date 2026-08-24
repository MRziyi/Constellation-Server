# Constellation Server

[English](README.md) · **简体中文**

[Constellation](https://github.com/MRziyi/Constellation) 的 Mac 端运行时——一个面向全天候
可穿戴助理的个人 AI 框架。本仓库包含两个守护进程（一个负责思考，一个负责动手），以及它们
读写的那套 Markdown 知识库的种子。

> **适用范围。** 这是一个单人日常在用的研究原型，不是产品。它跑在 macOS 上，通过 AppleScript
> 驱动苹果应用，系统 prompt 里直接写着作者的名字。部署之前请先读 [已知局限](#已知局限)。

## 两个守护进程

| | **Cortex**——大脑 | **Tool Agent**——双手 |
|---|---|---|
| 启动 | `python -m cortex.main` | `python -m tool_agent.main` |
| 监听 | WSS `:8888`（眼镜）· HTTP `:8890`（管理） | WS `127.0.0.1:8889`（只对 Cortex） |
| 职责 | 语音转写、意图分类、规划、agent 执行、写 Twin | 每个 RPC 通过一个 adapter 执行一个叶子动作 |
| 对外 | OpenAI / Groq / Claude Agent SDK，以及 Tool Agent | macOS 应用、文件系统、Claude Code CLI |

只有 Cortex 会推理。Tool Agent 刻意不推理：它就是一堆小而可审计的 adapter 的注册表，好让
「这个系统究竟能对我的机器做什么」这个问题，靠读一个目录就能答上来。

## 一次请求的路径

```
 眼镜 ──音频分片──▶ whisper_pipeline ──▶ STT 复核卡 ──▶（你批准）
                                                            │
                                                            ▼
                                                        意图分类器
                                            SIMPLE ◀───────┴───────▶ COMPLEX
                                               │                       │
                                          router.py               claude_sdk_agent
                                     （单次 adapter 派发）      （多步、阶段检查点、
                                               │                 Twin 作为工作目录）
                                               ▼                       │
                                          Tool Agent ◀─────────────────┘
                                               │
                                   有副作用？ ──▶ 预览卡 ──▶（你批准）
                                               │
                                               ▼
                                        执行 · 写 receipt · 推 HUD 卡片
```

有两道闸是承重的，不提供关掉的开关：

- **STT 复核**——转写结果先给你看，再让任何东西基于它行动。听错的话绝不会悄悄变成动作。
- **副作用预览**——发邮件、加提醒、加日程、发消息、往 Twin 之外写文件，一律先出预览卡。只读操作不需要批准。

## 目录结构

```
cortex/            大脑
  cortex/
    main.py              CLI 入口；加载 .env，绑定 WSS + HTTP
    server.py            面向眼镜的 WebSocket 端点与一轮对话的编排
    whisper_pipeline.py  whisper.cpp 语音转写（双档：快出草稿 + 精出终稿）
    classifier.py        一次廉价调用，判定 SIMPLE 还是 COMPLEX
    router.py            SIMPLE 路径——事件到单次 adapter 派发
    claude_sdk_agent.py  COMPLEX 路径——进程内 Claude Agent SDK
    agent_brief.py       递给 agent 的那份 brief 的唯一真源
    prompts.py           所有可调 prompt 常量，集中一处
    llm_cache.py         所有 LLM 调用的唯一收口：缓存、重试、JSON 修复
    twin.py              Twin 读写与 CHANGELOG 追加
    face_index.py        端上人脸识别（InsightFace，CoreML）
    enroll_parser.py     把口述简介解析成结构化人物记录
    insight_engine.py    主动浮现
    distiller.py         把隐式反馈沉淀成 Twin 更新
    sessions.py          会话线程 · session_router.py · session_browser.py
    markdown_runs.py     Markdown 转 HUD 的样式化文本段
    mail_inbound.py      收到邮件 → 可口述回复的 HUD 卡片
    http.py              管理用 HTTP 接口 · control_plane.py  状态镜像
    schema.py            Pydantic 协议模型 · ids.py · tcc_check.py · audio_buffer.py
  launchd/               plist 模板，由 scripts/install.sh 安装

tool-agent/        双手
  adapters.yaml          加载哪些 adapter——这个文件就是能力边界
  tool_agent/
    server.py            回环地址上的 WS 服务 · registry.py  adapter 加载
    adapters/            一个 adapter 一个文件（见下表）

twin-seed/         数字孪生的出厂状态
scripts/           安装与运维（start/stop/restart/status/logs）
test-harness/      像真实客户端一样驱动 Cortex 的端到端流程
```

### Adapters

`adapters.yaml` 就是全部能力边界——没列在里面的 adapter 就够不着，模型再怎么要也没用。

| Adapter | 能做什么 |
|---|---|
| `claude_code` | 调用 Claude Code CLI：起草、执行、续接、列会话 |
| `applescript_mail` | 通过 Mail.app 读、搜、起草、发送——发送一律先预览 |
| `applescript_calendar` · `applescript_reminders` | 读取与创建日程、提醒 |
| `apple_notes` | 读写备忘录 |
| `apple_shortcuts` | 调用任意用户自定义的「快捷指令」 |
| `imessage` | AppleScript 发送；从 `chat.db` 读近期会话（需完全磁盘访问权限） |
| `safari_state` | 当前标签页、全部标签页、近期历史 |
| `fs` | 读、写、追加、grep、列目录、删除——写入限定在白名单根目录内 |
| `system_status` | 电量、专注模式、前台应用、网络、时间 |
| `twin_query` | 对 Twin 的语义问答 |
| `echo` | 主干连通性测试 |

这里刻意**没有**「图像转文字」的 adapter：一帧画面作为多模态图像块直接进入 planner，由模型
自己看，而不是先被压成一段有损的文字描述。

## 数字孪生 Twin

系统关于你的一切，都是 `~/constellation/twin/` 下的 Markdown 文件，由
[`twin-seed/`](twin-seed/) 播种。没有数据库、没有向量库、没有锁定——`grep` 能用，`vim` 能用，
`git init` 也能用。

```
identity.md              你是谁，以及你希望别人怎么替你落笔
people/core/<slug>.md     一人一个文件
projects/<Name>.md        一个项目一个文件，含它的代码在哪
memos/<slug>.md           保存的笔记，常带一张照片
follow-ups.md             你对别人许下的承诺
receipts/<date>.md        以你名义执行过的每个动作，只增不改
CHANGELOG.md              系统对 Twin 的每一次写入，只增不改
.claude/skills/           agent 自动发现的风格指南
```

Cortex 尊重 mtime：文件是你手改的，冲突时你赢。`twin-seed/identity.md` 出厂就是一份待填的
模板，`twin-seed/people/core/` 里的两个人是虚构示例——用之前删掉。

## 安装

**依赖**：Apple Silicon 的 macOS · Python 3.11+ · [whisper.cpp](https://github.com/ggml-org/whisper.cpp)
（`brew install whisper-cpp`）· [Claude Code CLI](https://claude.com/claude-code) ·
一个 OpenAI API key。

```bash
git clone https://github.com/MRziyi/Constellation-Server.git
cd Constellation-Server

cp .env.example .env       # 然后填进你自己的 key
$EDITOR .env

./scripts/install.sh       # 建 venv、播种 Twin、装 launchd、启动
```

`install.sh --twin-only` 只播种 Twin；`--skip-launchd` 只装代码不做守护进程。若要让眼镜从
网络而不是回环地址访问 Cortex，运行安装脚本前把 `CORTEX_HOST` 设成眼镜够得着的地址。

日常运维：

```bash
./scripts/status.sh        # launchd 作业、端口、健康状态、眼镜连接
./scripts/restart.sh cortex
./scripts/logs.sh
./scripts/dev-local.sh     # 本地演示，不需要眼镜和中继
```

然后去填 `~/constellation/twin/identity.md`——系统的上限就是这个文件的质量。

### macOS 权限

AppleScript adapter 需要 TCC 授权（邮件、日历、提醒、备忘录、信息、Safari 的「自动化」权限；
读 `chat.db` 需要「完全磁盘访问」）。`cortex/cortex/tcc_check.py` 会在启动时报告缺哪些。在无
显示器的机器上授这些权限相当别扭——那个问题记录在
[DEPLOYMENT-mac-mini-migration.md](https://github.com/MRziyi/Constellation/blob/main/docs/server/DEPLOYMENT-mac-mini-migration.md)。

## 配置

密钥放 `.env`（已被 git 忽略）。行为通过环境变量调，完整清单和默认值见
[`.env.example`](.env.example)。最可能动的几个：

| 变量 | 用途 |
|---|---|
| `OPENAI_API_KEY` | 必填——router、分类器、视觉 |
| `GROQ_API_KEY` | 可选——延迟敏感调用走的快速推理 |
| `CORTEX_ROUTER_MODEL` · `CORTEX_CLASSIFIER_MODEL` | 各阶段的模型选择 |
| `CORTEX_AGENT_MODEL` · `_FAST` · `_DEEP` · `CORTEX_AGENT_EFFORT` | agent 路径的模型档位 |
| `CORTEX_SDK_MAX_BUDGET_USD` | 单次 agent 运行的硬性花费上限 |
| `CONSTELLATION_INSIGHT_ENGINE` | 开启主动浮现 |
| `CONSTELLATION_FACE_MODEL` · `_THRESHOLD` · `_DET_SIZE` | 人脸识别调参 |
| `WHISPER_CLI` · `WHISPER_SERVER` | 本地语音转写的路径 |

## 测试

`test-harness/` 通过真实的 WebSocket 接口驱动 Cortex——这些是打真实服务的端到端流程，不是
单元测试。不主动要求就不会真发出去：

```bash
./scripts/start.sh
python test-harness/full_loop.py
TEST_RECIPIENT=you@example.com python test-harness/mail_flow.py   # 干跑
TEST_RECIPIENT=you@example.com python test-harness/mail_flow.py --send-for-real
```

## 已知局限

- **结构上就是单用户。** 作者的名字在系统 prompt 和 Twin 路径里；做多租户是一次真正的重构。
- **只支持 macOS**，且依赖 AppleScript 的自动化接口——苹果随时可能改。
- **没有做隐私加固。** 任务内容会送到云端模型。Twin 留在磁盘上、人脸识别在端上，但这不是 privacy-by-design 的系统。
- **没有单元测试意义上的测试套件**——正确性靠端到端流程和日常使用来验。

## 相关仓库

[Constellation](https://github.com/MRziyi/Constellation)（设计与架构）·
[Constellation-Glass](https://github.com/MRziyi/Constellation-Glasses)（眼镜端客户端）

## 许可

[Apache License 2.0](LICENSE)。
