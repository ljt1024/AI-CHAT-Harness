# AgentForge

在 **AI-CHAT 2.1.1** 前端基础上演进的智能体平台：保留 **React + Vite** 前端，用 **Python FastAPI Harness** 替换了原来的 Node/Express 后端，并加入 Docker 沙箱、MongoDB 持久化、技能与 MCP 扩展、人工审批和产物交付。

> **当前状态**：T0–T12 已完成并通过实机验收（真实 Docker、真实 MongoDB、真实模型浏览器验证）；前端智能体体验已重构为连贯的流式展示——思考 / 行动 / 观察活动条、分块稳定的 Markdown、输入框上方的人工审批卡、带防遮挡策略的产物预览。
> 原 `end/` 后端仅作参考保留，**其配置与接口不适用于 Harness**。验收记录见 [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md)，待办见 [NOTES.md](NOTES.md)。

## 架构

| 层 | 技术 | 说明 |
| --- | --- | --- |
| 前端 | React 18 + TypeScript + Vite | 端口 `5174`，`/api` 与 `/ws` 代理到 Harness |
| 运行时 | Python 3.12 + FastAPI（`harness/src/agentforge`） | 单进程、单 worker；端口默认 `8000`，OpenAPI 在 `/docs` |
| 模型 | DeepSeek 流式网关（`gateway/`） | 支持增量思考内容与工具调用流 |
| 持久化 | MongoDB（`docker-compose.yml`） | 会话、追踪、检查点、审批、技能、产物（GridFS） |
| 执行 | Docker 沙箱（`sandbox/`） | 非 root、默认无网络、超时即销毁；带预热池与快照 |

## 快速开始

### 1. 依赖

需要 **Node.js >= 22.13**、**Python >= 3.12**、**uv**、**Docker**（用于 MongoDB 与沙箱）。

```bash
npm ci
cd harness && uv sync
```

### 2. 启动 MongoDB

```bash
docker compose up -d mongo      # 127.0.0.1:27017，库名 agentforge
```

### 3. 配置环境变量

复制 `.env.example` 为 `.env`（**不要提交**，密钥只留在服务端）：

```env
DEEPSEEK_API_KEY=你的密钥
MONGODB_URI=mongodb://localhost:27017
AGENTFORGE_DB=agentforge
AGENTFORGE_WORKSPACE=.agentforge/workspaces
MCP_SERVER_URL=http://localhost:9000/mcp
```

### 4. 启动后端

```bash
cd harness
uv run uvicorn agentforge.server:app --host 127.0.0.1 --port 8000
```

只能使用**一个 Uvicorn worker**：会话锁、审批 future 与 SSE 重放都在进程内。没有 MongoDB 时健康检查返回 `degraded`；没有模型密钥时运行返回 `503`。

### 5. 启动前端

```bash
npm run dev        # http://localhost:5174/ai
```

端口冲突时换端口并指定后端目标：

```bash
cd harness && uv run uvicorn agentforge.server:app --host 127.0.0.1 --port 8016
AGENTFORGE_API_TARGET=http://127.0.0.1:8016 npm run dev -- --port 5186
```

## 核心能力

### 智能体循环：思考 → 行动 → 观察

```text
用户输入 → 读取会话记忆 → 思考 → 选择工具 → 执行工具 → 观察结果
                              ↑                         │
                              └─────────────────────────┘
                         → 输出最终回答 → 提交成功轮次记忆
```

循环带停止条件与并发保护：迭代上限、无进展检测、取消、失败恢复与超时；长上下文按摘要 + 近期消息压缩，并可从检查点恢复。执行轨迹来自真实事件，不臆造模型思考。

### 工具

| 类别 | 工具 |
| --- | --- |
| 文件 | `read_file`、`write_file`、`grep`、`glob`（限定在会话工作区，阻止凭据路径与 shell 语法） |
| 命令 | `run_command`（Docker 沙箱内执行，超时与 PID 受限） |
| 技能 | `list_skills`、`load_skill`、`install_skill`、`delete_skill` |
| MCP | 通过 Streamable HTTP 注册外部服务，工具按命名空间暴露 |

### Docker 沙箱

- 非 root 运行、只读根分区、仅工作区可写、默认禁用网络、不继承宿主环境变量（只传 `PATH`/`LANG`/`HOME`）。
- 预热池按会话归属隔离，空闲回收；工作区快照以 tar 存入 GridFS，恢复前校验归属、完整性与归档路径。
- 沙箱失败**不会**回退到宿主执行。离线命令与已安装技能脚本默认自动执行；宿主命令、技能安装、MCP 调用仍需人工确认（见 [ADR-0013](docs/adr/0013-autonomous-docker-commands.md)）。
- `POST /api/sandbox/exec` 把沙箱开放给宿主侧工具：一次性容器里放入文件并执行命令，不经模型回合，复用同一套隔离参数。

#### 在沙箱里跑宿主侧脚本

宿主上的技能脚本若直接执行，会以当前用户权限运行。`scripts/skill-run.py` 把这类脚本送进沙箱：

```bash
scripts/skill-run.py ~/.workbuddy/skills/<skill>          # 自动识别 run.py / main.sh
scripts/skill-run.py <skill_dir> python run.py --flag      # 显式指定命令
scripts/audit-skills.sh [dir]                              # 安装前静态审计（只读，不执行）
```

实测结果：`cwd=/workspace`、网络阻断、根分区只读、以宿主 uid 运行。容器隔离能限制**后果**，但挡不住提示注入——指令文档必须被读入上下文才有效，所以安装前仍需人工过一遍。

### 技能与 MCP

- 技能可从**公开 GitHub**（按 commit 固定）、**沙箱工作区**或**显式内联**导入；导入前校验、导入后按 SHA-256 同步进沙箱，`SKILL.md` 注入提示词。
- 单文件上限 1 MB、整包 5 MB、100 个文件、`SKILL.md` 24 KB；安装过程不执行代码、不装依赖。
- MCP 以无凭据环境启动并绑定独立工作区；外部调用一律需要审批。

### 人工审批

需要确认的操作会挂起执行，前端在**输入框上方**展示审批卡（可滚动查看参数、允许 / 拒绝 / 停止），审批结果按轮次折叠进会话记录，便于回溯。超时一律拒绝（见 [ADR-0008](docs/adr/0008-sandbox-runtime-approval-ui.md)）。

### 产物与预览

`write_file` 产出的 PDF / Excel / PPT / HTML / PNG 会连同本地 CSS、JS、图片打包为不可变产物存储，可下载、可在应用内预览（HTML 预览为净化环境，不执行脚本）。单个 HTML 2 MB、每轮 20 个条目（见 [ADR-0012](docs/adr/0012-workspace-writes-and-html-delivery.md)）。

### 前端流式体验

- **思考 / 行动 / 观察活动条**：运行中叙述当前阶段（正在思考 → 正在调用 *工具* → 正在生成回答 → 已完成 · N 步 · X 秒），步骤以阶段徽章 + 工具名单行呈现，完成后自动折叠。
- **稳定的流式渲染**：已完成的段落按块 memo 渲染，未闭合段落以纯文本增长并带光标，按 ~30fps 合帧；只在用户停留在底部时跟随滚动。
- **产物预览**：≥1101px 时自动打开；窄视口改为手动点开，避免全屏预览遮住整段会话。
- 中英文双语，明暗主题，Markdown 与代码复制。

### 人工介入（HITL）

介入点不再散落在源码里，统一由 [`config/intervention.yaml`](config/intervention.yaml) 声明，代码里各工具的级别退化为兜底。两类介入共用同一套挂起通道（`asyncio.Future` + SSE 事件 + `/api/approvals/{id}`），但语义相反：

| | 批准型 `approve` | 信息型 `input` |
|---|---|---|
| 谁触发 | 系统策略（危险命令、越界写入、装技能） | 模型自己，通过 `ask_user` 工具 |
| 问什么 | 这个动作能不能做 | 这个参数我不知道，你知道吗 |
| 超时 | **拒绝**（fail-closed） | **跳过**（fail-open，人不在也让任务继续） |

- 规则细化到**参数级**：`run_command` 的 `ls` 与 `rm -rf` 走不同规则，`write_file` 命中 `.env` / `*secret*` 直接拒绝。
- 策略规则只能**收紧**工具的默认级别，不能放松——否则"把 ls 列为常规"会连带放开更严格的宿主机 shell。
- 信息型自带闸门：每轮最多问 `model_requests.max_per_run` 次，问题命中 `forbid_patterns`（密钥类）时在到达用户之前就被拒绝。
- `GET /api/interventions/policy` 返回解析后的完整策略，回答"系统到底在哪些地方拦我"。
- 交互卡片渲染在输入框上方；交互卡不进上下文，只在消息里留一行可展开的审计轨迹（已批准 / 已拒绝 / 已回复 / 已跳过）。

## 项目结构

```text
AI-CHAT-Harness/
├── src/                  # 前端：app / features(chat, agents) / shared / infrastructure
├── config/               # intervention.yaml：人工介入策略（唯一真理源）
├── harness/
│   ├── src/agentforge/   # FastAPI 运行时：api、loop、gateway、middleware、sandbox、
│   │                     #   tools、skills、capabilities、artifacts、permissions、
│   │                     #   policy、store
│   └── tests/            # unit + e2e（真实 Docker / MongoDB 用例会自动跳过缺口环境）
├── docs/                 # IMPLEMENTATION、CAPABILITIES 与 ADR 0001-0013
├── mcp-server/           # 独立 MCP 示例服务
├── examples/skills/      # 示例技能包
├── scripts/              # i18n 检查与浏览器验证脚本
└── end/                  # 原始 AI-CHAT Node 后端（参考保留，不再驱动本项目）
```

## 验证

```bash
npx tsc --noEmit                 # TypeScript 检查
npm run check:i18n               # 中英文资源一致性
npm run lint                     # 前端 lint（覆盖迁移的基础设施与智能体 API）
npm run build                    # 前端生产构建
cd harness
uv run ruff check src tests     # 后端 lint
uv run pytest tests -q          # 后端全量测试（含真实 Docker / MongoDB 用例）
```

Docker 与 MongoDB 用例在缺少外部依赖时会显式跳过；浏览器验证脚本位于 `scripts/`，部分会调用真实模型并产生费用。

## 边界与限制

- 面向**可信单用户、本地回环**的开发运行时，**尚未实现认证与多租户隔离**；对外暴露前必须在部署层加访问控制（下一阶段 M2A / T15.A1-A4）。
- 受限工具不等于 Docker 安全边界，真正隔离由沙箱提供。
- 尚未提供通用联网搜索；命令执行只在**离线** Docker 沙箱内进行，不能访问外网。
- 产物预览为净化环境，交互脚本只在下载后的文件中运行。

## 文档索引

- [实现与验收记录](docs/IMPLEMENTATION.md) · [待办与决策](NOTES.md)
- [技能与 MCP 能力说明](docs/CAPABILITIES.md)
- ADR：[0001 基础与运行时](docs/adr/0001-base-and-runtime.md) · [0002 SSE 兼容](docs/adr/0002-sse-compatibility.md) · [0003 运行时与检查点](docs/adr/0003-runtime-and-checkpoints.md) · [0004 中间件与审批](docs/adr/0004-middleware-and-approvals.md) · [0005 上下文压缩](docs/adr/0005-context-compaction.md) · [0006 Docker 沙箱](docs/adr/0006-docker-sandbox.md) · [0007 预热池与快照](docs/adr/0007-sandbox-pool-and-snapshots.md) · [0008 沙箱审批 UI](docs/adr/0008-sandbox-runtime-approval-ui.md) · [0009 技能与 MCP](docs/adr/0009-skills-and-mcp-runtime.md) · [0010 技能安装](docs/adr/0010-agent-skill-installation.md) · [0011 模型输出恢复](docs/adr/0011-model-output-recovery.md) · [0012 工作区写入与 HTML 交付](docs/adr/0012-workspace-writes-and-html-delivery.md) · [0013 沙箱自主命令](docs/adr/0013-autonomous-docker-commands.md)

## 常见问题

- **健康检查 `degraded`**：MongoDB 未启动或 `MONGODB_URI` 不对。
- **运行返回 503**：未配置 `DEEPSEEK_API_KEY`，或密钥无对应模型权限。
- **接口 404 / 无流式**：确认前端走的是 Vite 代理（`/api` → 后端），且反向代理未缓冲 SSE、读取超时大于 300 秒。
- **沙箱相关失败**：检查 Docker 是否运行、镜像是否可用；沙箱失败不会回退宿主。
- **Docker Hub 拉取失败**：可用 `public.ecr.aws/docker/library/{mongo:7,python:3.12-slim}` 后本地打 tag。

---

# 附录：原始 AI-CHAT 说明（旧后端 `end/`，仅供参考）

以下为 AI-CHAT 2.1.1 的原始使用说明，描述的是已停止驱动本项目的 Node/Express + LangGraph 后端，其中的环境变量、端口与工具**不适用于 Harness**。

[在线体验](http://118.31.167.0:3000/ai) · [后端接口与配置](./end/README.md) · [工程结构规范](./ARCHITECTURE.md)

> 在线环境可能与当前分支版本不同，本文以仓库代码为准。

### 功能概览（旧后端）

| 功能 | 旧后端能力 |
| --- | --- |
| 多模型对话 | DeepSeek、通义千问及自定义 OpenAI 兼容模型；按文本、多模态、文生图、自定义分类 |
| 深度思考 | 流式展示模型返回的思考内容，显示耗时，支持手动折叠，完成后默认保持展开 |
| ReAct 智能体 | LangGraph 编排“思考 → 行动 → 观察 → 再思考”，逐步流式展示工具调用与结果 |
| 多智能体协作 | 主智能体按需委派规划、研究、写作角色 |
| 会话记忆 | LangGraph SQLite checkpoint、成功轮次持久化、历史摘要与近期上下文组合 |
| 工具 | 计算、时间、中国天气，以及 PDF、Excel、PPT、HTML 文件生成 |
| 文件预览 | 左侧执行、右侧预览；生成草稿同步展示，完成后可下载、全屏查看和从历史重新打开 |
| 图片理解 | DeepSeek 原生 Files API，以及其他视觉模型的图片 URL 输入 |
| 文生图 | Qwen-Image-2.0，生成图片后预览与下载 |
| 会话管理 | 浏览器本地历史、重命名、置顶、删除、JSON 导入导出、重新生成 |
| 交互与国际化 | 中英文、明暗主题、Markdown、代码复制、截图分享、移动端适配 |

### 快速开始

#### 1. 环境与依赖

需要 **Node.js >= 22.13.0** 和 npm；后端的 SQLite checkpoint 依赖此运行环境。在项目根目录执行：

```bash
npm ci
npm ci --prefix end
```

#### 2. 配置环境变量

在根目录创建 `.env.development`：

```env
VITE_CHAT_BASE_URL=http://localhost:3001/api/chat/completions
```

在 `end/.env` 配置后端，至少填写一个实际使用的模型供应商密钥：

```env
PORT=3001
DEEPSEEK_API_KEY=替换为你的DeepSeek密钥
# 使用通义千问或文生图时填写
# DASHSCOPE_API_KEY=替换为你的阿里云百炼密钥
# 使用天气工具时填写高德Web服务类型的Key
# AMAP_API_KEY=替换为你的高德密钥
# 开启自定义模型管理时设置，并在配置界面输入相同令牌
# MODEL_CONFIG_TOKEN=替换为你生成的管理令牌
```

未配置密钥的内置供应商模型会显示为未启用。已配置密钥不代表账号一定具有对应模型的调用权限。

密钥只放在后端环境中，不能写入 `VITE_*` 变量；这些前端变量会进入浏览器构建产物。环境文件已由 `.gitignore` 排除，请自行创建。

#### 3. 启动前后端

终端一，启动后端（默认端口 `3001`，支持代码变更自动重启）：

```bash
npm run dev --prefix end
```

终端二，启动前端（默认端口 `5174`）：

```bash
npm run dev
```

访问 **http://localhost:5174/ai**。前端端口被占用时可以指定新端口：

```bash
npm run dev -- --host 127.0.0.1 --port 5175
```

前端直接请求 `VITE_CHAT_BASE_URL`，当前 Vite 配置没有 API 代理。模型、上传、智能体等接口会基于该地址推导，因此应填写本项目后端的完整聊天接口地址，而非供应商 API 地址。

### 模型与使用方式

#### 内置模型

| 模型 ID | 用途与区别 |
| --- | --- |
| `deepseek-flash` | 流式对话、深度思考、图片理解、原生 Files API、智能体工具调用 |
| `deepseek-chat` / `deepseek-reasoner` | 保留的对话模型 ID；项目中未标记为支持图片理解 |
| `qwen-turbo` | 文本对话、深度思考、工具调用 |
| `qwen3.5-plus` | 对话、深度思考、图片理解、工具调用 |
| `qwen-max` | 文本对话与工具调用；项目中未启用深度思考 |
| `qwen-image-2.0` | 专用文生图模型，不作为普通聊天或 ReAct 主模型 |

能力标签来自后端模型配置，实际可用性以供应商接口和账号权限为准。“多模态”分类目前指图片理解，不代表音频、视频能力。

#### 自定义模型

在 **切换模型 → 模型配置** 中新增、编辑或删除自定义模型，填写展示名称、上游模型 ID、Base URL 和 API Key，并按实际能力设置图片理解、工具调用选项。

- 当前适配 OpenAI 兼容的 **流式 Chat Completions** 接口，不包含自定义文生图协议。
- 未启用工具调用能力的模型不能进入智能体模式。
- 后端必须设置 `MODEL_CONFIG_TOKEN`；管理请求通过 `x-model-config-token` 校验。未设置时管理接口关闭。
- 配置是服务端共享配置，保存在 `end/local_storage/custom-models.json`，不是每个用户独立的模型列表。
- 模型列表不会返回 API Key；编辑时留空密钥表示保留原值。配置文件中包含密钥，需要随服务端数据妥善保管。

#### 深度思考

选择支持深度思考的模型并打开开关后，发送消息即显示“思考中…”，收到 `reasoning_content` 后实时展示。思考结束后显示耗时，默认保持展开，可点击标题手动折叠；历史消息也默认展开。

耗时按前端收到首个思考片段到首个回答片段的时间计算，不代表供应商内部精确计算时间。旧消息若没有计时数据，不会补造耗时。

#### 图片理解

选择支持图片理解的模型后上传图片，等待上传完成再发送问题。

- **DeepSeek Flash**：通过 DeepSeek 原生 Files API 上传，消息使用 `file_id`，无需配置 OSS。支持 JPEG、PNG、GIF、WebP，普通聊天和智能体模式均可使用。
- **其他支持图片 URL 的视觉模型**：使用 OSS 上传链路，需要配置下文的 OSS 参数。
- 应用默认单文件上限为 **20 MiB**。DeepSeek 原生上传当前只接受图片，不能据此上传 PDF、Word 等文档。
- 移除输入框附件不会删除 DeepSeek 端已上传的文件。

#### 文生图

先选择 **Qwen-Image-2.0**，再输入画面描述。需要配置 `DASHSCOPE_API_KEY`，且密钥地域、接口和模型权限一致。

文生图使用独立 LangGraph 流程调用 LangChain 图片工具，流式返回执行状态，图片生成完毕后展示 PNG，支持预览、全屏和下载。当前为单轮生成，不支持连续图片编辑或像素级流式输出，普通聊天模型不会自动获得文生图能力。

### 智能体模式

在输入区开启智能体模式，使用支持工具调用的模型。主智能体通过 LangChain 消息与工具接口执行任务，由 LangGraph 管理循环和记忆。

```text
用户输入 → 读取会话记忆 → 思考 / 选择工具 → 执行工具 → 观察结果
                              ↑                         │
                              └─────────────────────────┘
                         → 输出最终回答 → 提交成功轮次记忆
```

执行期间通过 SSE 展示步骤、工具参数、观察结果和回答。页面可显示“正在调用天气工具”“已生成并写入文件”等实际执行事件，并支持展开原始参数与结果。执行轨迹来自真实事件；模型思考文本仅在上游返回时展示。

#### 可用工具

| 工具 | 用途 |
| --- | --- |
| `calculate` / `current_time` | 计算与当前时间 |
| `get_weather` | 高德地理编码与天气查询，支持中国城市实时天气、预报 |
| `delegate_planner` / `delegate_researcher` / `delegate_writer` | 委派规划、研究和写作角色 |
| `export_pdf` / `export_excel` / `export_pptx` / `export_html` | 生成文件并返回下载、预览信息 |

例如：“查询杭州天气并给出出行建议”“把这份计划整理成 PDF”“生成一份包含合计的 Excel 预算表”“制作项目介绍 PPT，并同步预览”。

天气工具需要高德 **Web 服务类型 Key**，免费额度及使用范围以高德平台规则为准。研究角色当前不等于联网搜索；项目尚未提供通用网页搜索或任意代码执行工具。

#### 记忆与执行边界

- 使用 `sessionId` 区分会话、`turnId` 标识轮次，SQLite checkpoint 保存在 `end/local_storage/agent-checkpoints.sqlite`。
- 仅成功完成的轮次提交为后续对话记忆；失败、取消或截断不会覆盖已成功的历史。最近一轮重试支持替换该轮记忆。
- 长上下文通过摘要与近期消息组合控制体积；摘要有损且按字符估算，完整成功历史仍保留在存储中。
- 浏览器聊天记录与服务端智能体记忆是两份数据，普通聊天历史不会自动完整同步为智能体记忆。
- 删除浏览器会话不等于删除服务端 checkpoint 或生成文件。
- 当前默认最多 12 次推理迭代，智能体请求总超时 5 分钟；不再设置应用层统一 `2048 token` 输出上限，但仍受供应商限制。

#### 文件生成与同步预览

| 格式 | 实现与预览方式 |
| --- | --- |
| PDF | PDFKit 生成，内置中文字体，支持分页与缩放 |
| Excel | ExcelJS 生成 `.xlsx`，提供表格预览，支持数值与 SUM 等受控能力 |
| PPT | PptxGenJS 生成 `.pptx`，另行生成 PDF 用于预览；并非 Office 原文件的精确转换 |
| HTML | 支持生成过程中的草稿预览，最终页面在受限沙箱中展示 |

左侧继续执行时，右侧根据已收到的内容更新草稿；保存后切换为最终文件预览。预览共享页面顶栏，支持全屏和下载原文件。HTML 预览限制脚本与远程资源，不等同于完整浏览器运行环境；Excel 不支持任意公式执行。

### 环境配置参考

以下变量放在 `end/.env`，按需配置：

| 变量 | 默认值 / 说明 |
| --- | --- |
| `PORT` | `3001` |
| `DEEPSEEK_API_KEY` | DeepSeek 对话、图片理解和原生文件上传 |
| `DASHSCOPE_API_KEY` | 通义千问与文生图，也可使用 `QWEN_API_KEY` |
| `AMAP_API_KEY` | 高德天气工具，也可使用 `GAODE_API_KEY` |
| `MODEL_CONFIG_TOKEN` | 自定义模型管理令牌，未配置则禁用管理接口 |
| `QWEN_IMAGE_ENDPOINT` | 默认阿里云 DashScope 多模态生成接口 |
| `MAX_UPLOAD_FILE_SIZE_BYTES` | `20971520`，即 20 MiB |
| `MAX_FILE_UPLOAD_COUNT` | `10`，后端上传数量上限；当前界面单次选择一个文件 |
| `MAX_FILE_CONTENT_LENGTH` | `12000`，文件文本内容长度上限 |
| `OSS_SIGNED_URL_EXPIRES_SECONDS` | `1800`，OSS 签名 URL 有效秒数 |

图片 URL 上传链路需要：

```env
OSS_REGION=oss-cn-hangzhou
OSS_BUCKET=你的Bucket名称
OSS_ACCESS_KEY_ID=你的AccessKeyId
OSS_ACCESS_KEY_SECRET=你的AccessKeySecret
# 可选：OSS_STS_TOKEN、OSS_ENDPOINT、OSS_SECURE、OSS_PREFIX
```

OSS 参数也支持 `ALI_OSS_*` 别名。仅使用文本对话、DeepSeek 原生图片上传或文件生成时，无需配置 OSS。

### 项目结构

```text
AI-CHAT/
├── src/
│   ├── app/                   # 应用装配、路由、国际化
│   ├── pages/                 # 页面组合
│   ├── features/              # chat、agents 等业务模块
│   ├── shared/                # 共享组件、hooks、类型与资源
│   └── infrastructure/        # 外部服务适配边界
├── end/
│   ├── app.js                 # 后端入口
│   ├── src/
│   │   ├── agents/            # LangGraph、工具、记忆与角色注册
│   │   ├── models/            # LangChain 模型适配
│   │   ├── services/          # 对话、上传、天气、文件生成等服务
│   │   ├── routes/            # HTTP 与 SSE 接口
│   │   ├── config/            # 环境变量、内置与自定义模型
│   │   └── i18n/              # 后端中英文文案
│   ├── assets/fonts/          # PDF 中文字体（字体文件不纳入 Git，见该目录 README）
│   ├── local_storage/         # 运行数据，不纳入 Git
│   └── scripts/               # 后端验证脚本
├── scripts/                   # 国际化检查与浏览器验证脚本
├── ARCHITECTURE.md            # 分层与依赖规范
└── README.md
```

前端中英文资源位于 `src/app/i18n/locales/`，后端资源位于 `end/src/i18n/locales/`。语言默认跟随浏览器，支持用户切换与持久化；用户输入和模型生成内容不会自动翻译。

### 开发与验证

在项目根目录执行：

```bash
npx tsc --noEmit             # TypeScript 检查
npm run check:i18n          # 国际化资源检查
npm test --prefix end      # 后端测试
npm run build              # 前端生产构建，输出 dist/
npm run preview            # 本地预览构建产物，仍需单独运行后端
```

浏览器验证脚本需要 Python 和 Playwright。建议在虚拟环境中安装：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install playwright
python -m playwright install chromium
python scripts/verify_i18n.py --url http://localhost:5174/ai
```

智能体、流式步骤、文件预览、文生图及 DeepSeek 图片理解分别提供 `verify_agents.py`、`verify_step_stream.py`、`verify_preview.py`、`verify_image.py`、`verify_deepseek_vision.py` 等脚本。执行前阅读对应脚本的依赖与模型要求，部分文件验证还需安装 PDF / Excel 解析库。

这些端到端脚本可能调用真实模型并产生费用；`verify_i18n.py` 默认不发送真实模型请求，添加 `--live` 才执行在线对话验证。后端真实记忆验证入口为 `node end/scripts/verifyMemory.js`。

### 部署与数据保留

1. 在根目录创建 `.env.production`，将 `VITE_CHAT_BASE_URL` 设置为用户浏览器可访问的生产聊天接口完整地址，再执行 `npm run build`。前端变量在构建时固化，不能只修改服务器环境而不重新构建。
2. 将 `dist/` 部署到静态服务器，为 `/ai` 等前端路由配置回退到 `index.html`。
3. 部署 `end/`，保留字体资源和依赖锁文件；服务器安装 Node.js 22.13+，执行 `npm ci --omit=dev --prefix end`，配置 `end/.env` 后运行 `npm start --prefix end`。
4. 使用 PM2 等进程管理器时设置正确的后端工作目录与 Node 解释器。当前并发保护位于单进程内，SQLite 部署建议使用单实例。
5. 反向代理 SSE 接口时关闭响应缓冲，将读取超时设为大于 300 秒；HTTPS 页面也应使用 HTTPS API 地址。

升级时保留 `end/.env` 和整个 `end/local_storage/`。其中包含会话 checkpoint、生成文件与上传文件、文件索引、自定义模型配置；删除会导致记忆或历史文件不可用。SQLite 备份应使用一致性快照或停服务备份，避免只复制运行中的主数据库而遗漏 WAL 数据。

当前没有完整的用户登录、多租户数据隔离体系。对外提供服务时，应在部署层配置访问控制；模型管理令牌只保护模型配置操作。

### 常见问题

- **接口 404**：检查 `VITE_CHAT_BASE_URL` 是否指向本项目后端的 `/api/chat/completions`，以及代理是否保留 `/api` 路径。不要填前端端口或供应商地址。
- **请求 400 / 模型不可用**：查看接口返回的具体错误与后端日志，核对模型 ID、供应商权限、工具调用能力和附件格式，不能仅凭已配置 Key 判断模型可用。
- **一次性返回，没有流式效果**：检查上游是否支持流式，以及代理、CDN 是否缓冲 SSE。文生图只流式返回状态，最终图片一次性返回。
- **模型配置无法保存**：检查后端是否设置 `MODEL_CONFIG_TOKEN`，界面填写的令牌是否一致，以及数据目录是否可写。
- **重启后记忆或文件丢失**：确认服务始终使用同一份 `end/local_storage/`，并检查部署是否覆盖了运行数据。
- **更换 API 地址后没有生效**：开发环境需重启 Vite，生产环境需重新构建并部署前端。

### 相关文档

- [后端接口与详细配置](./end/README.md)
- [项目分层与扩展规范](./ARCHITECTURE.md)
- [DeepSeek Files API](https://api-docs.deepseek.com/zh-cn/api/create-file)
- [DeepSeek 图片理解](https://api-docs.deepseek.com/zh-cn/guides/vision)
- [LangChain 文档](https://docs.langchain.com/oss/javascript/langchain/overview)
- [LangGraph 文档](https://docs.langchain.com/oss/javascript/langgraph/overview)
