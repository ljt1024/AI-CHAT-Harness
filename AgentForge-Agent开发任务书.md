# AgentForge · AI Agent 开发任务书 v3（AI-CHAT 二开版）

> 本文档是给 AI 编码 Agent 的执行指令，与《多智能体平台-开发计划.md》（§2.5 借鉴矩阵、§2.6 项目基座）配套使用。
> v3 核心变更：**项目基座为用户自研的 [AI-CHAT @2.1.1](https://github.com/ljt1024/AI-CHAT/tree/2.1.1)**——前端全保留，后端换 Python FastAPI Harness，旧 Express/LangGraph 后端（`end/`）**保留不动作参考**。
> 参考仓库：dsh（插件化/ADR/benchmark/Inspector）、ERP-AGENT（中间件栈/MCP/七层沙箱+五态生命周期/HITL/子Agent/Skills/MongoDB）。

---

## 0. 你的角色与总目标

你是全栈工程师，在 AI-CHAT 基座上实现 **AgentForge**：可创建智能体、配置能力（工具/技能/模型/权限/沙箱）、通过 Web 完成任务的多智能体平台。

**基座认知（先读后做）**：
- 前端 `src/`：React 19 + Vite + TS，分层 `app/pages/features/shared/infrastructure`，依赖方向规范见仓库 `ARCHITECTURE.md`（**必须遵守**：features 不得跨层引用、组件 PascalCase、`@/` 别名）
- 前端 `features/agents` 是架构文档明示的稳定扩展边界（types/registry/orchestrator/tools/hooks），智能体创建向导在此扩展
- 旧后端 `end/`（Express + LangGraph）**只读不改**，作为实现参照：
  - `end/src/agents/contextMemory.js` → T7 compaction 的实现参照（分批整理历史摘要）
  - `end/src/agents/memoryStore.js` → T2 checkpoint 的参照
  - `end/src/services/agentService.js` → T6 的并发保护/轮次重试参照
  - `end/src/routes/` → 现有 SSE 协议（sessionId/turnId）的契约来源

**范围分级**：

```
Tier 1 (T0~T9)   基座接入 + Harness 核心 + 中间件栈
Tier 2 (T10~T16) 沙箱池 + 平台 + Skills + 子Agent
Tier 3 (T17~T20) 质量基建 + 打磨上线
```

**明确不做**：市场/竞标拓扑、自研模型、移动端、K8s、迁移 Next.js、修改/删除 `end/` 旧后端、修改 `.workbuddy/`。

## 1. 执行协议

1. 按 Task 顺序推进，完成并通过验收命令才进入下一个
2. **ADR 制度**：架构性选择先写 `docs/adr/NNN-标题.md`（背景/决策/备选/后果，≤10 行）
3. 验收命令必须实际执行且通过
4. 不扩 scope：顺手能加的记 `NOTES.md` Backlog
5. 歧义：选更简单的方案，`NOTES.md` Decisions 区记一行
6. **禁止**：引入 LangChain/LangGraph/CrewAI（Python 端自研薄封装）；密钥进代码或 git；绕过 §4.5 沙箱红线；动 `end/` 目录
7. 每 Task 一次 commit：`T<编号>: <一句话>`
8. 前端改动必须符合基座 `ARCHITECTURE.md` 分层规范与 i18n 规范（中英文资源都要加）

## 2. 技术栈与环境（锁定）

| 项 | 值 |
|---|---|
| 基座 | AI-CHAT @2.1.1 fork，新分支 `agentforge` |
| 前端 | 基座原样：React 19 + Vite + TS + react-router 7 + i18next（**不迁移 Next.js**） |
| 新后端 | Python 3.12+（uv），FastAPI，**API 契约对齐前端 infrastructure 层现有协议**（sessionId/turnId/SSE 事件） |
| 数据库 | MongoDB 7（docker-compose），motor 异步驱动 |
| MCP | FastMCP，工具服务器独立进程 |
| LLM | DeepSeek（OpenAI 兼容），adapter 注册制预留多供应商 |
| 沙箱 | Docker（python:3.12-slim / node:22-slim） |
| 部署 | docker-compose：mongo + mcp-server + harness(:8000) + web(Vite build 静态托管) |

```bash
# .env（gitignore，提供 .env.example）
DEEPSEEK_API_KEY=sk-xxx
MONGODB_URI=mongodb://localhost:27017
AGENTFORGE_DB=agentforge
MCP_SERVER_URL=http://localhost:9000
```

## 3. 目录结构（基座之上增量，按此创建）

```
AI-CHAT/                        # fork 后的仓库根
├── src/                        # 前端（基座原有，按 §5 扩展）
│   ├── features/agents/        # 稳定边界内扩展：向导/编排配置
│   ├── features/chat/          # 对话页增强：HITL 卡片/工具过程/成本
│   └── infrastructure/         # api.ts 指向 :8000 + SSE 事件对齐
├── end/                        # 旧后端，只读参考，禁止修改
├── harness/                    # 新 Python 后端（本任务书主体）
│   ├── pyproject.toml          # uv 管理
│   ├── src/agentforge/
│   │   ├── gateway/            # 模型网关 (base.py + deepseek.py + registry.py)
│   │   ├── loop.py             # 主循环
│   │   ├── context.py          # 上下文管理
│   │   ├── permissions.py      # 三级权限 + HITL
│   │   ├── middleware/         # 7 层中间件栈
│   │   ├── tools/              # 内置工具 (base.py + fs.py + shell.py)
│   │   ├── mcp_client.py       # MCP 客户端
│   │   ├── sandbox/            # pool.py + docker_runner.py + breaker.py
│   │   ├── skills/             # manager.py
│   │   ├── subagent.py         # 子Agent委派
│   │   ├── store.py            # MongoDBStore + checkpoint saver
│   │   ├── evals/              # 基准评测 + HTML 报告
│   │   └── server.py           # FastAPI 入口(静态托管前端 build 产物)
│   └── tests/                  # unit/ + e2e/
├── mcp-server/
│   └── src/main.py             # FastMCP 工具注册
├── docker-compose.yml
├── docs/adr/
└── NOTES.md
```

## 4. 核心接口契约（签名必须一致）

### 4.1 模型网关

```python
class ModelAdapter(Protocol):
    async def chat_stream(self, req: ChatRequest) -> AsyncIterator[Chunk]: ...
    @property
    def pricing(self) -> Pricing: ...

class ModelGateway:
    def register(self, name: str, adapter: ModelAdapter) -> None
    async def chat(self, messages, tools, model_ref) -> AsyncIterator[Chunk]
# Chunk = ContentDelta | ToolCallRequest | UsageReport | Done
# 网络失败按 registry fallback 配置降级
```

### 4.2 中间件栈（7 层洋葱模型，借鉴 ERP-AGENT 9 层精简）

```python
class Middleware(Protocol):
    name: str; enabled: bool
    async def on_turn_start(self, ctx: TurnContext) -> TurnContext: ...
    async def on_tool_call(self, ctx, call: ToolCall) -> ToolCall: ...
    async def on_tool_result(self, ctx, result: ToolResult) -> ToolResult: ...
    async def on_turn_end(self, ctx: TurnContext) -> None: ...
    async def on_error(self, ctx, err: Exception) -> None: ...
# 注册顺序: SandboxHealth → ContextInjection → SkillsSync → ToolsSummarization
#          → SandboxCircuitBreaker → ModelCallLimit → ToolCallLimit
```

### 4.3 工具与 MCP

```python
class Tool(Protocol):
    name: str; description: str; schema: dict
    permission: Literal["auto", "rule", "confirm"]
    source: Literal["builtin", "mcp"]
    async def execute(self, args: dict) -> ToolResult: ...
# ToolResult = {"ok": bool, "output": str(≤4000字符), "error": str|None(含下一步建议)}
# MCP Server (FastMCP) 承载: read_file/write_file/grep/glob/run_command/web_search/web_fetch
# mcp_client 自动把 MCP schema 转换为 OpenAI function-calling 格式
```

### 4.4 主循环

```python
async def run_agent(agent: AgentDefinition, task: str, session_id: str,
                    events: EventBus) -> RunResult:
    """必须实现:
    - 4 类停止: 模型完成 / max_turns(25) / 预算($0.5) / 空转检测(3轮相同→反思注入,再3轮→终止)
    - 中间件栈贯穿每轮
    - compaction: token 估算达窗口 70% → 早期历史总结为状态摘要
      (参照 end/src/agents/contextMemory.js 的分批摘要思路, 但在 Python 侧独立实现)
    - 会话并发保护: 同一 session 同时只允许一个活跃 run
      (参照 end/src/services/agentService.js 的单进程并发保护)
    - L3/HITL: await_approval 事件挂起, POST /api/approvals 恢复
    - 每轮全量持久化 turns + tool_calls, trace_id 贯穿
    - checkpoint: 每子任务/每5轮 → saver 快照, 支持 resume
    """
```

### 4.5 Docker 沙箱（七层防护，红线规则）

```python
SANDBOX_DOCKER_ARGS = [
    "--read-only", "--tmpfs", "/tmp:size=256m", "--memory=512m", "--cpus=1.0",
    "--network=bridge", "--cap-drop=ALL",
    "--security-opt=no-new-privileges", "--security-opt", "seccomp=seccomp.json",
    "--pids-limit=256",
]
# workspace 卷唯一可写; 密钥零进入(环境变量过滤); 单命令 timeout 30s
# 快照: workspace tar → GridFS
```

**五态生命周期**（`sandbox/pool.py`）：

```
WARM(预热池,保持2热容器) → CLAIMED(认领) → CACHED(空闲保活) → DESTROY(销毁)
CREATE(池不足新建); 故障→熔断→重建
# 冷启动≤500ms / 空闲超10分钟回收 / 池补充异步
```

### 4.6 Skills 系统

```python
# 技能 = 文件夹 {SKILL.md, scripts/, deps.txt}
# manager: install / sync(SHA256 增量同步进沙箱) / restore(重建后恢复)
```

### 4.7 子 Agent 委派

```python
async def delegate(self, sub_agent_id: str, handoff: TaskHandoff) -> Artifact: ...
# TaskHandoff: {task_id, goal, acceptance_criteria[], constraints[], artifacts[], context_digest(<500token)}
# schema 强校验, 缺 acceptance_criteria 拒绝委托
# 子Agent = 完整 Harness 实例, 结果以 Artifact 引用回传
```

### 4.8 HTTP/SSE 契约（**必须兼容前端 infrastructure 层现有协议**）

```
# 兼容基座: sessionId 标识会话, turnId 标识用户输入轮次, SSE 事件流式返回
POST   /api/agents                    创建智能体(name/system_prompt/model/tools/skills/sandbox_policy/stop_conditions)
GET    /api/agents                    列表
POST   /api/sessions                  {agent_id, task} → {session_id, turn_id, trace_id}
GET    /api/sessions/{id}/events      SSE: turn_started/tool_call/tool_result/await_approval/middleware_rejected/done/error
POST   /api/approvals/{call_id}       HITL {approved: bool, reason?: str}
GET    /api/traces/{session_id}       轨迹 JSON(含中间件摘要与成本)
GET    /api/sessions/{id}/files/{p}   沙箱文件下载
POST   /api/skills                    安装技能
POST   /api/evals/run                 触发基准评测
WS     /ws/devtools/{session_id}      Inspector 实时事件
```

## 5. 前端（基座扩展，遵守 ARCHITECTURE.md）

| 页面/改造 | 基础 | 内容 |
|---|---|---|
| 对话页增强 | `features/chat` 已有 | 加：工具调用过程实时展示、HITL 批准/拒绝卡片、会话累计成本 |
| 创建智能体向导 | `features/agents` 边界内新增 | 分步表单：模型→工具与技能→system prompt 模板→权限与停止条件→沙箱策略；实时估算单价 |
| 轨迹页 | 新页面 | 按轮次展开（含中间件动作），按 trace_id 回放 |
| DevTools | 新页面 | 运行中会话实时事件流 + 熔断器/沙箱池状态 |

改造约束：SSE 解析与请求统一走 `infrastructure`；不直接在页面组件写编排逻辑；新增文案中英文资源齐全（`check:i18n` 通过）。

## 6. 任务清单（严格按序；每项含验收命令）

### Tier 1 · 基座接入与 Harness 核心

| Task | 内容 | 验收命令 |
|---|---|---|
| T0 | fork AI-CHAT@2.1.1 → 分支 `agentforge`；新增 `harness/` `mcp-server/` `docker-compose.yml`(mongo)；`docs/adr/0001`；`.env.example` | `docker compose up -d mongo && cd harness && uv run pytest tests/unit/test_config.py -q` |
| T0.5 | **前端协议适配**：`infrastructure` api.ts 指向 :8000，SSE 事件对齐（sessionId/turnId 保留）；旧后端 `end/` 零改动；写 `docs/adr/0002`（协议映射表） | `npm run dev` + `npm run dev --prefix end`（仅作对照）+ 手动验证前端能连新后端拿到 SSE 流（用 harness 的最小 stub 端点） |
| T1 | ModelGateway（adapter 注册制 + DeepSeek + usage 计量 + fallback） | `uv run pytest tests/unit/test_gateway.py -q`（mock，覆盖正常/未注册/网络失败 fallback） |
| T2 | MongoDBStore + checkpoint saver（agents/sessions/turns/tool_calls/checkpoints；session 结构含 sessionId/turnId） | `uv run pytest tests/unit/test_store.py -q`（testcontainers 或 mongomock；参照 end/src/agents/memoryStore.js 的 checkpoint 语义写对照测试） |
| T3 | 内置工具四件套 read_file/write_file/grep/glob | `uv run pytest tests/unit/test_tools_fs.py -q` |
| T4 | **MCP Server（FastMCP）**承载工具 + mcp_client 接入 + schema→function-calling 转换 | `uv run pytest tests/unit/test_mcp.py -q`（起真 server 进程） |
| T5 | 受限 run_command（白名单/超时/逃逸拦截/密钥过滤） | `uv run pytest tests/unit/test_tools_shell.py -q`（含 rm/逃逸/读密钥 三个必须失败用例） |
| T6 | 主循环 v1：4 类停止 + 空转检测 + **会话并发保护**（参照 agentService.js） + trace 落库 | `uv run pytest tests/unit/test_loop.py -q`（四条停止路径 + 并发同 session 拒绝） |
| T7 | 上下文管理：输出截断 + 70% compaction（参照 contextMemory.js） | `uv run pytest tests/unit/test_context.py -q` |
| T8 | **中间件栈（7 层，可独立开关）** | `uv run pytest tests/unit/test_middleware.py -q`（单层 + 全栈顺序 + 开关） |
| T9 | 三级权限 + **HITL 挂起/恢复/拒绝** | `uv run pytest tests/unit/test_hitl.py -q`（批准/拒绝/超时默认拒绝） |

### Tier 2 · 沙箱池与平台

| Task | 内容 | 验收命令 |
|---|---|---|
| T10 | **Docker 七层防护 runner**（密钥零进入，workspace 唯一可写） | `uv run pytest tests/e2e/test_sandbox.py -q`（需 Docker；fork 炸弹/写根目录/读密钥 三必须失败用例） |
| T11 | **沙箱池五态生命周期 + 预热池 + 三态熔断** | `uv run pytest tests/e2e/test_pool.py -q`（池取用/冷启动≤500ms/空闲回收/熔断恢复） |
| T12 | 沙箱快照（tar→GridFS）+ 文件上传下载 API（打通前端已有文件预览） | `uv run pytest tests/e2e/test_snapshot.py -q` |
| T13 | FastAPI 全端点 + SSE + **静态托管 Vite build 产物** | `uv run pytest tests/e2e/test_server.py -q` + curl 冒烟全部端点 |
| T14 | 前端扩展（§5 四项：对话页增强/向导/轨迹页/DevTools），走 features/agents 稳定边界 | `npm run build && npm run check:i18n` + Playwright：创建智能体→跑一次任务→HITL 卡片出现→轨迹可见 |
| T15 | **Skills 系统**（安装/SHA256 增量同步/恢复） | `uv run pytest tests/unit/test_skills.py -q`（含增量同步断言） |
| T16 | **子 Agent 委派**（TaskHandoff 校验 + 独立 Harness 实例 + Artifact 回传） | `uv run pytest tests/e2e/test_subagent.py -q`（含缺 acceptance_criteria 被拒） |

### Tier 2 补充 · 用户鉴权与隔离（T15.A，待实施）

对应《多智能体平台-开发计划.md》阶段 2.1 / M2A，作为当前 Skills/MCP 接入后的下一平台安全里程碑。执行顺序为 T15 → T15.A1~T15.A4 → T16；保留旧任务编号，不回退已完成模块。未通过前仅允许可信本机单用户开发，禁止多人开放。首版不含公开注册、团队/RBAC、SSO 和资源共享。

下列路径和命令是**计划交付项**，尚未实现，不能据此宣称已支持鉴权或用户隔离。`uv run` 命令在 `harness/` 下运行。

| Task | 内容 | 计划验收命令 |
|---|---|---|
| T15.A1 | 管理员创建账号、成熟库密码哈希、服务端可撤销 Cookie 会话；登录/退出/当前用户、过期与限流、CSRF/Origin/CORS；前端登录和失效处理 | `uv run pytest tests/unit/test_auth.py tests/e2e/test_auth_api.py -q`；`npm run build` + `npm run check:i18n` |
| T15.A2 | 服务端身份贯穿存储与运行；所有资源操作/引用按用户授权；安装/加载 Skills、发现/调用 MCP、会话/Trace/检查点/审批/SSE/文件全部纳入；匿名模型调用关闭 | `uv run pytest tests/e2e/test_user_isolation.py -q`（双用户 CRUD、伪造 owner、跨用户引用/执行/审批/订阅/恢复/下载及同内容技能独立安装） |
| T15.A3 | 用户/会话级工作区、运行锁、租约/快照、并发限制；MCP 凭证加密与脱敏；维护窗口显式归属迁移、dry-run、幂等、备份恢复和回滚 | `uv run pytest tests/e2e/test_tenant_sandbox.py tests/unit/test_mcp_secrets.py tests/e2e/test_owner_migration.py -q`（需真实 Docker/MongoDB；迁移在备份副本验证） |
| T15.A4 | 双账号前端状态分区与退出清理、完整登录→安装→加载→MCP→审批流程、越权回归；补齐安全检查与部署说明 | `uv run pytest tests/unit tests/e2e -q`；`uv run --with playwright python ../scripts/verify_user_isolation.py`（运行中的测试服务、两独立浏览器登录态）；前端 build/lint/i18n 全通过 |

实施前先补充鉴权与数据归属 ADR，明确接口、索引、Cookie/CSRF 策略、退出/在途执行策略和迁移回滚流程；身份不得由模型或客户端指定。按顺序完成身份→存储/API/工具授权→沙箱及迁移→前端与端到端验收，禁止只做登录页而保留匿名执行入口。详细约束和发布 Gate 以阶段 2.1 为准。

### Tier 3 · 质量与上线

| Task | 内容 | 验收命令 |
|---|---|---|
| T17 | **基准评测基建**：20 固定任务（修bug/写函数/调研各1/3）+ runner + HTML 报告 | `uv run python -m agentforge.evals.run --suite smoke --report out/report.html` |
| T18 | Inspector 打磨：实时事件 + 熔断/池状态 | 手动：跑任务时 DevTools 实时滚动全部事件 |
| T19 | 安全自查脚本（密钥泄漏扫描/白名单默认 deny/schema 必填） | `uv run python -m agentforge.security_check` 全绿 |
| T20 | docker-compose 一键全栈 + README + 端到端冒烟（含旧后端退役前的双跑对照说明） | `docker compose up -d && uv run python scripts/smoke.py` |

## 7. 完成定义（Definition of Done）

- [ ] T0~T20 验收命令实际执行通过（T10~T12、T20 需 Linux + Docker）
- [ ] 多人发布前，T15.A1~T15.A4 / M2A 全部验收通过，旧数据显式归属迁移与回滚演练完成；未完成不得声明支持多用户隔离
- [ ] 前端 `end/` 目录 diff 为零（旧后端未被动过）
- [ ] `grep -r "DEEPSEEK_API_KEY" harness/ --include="*.py"` 仅环境变量读取处
- [ ] 前端改动符合 ARCHITECTURE.md（lint + check:i18n 通过）
- [ ] 所有架构决策有 ADR；Decisions/Backlog 在 NOTES.md 完整
- [ ] 基准集 20 任务跑通且 HTML 报告生成

---

*给人类用户的备注：开工前准备 ① fork AI-CHAT 并建 `agentforge` 分支 ② DeepSeek API Key ③ Docker 环境（Tier 2 起）。把本文档 + 计划文档一起交给编码 Agent，指示"从 T0 开始按任务书执行，每完成一个 Tier 暂停汇报一次"。T0.5 是本版关键任务——协议适配决定前端复用成败，建议人工复核 ADR-0002 的协议映射表。*
