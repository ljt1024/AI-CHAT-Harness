# AgentForge

**在线体验**：<https://124.221.253.132/> —— 打开即用，无需安装。未登录可先跑 4 轮对话（访客模式下上传文件等能力不可用）；用邮箱验证码登录，第一次验证即自动注册。演示实例，数据可能随时清理，不要放真实敏感内容。

在 **AI-CHAT 2.1.1** 前端基础上演进的智能体平台：保留 **React + Vite** 前端，用 **Python FastAPI Harness** 替换了原来的 Node/Express 后端，并加入 Docker 沙箱、MongoDB 持久化、技能与 MCP 扩展、人工审批和产物交付。

> **当前状态**：T0–T13、T15 与 T15.A1/A2 已完成并通过实机验收（真实 Docker、真实 MongoDB、真实模型浏览器验证）；T14 前端只差创建向导、独立轨迹页与 DevTools；T15.A3 已完成沙箱租约归属、每账号配额与 MCP 令牌加密（ADR-0029），剩余按用户目录与迁移演练。沙箱隔离已从「会话」推进到「会话 + 账号」。前端智能体体验是连贯的流式展示——思考 / 行动 / 观察活动条、分块稳定的 Markdown、输入框上方的人工审批卡、带防遮挡策略的产物预览。
> 原 `end/` 后端已于 2026-11 移除（[ADR-0033](docs/adr/0033-the-reference-tree-retires.md)），仓库里只剩一套可运行的 Python 运行时；需要对照旧实现时从移除前的提交取回。验收记录见 [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md)，待办见 [NOTES.md](NOTES.md)。

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
# 上游模型自动重试（ADR-0019）：只重试限流/5xx/连接中断，且只在尚未输出任何内容前重试
MODEL_RETRIES=2
MODEL_RETRY_BACKOFF=1
MODEL_RETRY_MAX_BACKOFF=8
MONGODB_URI=mongodb://localhost:27017
AGENTFORGE_DB=agentforge
AGENTFORGE_WORKSPACE=.agentforge/workspaces
MCP_SERVER_URL=http://localhost:9000/mcp
# 管理员账号：启动时创建第一个用户名/密码账号（不在网页上自助创建）
ADMIN_USER=admin
ADMIN_PASSWORD=一个至少 10 位的密码
# 邮箱注册/登录（ADR-0018）：完整配置项见 .env.example。
# 默认 console 通道只把验证码写进日志；对外部署必须改成 smtp 并填好主机与账号，
# 否则发码接口返回 503；MAIL_DEBUG_ECHO 永远不要在生产打开。
MAIL_PROVIDER=console
SMTP_HOST=
SMTP_FROM=
# 收件箱里显示的名字；QQ/163 要求 SMTP_FROM 与账号一致，能改的只有这个名字
SMTP_FROM_NAME=
MAIL_DEBUG_ECHO=false
# 关闭后新地址不能自助注册，只有已有账号能登录
SIGNUP_OPEN=true
# 访客试聊：不登录可先浏览并真实对话 N 轮（0=关闭，回到全站必须登录）；
# 全站每日匿名轮数上限 + 匿名会话保留时长（小时，到期清理）
GUEST_TURNS=4
GUEST_DAILY_TURNS=200
GUEST_RETENTION_HOURS=72
# 会话文件库配额（ADR-0020）：单文件 / 单会话文件数 / 单账号总字节。
# 反向代理的请求体上限要 ≥ UPLOAD_MAX_BYTES，否则 nginx 先返回自己的 413
UPLOAD_MAX_BYTES=26214400
UPLOAD_MAX_FILES=20
UPLOAD_MAX_BYTES_PER_OWNER=209715200
# 可选：HTTPS 部署时设 true；额外放行的跨域写请求来源（逗号分隔）
COOKIE_SECURE=false
ALLOWED_ORIGINS=
# 可选：日志级别与格式（text/json），MCP 发现与调用超时（秒）
AGENTFORGE_LOG_LEVEL=INFO
AGENTFORGE_LOG_FORMAT=text
MCP_DISCOVER_TIMEOUT=30
MCP_CALL_TIMEOUT=60
# 可选：启动时自动注册的 MCP 服务列表（含令牌，已被 .gitignore 忽略）
MCP_CONFIG=../config/mcp.json
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

### 6. 生产部署（参考拓扑）

以腾讯云轻量 Ubuntu 24.04（2C2G）为例：

- **MongoDB**：`docker run mongo:7`，只绑 `127.0.0.1:27017`，`--wiredTigerCacheSizeGB 0.25` 限内存。应用账号建在 `agentforge` 库里，连接串的认证库是 `authSource=agentforge` 而不是 `admin`——这是最常见的连不上原因。
- **后端**：专用系统用户跑 venv + uvicorn（`127.0.0.1:8100`，单 worker），systemd 单元 `agentforge.service`（`Restart=always`），日志 `/var/log/agentforge/`。用户需在 `docker` 组里才能管理沙箱容器。
- **前端**：`npm run build` 产物放 `/var/www/agentforge`，**整目录一起发**——`dist/pdfjs/` 里是 pdf.js 的 CMap 表、标准字体、wasm 解码器和解析器 worker，只拷 `assets/` 会让中文 PDF 回到白纸（ADR-0022）；nginx 同站监听 `80` / `8099`；`/api`、`/ws` 反代到后端，SSE 必须 `proxy_buffering off`，`/health` 单独代理。**请求体上限要与 `UPLOAD_MAX_BYTES` 对齐**：nginx 默认只收 1 MB，文件库（ADR-0020）的上传会在到后端之前就被 413 挡掉，所以 `/api` 的 location 里需要 `client_max_body_size 26m;`（略大于 25 MB 的 `UPLOAD_MAX_BYTES`，留给 multipart 头部）。修改站点配置时注意四件事。一是 `location ~ \.mjs$ { default_type application/javascript; }` 不能删：pdf.js 的解析器以**模块 worker** 方式启动，nginx 不认识 `.mjs` 就回 `application/octet-stream`，浏览器直接拒绝 `import()`，预览报 `Setting up fake worker failed`（ADR-0023）。二是 worker **不放在 `/assets` 里**，它与 CMap 表同住 `dist/pdfjs/`，URL 形如 `/pdfjs/pdf.worker.min.mjs?v=6.3.289`，**版本号就是缓存键**：内容哈希只跟字节走，而「响应头曾经是错的」不改变字节，修正后的响应于是顶着同一个 URL 发出，老浏览器缓存里那份错的（`immutable`、30 天、连刷新都不回源）永远换不掉——同一个坑踩过两次，第二次上线当晚就复发。三是 `location /pdfjs/` 用 7 天且 `try_files $uri =404`：缺一个 `.bcmap` 必须 404，回落 `index.html` 等于把 HTML 页塞给 pdf.js 解析。四是 `location = /index.html` 要 `no-cache`，入口文档决定加载哪些哈希分块，它一旦被缓存住，发布就到不了任何人。**通用规则**：`/assets` 是 `immutable` 30 天 + 内容哈希命名，**只改响应头不会让已经缓存了旧响应的浏览器回源**（强刷只救一个人），要让修复触达老客户端必须同时换 URL——缓存条目是 `(URL, 响应头, 响应体)`，哈希只保护其中一样。
- **沙箱镜像**：`docker build -t agentforge/python-docs:3.12 harness/docker/sandbox-docs`（国内构建给 pip 换内网源），`.env` 里 `SANDBOX_IMAGE=agentforge/python-docs:3.12`、`SANDBOX_WARM_SIZE=0`（小内存机器不预热）。
- **前端不引用任何外网资源**：`@import` Google Fonts 已移除——样式表在不可达网络下会永远 pending，而 deferred module 脚本要等样式表加载完才执行，结果就是整站白屏且无任何报错。字体走 `:root` 里的系统栈（PingFang SC / Noto Sans SC）。

## 核心能力

### 账号与登录

每个 `/api` 请求（`/api/auth/*`、`/health` 除外）都要先登录。账号有两个入口：启动时由 `ADMIN_USER` / `ADMIN_PASSWORD` 创建的管理员账号（用户名密码，只能这样创建，不存在网页上抢注第一个账号），以及下文的邮箱验证码自助注册。两条路径最终都只发一枚 HttpOnly、SameSite=Lax 的 Cookie；会话本体在 MongoDB 里，登出、过期和撤号都会让它失效。密码用 `hashlib.scrypt` 加盐哈希，登录失败有按账号的限流锁定。

**邮箱注册与登录**（ADR-0018）：登录框默认是「邮箱」页签——填地址、收 6 位验证码即可进入；**第一次验证一个新地址就等于注册**，不需要单独的注册页。账号本体没有密码，验证码是唯一的门，错 5 次即作废、5 分钟过期、单次消费。邮件按 `MAIL_PROVIDER` 选择：`smtp` 用标准库 `smtplib` 直连（`SMTP_TLS=starttls` 对应 587、`ssl` 对应 465、`none` 用于本地收件箱），不新增依赖；`console` 是开发通道，验证码只打进服务端日志，再配 `MAIL_DEBUG_ECHO=true` 才会回显到界面上。发送侧有三道闸：同地址 60 秒冷却（`VERIFY_COOLDOWN`）、每地址每日 `VERIFY_DAILY_PER_ADDRESS` 条、每 IP 每日 `VERIFY_DAILY_PER_IP` 条——验证码接口是公开的，没有这三道闸就成了烧邮件额度的入口。`SIGNUP_OPEN=false` 关闭自助注册（未注册地址直接 403，也不花一条邮件）。已有的用户名密码账号不受影响，可在侧边栏账号菜单里绑定邮箱（验证码发到 `purpose=bind`），绑定时地址被别人占用会明确拒绝。接口与日志里出现的邮箱一律是 `a***@example.com` 掩码。短信端点（ADR-0017）仍在后端保留，但界面不再提供手机号入口。

**访客试聊**（ADR-0015）：不登录也能进界面，并真实对话 `GUEST_TURNS` 轮（默认 4）。访客身份来自独立的 `agentforge_guest` Cookie，归属过滤原样生效——访客之间互相看不见。全站每日匿名轮数由 `GUEST_DAILY_TURNS` 封顶（清 Cookie 换不来新额度）；超过后返回 403，前端弹出登录框，**试聊的会话在登录后原样转到账号名下**（邮箱注册同样接管）。装技能、接 MCP、建智能体、执行沙箱命令等改动安装的操作不向访客开放，未认领的访客会话在 `GUEST_RETENTION_HOURS` 小时后清理。`GUEST_TURNS=0` 恢复"全站必须登录"。

授权模型：资源（智能体、会话、轨迹、产物、技能、MCP 服务）都带 `owner_id`，读取、续跑、审批、订阅 SSE、下载文件全部按调用者过滤，别人的资源返回 404——既不能访问，也无法确认它存在。内置的 `default` 智能体是共享只读的，任何人可以运行但不能占据这个 id。写请求额外校验 Origin（反代场景认 `X-Forwarded-Host`，或用 `ALLOWED_ORIGINS` 显式放行）。

登录墙出现在第一次需要账号的时刻：未登录可直接浏览与试聊，额度用尽或执行受保护操作时弹出登录层（草稿不丢），会话过期也退回弹层而不是整页丢弃；侧边栏底部显示访客剩余轮数或账号与退出。

### 智能体定义与创建向导

一个智能体就是一条存下来的定义：系统提示词、模型、可用的工具、技能、MCP 服务、沙箱策略、每个工具的确认级别，以及停止条件（轮数 / 预算 / 空转）。`/agents` 页面用三步向导把它建出来（基本信息 → 能力 → 边界与确认），列表里可以复制、编辑、删除，或直接「使用」——带着 `?agent=<id>` 回到对话页开一段新会话。

- 对话一旦开始就**不能换智能体**：定义决定了这段会话的工具、模型与限制，服务端本来就拒绝中途改变执行环境，所以有消息时选择器锁定，重开旧会话也会显示它当初用的那一个（前端按会话记 `chat.agent.<sessionId>`）。选中智能体后模型切换器显示"由智能体决定"，因为带 `agentIds` 的请求以定义里的模型为准。
- 「使用」的优先级高于「恢复」：从 `/agents` 点「使用」时，页面正打开的那段旧会话仍然带着自己的智能体记录，但这次点击的意思是**开新会话跑这一个**，所以请求里的 `agentIds` 必须是刚选的这条，而不是旧会话的那条。两者在同一次渲染里竞争同一份状态，谁后写谁赢——这里的顺序由代码显式保证，不靠执行顺序碰运气。
- 内置的 `default` 智能体是共享只读的：所有账号（含访客）都能运行，但没人能改或删；想改造它就「复制」一份。
- 工具清单不硬编码：`GET /api/tools` 直接返回运行时注册表的真实内容（技能工具除外，它们会被自动挂到每个智能体上）。保存时走与创建同一套校验，未注册的模型 provider、不存在的工具名都会当场 422，而不是等第一次运行才失败。
- 访客可以浏览目录、可以运行内置智能体，但建 / 改 / 删一律 401，界面据此弹出登录层。

### 智能体循环：思考 → 行动 → 观察

```text
用户输入 → 读取会话记忆 → 思考 → 选择工具 → 执行工具 → 观察结果
                              ↑                         │
                              └─────────────────────────┘
                         → 输出最终回答 → 提交成功轮次记忆
```

循环带停止条件与并发保护：迭代上限、无进展检测、取消、失败恢复与超时；长上下文按摘要 + 近期消息压缩，并可从检查点恢复。压缩**保留多少原文按 token 预算算**（默认窗口的 1/4）而不是固定条数——"最近三条"在长任务里往往只是一个短回合，而摘要是有损的；尾部再大也不会超过预算，实在放不下才退回到最小保留量。压缩不再因为摘要偏长就把整轮打断——超出预算时按段落 / 句子边界裁剪，模型返回空摘要才报错并提示开新会话。执行轨迹来自真实事件，不臆造模型思考。

### 工具

| 类别 | 工具 |
| --- | --- |
| 文件 | `read_file`、`write_file`、`grep`、`glob`（限定在会话工作区，阻止凭据路径与 shell 语法） |
| 命令 | `run_command`（Docker 沙箱内执行，超时与 PID 受限） |
| 联网 | `web_search`、`web_fetch`（在**宿主进程**里执行，容器仍然断网，见下） |
| 商品 | `search_products`、`get_product`（联盟商品库关键词搜索与详情，用于比价，见下） |
| 技能 | `list_skills`、`load_skill`、`install_skill`、`delete_skill` |
| MCP | 通过 Streamable HTTP 注册外部服务，工具按命名空间暴露 |

#### 联网：搜索与抓取（ADR-0024）

沙箱默认断网，这点没变——它跑的是模型写的代码，给这段代码一张网卡是另一类信任问题。联网因此是**宿主进程的能力**，以两个窄工具暴露给模型：

- `web_search`：按查询语言选引擎（中文走 360，其余走 Bing 的 RSS 输出），另一个作兜底。结果会丢掉与查询词毫无交集的条目——Bing 对部分中文查询会返回“关于中国”的百科页，那种结果看起来像答案，必须挡掉。
- `web_fetch`：抓一个公开网页并转成可读文本，脚本与样式剔除，非文本类型直接拒绝。

两道闸门管住“公开地址”这件事，靠判定而不是假设：URL 与**每一跳重定向**都会重新解析，只放行 `is_global` 为真的地址（回环、内网、链路本地含 169.254.169.254、CGNAT、保留段一次性排除）；只走 http/https，URL 里不许带凭据，`trust_env=False`（服务上的 HTTP 代理不能悄悄成为这些请求的出口），边下边计数封顶 2MB，15 秒超时，只收文本类型。

两个工具都是 `auto` 权限：给一个只读 GET 加确认弹窗，会把“确认”训练成习惯性点击，真正要紧的闸门就没人看了。隔离的会话里它们是唯一不进容器的工具（`api.py` 的 `WEB_TOOL_MAP` 分支），否则模型会“拥有”一个只返回 DNS 失败的工具。

已知取舍：搜索是抓页面而不是调 API，有验证码与改版风险（360 侧做了 1 秒间隔与 5 分钟缓存）；`web_fetch` 理论上能把工作区内容塞进查询串带出去——任何出网路径都有这个性质，记录里能看到 URL，真要收紧改一条策略即可。升级路径是把同一个工具后端换成搜索 API（博查 / Tavily / Serper），解析函数是纯函数，已有测试覆盖。

#### 商品价格：为什么是商品库而不是爬虫（ADR-0027）

比价要的是「关键词 → 商品列表 → 到手价 / 原价 / 销量」。淘宝、京东、拼多多的搜索页都要求登录态、滑块验证和签名参数，爬虫今天能跑明天就失效，跟上的代价是账号被限。所以这两个工具接的是**联盟商品库**（淘客易，Bearer token；`tools/ecommerce.py`）：

- `search_products`：关键词 + 价格区间 + 排序（相关度 / 价格升降 / 销量），返回标题、到手价、原价、券额、月销与商品链接；
- `get_product`：按商品 ID 或淘宝/天猫链接查一条记录，用来确认某一行的报价。

**覆盖范围是这里最重要的取舍**：联盟库只收录带佣金的商品，不是全站索引。热门商品覆盖得好，冷门型号可能查不到——查不到不等于没有在售。工具描述和返回值里都写了这句，因为一个以为自己在看全市场的模型会直接回答「全网最低价」。

凭证走 `TAOKEYI_TOKEN`，未配置时工具不报异常，而是返回一条可执行的说明（让运营去配，不许向对话里的用户索要），模型据此如实说价格未核实。两个工具都是 `auto` 权限：一次只读 GET，改不了任何一侧的数据，加确认只会训练出习惯性点击。

与联网工具同理，它们是**宿主能力**：隔离会话里由 `api.py` 的 `ECOM_TOOL_MAP` 分支直接注册，token 不进容器。换一家商品库（大淘客、多多进宝、OneBound）只需要再写一个字段映射函数，两个工具只认归一化后的记录。

### 会话文件系统（上传 / 下载）

文件属于**会话**，不属于某条消息：一次上传，这段对话的每一轮都能读它；刷新页面、切换模型、失败的回合都不会弄丢（服务端才是唯一真相）。

- 前端：输入框左侧的回形针。上传后文件以 chip 出现，× 删除。**附件意味着需要工具去读**，所以上传会自动切到智能体模式——纯聊天端点没有工具，传上去也会被静默忽略。
- 智能体看到的是 `<workspace>/uploads/<安全文件名>`，系统提示里同时列出文件名、大小与类型，`read_file` / `grep` / `run_command` 都能用。
- 接口：`POST /api/sessions/{sid}/files`（multipart，字段名 `file`）、`GET /api/sessions/{sid}/files`、`DELETE /api/sessions/{sid}/files/{fid}`；下载统一走 `GET /api/files/{fid}/download`，与产物共用一个地址。
- 默认配额（可配）：单文件 25MB、每会话 20 个、每账号 200MB。匿名访客不能上传。
- 反方向仍然可用：智能体生成的文件继续作为产物交付（见下文“产物与预览”），两套都指向同一个 workspace。

安全边界不依赖“用户会传正常文件”这个假设（详见 [ADR-0020](docs/adr/0020-conversation-file-system.md)）：

| 风险 | 处理 |
| --- | --- |
| 文件名是路径 | 文件名只做展示与 `uploads/<label>` 标签；磁盘上用随机名，`O_CREAT\|O_EXCL\|O_NOFOLLOW` + 0600 |
| 伪造类型 | 扩展名白名单（文档/数据/图片/zip），并按魔数校验 PDF、图片、zip 与 Office 容器 |
| 谎报长度 | 按 64KB 分块读、按字节预算截断；中间件先用 `Content-Length` 挡掉超大请求体（Starlette 会在处理函数之前就把表单落到临时盘） |
| 文件名冲突 | 自动改名为 `report-2.csv`，不覆盖已有文件 |
| 越权与串会话 | 行按 `owner_id` 读；`fileId` 必须属于请求里的 `sessionId`，否则与不存在同样是 404 |
| 会话 id 穿越 | `^[A-Za-z0-9_-]{1,64}$`，因为它会成为目录名 |
| 浏览器执行上传内容 | 一律 `attachment` + `nosniff`；HTML 另加 sandbox CSP，页面内从不内联渲染 |

### Docker 沙箱

- 非 root 运行、只读根分区、仅工作区可写、默认禁用网络、不继承宿主环境变量（只传 `PATH`/`LANG`/`HOME`）。
- 预热池按会话归属隔离，租约再绑定到认领它的账号（拿别人的会话 id 认领不到），每账号的并发沙箱数受 `SANDBOX_MAX_PER_OWNER` 限制；空闲回收；工作区快照以 tar 存入 GridFS，恢复前校验归属、完整性与归档路径。
- 容器被回收后**工作区目录保留**：同一会话重建沙箱时挂回同一目录，文件还在。清理发生在三处——**删除会话**立即销毁该会话的容器与工作区（对话都没了，它产生的文件不该留在磁盘上）；进程启动时扫掉上一进程遗留的容器与无人可重建的目录（`SIGKILL` 不会走关闭钩子，这些容器原本会永远跑着）；维护循环每小时回收一次。
- 判断"遗留"靠归属而非状态：容器带 `agentforge.instance` 标签，进程启动时在 `workspaces/instances/` 写一份带 pid 的声明。启动清理只回收**属主进程已不在**的容器和无人可重建的目录，同机另一个还活着的后端、它正在用的容器与目录都不动。正常关闭会撤掉自己的声明。
- 沙箱失败**不会**回退到宿主执行。离线命令与已安装技能脚本默认自动执行；宿主命令、技能安装、以及未声明只读的 MCP 调用仍需人工确认（见 [ADR-0013](docs/adr/0013-autonomous-docker-commands.md)）。
- `POST /api/sandbox/exec` 把沙箱开放给宿主侧工具：一次性容器里放入文件并执行命令，不经模型回合，复用同一套隔离参数。
- 文档生成镜像 `agentforge/python-docs:3.12`（`harness/docker/sandbox-docs/`）：同一个解释器预装 reportlab / openpyxl / python-pptx / python-docx——沙箱没有网络装不了依赖，PDF / PPT / Excel 这类交付物只能镜像自带。构建后用 `SANDBOX_IMAGE` 把它设为默认镜像；集成文件工具（`write_file` 等）在所有含 CPython 的镜像上可用（`PYTHON_SANDBOX_IMAGES`），`node:22-slim` 例外。

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
- MCP 以无凭据环境启动并绑定独立工作区。远程工具**不进沙箱**（走宿主网络、带令牌），所以要不要审批由远端自己声明的注解决定：`readOnlyHint=true` 且非 `destructiveHint=true` 的只读工具直接执行，其余每次都要批准。`config/intervention.yaml` 里可用 `"mcp_*"` 整体收紧、用 `mcp_local__run_command: confirm` 覆盖单个工具；策略 `deny` 优先级最高。本仓库的默认策略文件里就有一条按工具放行的例子——拼多多百亿补贴的目录查询（`mcp_pdd_baiyi_mcp__get_baiyi_items: auto`）：它不声明注解，但只是读一份缓存的商品列表。
- **结果裁剪分两档**：本地工具（shell、文件）单次结果截到 4,000 字符；远程 MCP 工具给到 16,000。远程调用往往一次返回一整份文档（商品列表、JSON 页），按本地那档切会把 JSON 切成半截字符串，模型拿到的是无法解析的碎片。上限由工具自己声明（`ToolResult.limit`），注册表与中间件两层都按它来，所以不会被按小档重复截断；超出 16k 的应该用服务自己的 `page` / 区间参数分页，而不是继续放大这一档。
- MCP 支持可选访问令牌（Bearer header）：连接**魔搭托管 MCP** 时填入服务 URL 与魔搭令牌即可；令牌只存后端，接口返回、日志、错误信息中一律脱敏（托管 URL 的租户段显示为 `****`）。发现超时 30s、调用超时 60s（`MCP_DISCOVER_TIMEOUT` / `MCP_CALL_TIMEOUT`），401/403 会明确提示令牌被拒。已注册的服务**每轮重新发现**（托管服务可能换工具或过期），彼此并发，所以首 token 前的等待取决于最慢的那个而不是它们的总和——串行时四个服务要等 11 秒。
- **注册即启用，运行时自动装配**：MCP 的产品入口是 `/agents` 页的「工具广场」（见下），技能安装面板仍只在 `VITE_SHOW_SANDBOX_UI=true` 时出现。配置好一次后每个会话都能用，不需要逐轮勾选。若某个已保存的智能体显式 pin 了服务列表，pin 优先且服务不可用会让该轮直接失败；仅"自动装配"的服务掉线只记一条 `skipped` 日志、不影响本轮。`MCP_AUTO_ATTACH=false` 可退回只认 pin。
- **可以只关不删**：`PATCH /api/mcp/servers/{id}` 带 `{"enabled": false}` 把服务停用，URL、令牌、工具清单都留着。停用的服务不会自动装配，也就不会每轮多花一次发现往返、多记一条「不可用」；pin 仍然压过开关——点名要用的服务照旧参与，坏掉就报错而不是悄悄少几个工具。
- 也可以直接粘贴平台给出的整段配置（`{"mcpServers": {"fetch": {"type": "streamable_http", "url": "...", "headers": {"Authorization": "Bearer ..."}}}}`）：服务标识取自配置里的键名，`headers` 只接受 `Authorization`，令牌折进同一条记录；同一 URL 重复粘贴是刷新而不是新增，适合令牌轮换。

**工具广场**（`/agents` 页第二个 tab）：目录由运营方维护在 `config/mcp_catalog.json`，启动时播种为共享只读的 `mcp_catalog` 行，每条给出名称、说明、地址、是否需要令牌、去哪申请（`docs`）。仓库自带的这份目录预置了 14 个**公共**服务（高德当前 `enabled: false`：端点握手超时，先留在目录里并注明原因，恢复后再启用）——文档与代码（Context7、DeepWiki、Cloudflare、Microsoft Learn、AWS）、搜索与抓取（Exa、Firecrawl、博查、Tavily）、地图出行（高德、腾讯位置、百度）、企业数据（天眼查）——所以新部署打开页面就有东西可接，运营方按需增删。用户点「添加」只提交自己的令牌——**地址永远来自目录，卡片上没有填 URL 的地方**。这不是界面上的取舍，而是 SSRF 的边界：MCP 调用从宿主进程发出，注册一个 URL 就等于让本机去连那个地址，而本机旁边就是 MongoDB 和云元数据端点。因此：

- 注册默认拒绝回环、RFC1918、链路本地（含 `169.254.169.254`）；`MCP_ALLOW_PRIVATE=true` 才放行，只在本机/内网自建服务时打开。
- `config/mcp.json` 是运营方自己的清单，视为可信，仍可注册 `127.0.0.1:9000`；任何用户在页面上输入的地址不享受这个待遇。
- 目录条目可用 `"private": true` 声明自己是内网服务，这时安装放行——地址仍是运营方给的，不是用户填的。
- **密钥可以放在链接参数里**：高德 `?key=`、百度 `?ak=`、Tavily `?tavilyApiKey=` 这类服务只能这样传，条目写成 `https://mcp.amap.com/mcp?key={token}`，`{token}` 由安装者的令牌填充（`endpoint()` 在唯一出网点替换并转义）。库里存的是模板，密钥只有加密的 `token` 一列，所有人的行地址一致。反过来，写死查询串会被拒绝——分不清那是密钥还是普通参数，而目录文件是要提交到仓库的。
- `"auth": "optional"` 给「不填密钥也能试用、额度有限」的托管服务（Exa、Firecrawl）：卡片标「可选密钥」，不填直接装，此时 `{token}` 参数会被整段去掉，而不是发出一个空值。
- 名称与说明带 `name_en` / `summary_en`：目录是运营数据，不能放进语言文件，英文界面下缺译时回落中文而不是空白。
- **tab 记在地址栏**（`/agents?tab=tools`），不是组件状态。状态只活到组件被卸载为止，而刷新、dev server 重载、渲染崩溃后的重挂载都会把它清掉——表现就是广场闪一下变回智能体列表，且没有任何链接能把人带到广场。后退/前进也跟着走。
- **失败不等于空目录**：请求没成功时页面显示原因和「重试」，不会渲染「管理员还没有配置服务目录」——这两件事在屏幕上必须长得不一样，否则一次 401 会被读成「这个部署什么都没配」。
- **已经画出来的目录不会被一次刷新抹掉**：重挂载后的第一帧直接用上次的答案（模块级缓存 + `sessionStorage`，按账号分键），不是先空一帧再在 effect 里补；目录与「我的服务」各自结算（`Promise.allSettled`），一个装了坏服务的账号不会因为那行发现超时（30s）就丢掉目录；刷新真的返回空时保留现有卡片并说明「下面仍是上一次的结果」。曾经的表现是：列表一闪、点一下消失。见 [ADR-0031](docs/adr/0031-a-shelf-once-seen-is-not-emptied-by-a-refresh.md)。
- **一次掉线不等于把货架收走**：目录本身是公开的（只有安装、停用、删除需要账号），所以「登录态」不再决定整块内容是否渲染。`user` 会在**任何一个 401** 时变成 `null`（过期 Cookie、后端重启换了签名密钥、一次失败的点击），以前这会把整个广场连同已经画出来的卡片一起摘掉，而刷新重新读会话就又回来了——表现正是「刷新就有，点一下就没了」。现在掉线只在货架上加一句登录提示，卡片留在原处；同一账号的内存答案优先于 `sessionStorage` 副本（隐私模式、配额满时后者读不到）；缓存也拒绝把「空目录」记成这个账号的答案。见 [ADR-0032](docs/adr/0032-a-dropped-session-is-a-banner.md)。
- **令牌落库加密**（Fernet，`MCP_SECRET_KEY` 或 `AGENTFORGE_WORKSPACE/.mcp-key`，0600）；升级前写入的明文行在启动时迁移一次。轮换密钥会让旧令牌读不出来并明确报错，而不是静默变成「无令牌」。
- 接口：`GET /api/mcp/catalog`、`POST /api/mcp/catalog/{id}/install`（请求体只有 `{token}`）。**注册是同步的**：安装必须验证凭证，201 立刻返回工具清单；页面负责显示等待与失败原因。
- 手动填写地址与粘贴导入仍在页面底部的「手动添加服务」里——这是唯一需要键入 URL 的路径，也是唯一会被地址闸门拦下的路径（见 [ADR-0029](docs/adr/0029-the-catalogue-owns-the-address.md)）。目录能列什么、密钥放在哪，见 [ADR-0030](docs/adr/0030-a-catalogue-can-list-somebody-elses-services.md)。
- **装上了不等于密钥有效**：百度会用假 `ak` 返回 14 个工具，调用时才报错。页面如实显示发现结果，不做「绿勾即开通」的暗示。另外只支持 Streamable HTTP 与 Bearer/查询参数两种鉴权，纯 OAuth（Notion、Linear、Sentry、飞书、钉钉）和纯 SSE 的服务不收录——卡片点不完，比没有卡片更糟。

**启动文件**：把整份 `mcpServers` 写进 `config/mcp.json`（照 `config/mcp.example.json` 抄），启动时自动注册，不需要任何 HTTP 调用；适合放进编排脚本或在重启后恢复。条目支持 `"enabled": false`（保留配置但停用）。已注册的同 id 条目是刷新（令牌轮换即生效）而不是重复新增；文件里删掉一条**不会**删除已注册的服务——文件是播种，不是唯一真相。该文件含令牌，已进 `.gitignore`，权限按 600 写入。

```bash
# 等价的一次性导入（服务已在本机 9000 端口且要求 demo-token）
curl -s localhost:8000/api/mcp/servers/import -H 'content-type: application/json' \
  -d '{"config":"{\"mcpServers\":{\"local\":{\"type\":\"streamable_http\",\"url\":\"http://127.0.0.1:9000/mcp\",\"headers\":{\"Authorization\":\"Bearer demo-token\"}}}}"}'
```

### 人工审批

需要确认的操作会挂起执行，前端在**输入框上方**展示审批卡（可滚动查看参数、允许 / 拒绝 / 停止），审批结果按轮次折叠进会话记录，便于回溯。超时一律拒绝（见 [ADR-0008](docs/adr/0008-sandbox-runtime-approval-ui.md)）。

审批卡多一个**「本会话都允许」**：放行只作用于当前会话、只按**工具名**记，不记参数，只存在内存里，会话删除或进程重启即失效，落盘不做、跨会话不继承。它对**所有需要确认的工具**都可用——原先只给远程 MCP 工具，代价是一个任务里同一句 `run_command` 被连续批准二十次，人会开始不看内容就点，比多一次确认更危险。策略层的拒绝不受影响：拒绝在查「已记住」之前就判定了，所以被策略禁止的调用不会因为记住而被放行。

### 产物与预览

工作区里可预览的文件——HTML、PDF、Excel、PPT、Word 与图片（png/jpg/gif/webp）——会在每轮结束时打包为不可变产物存储，可下载、可在应用内直接预览：

- **HTML** 连同本地 CSS、JS、图片打包，预览为净化环境，不执行脚本；
- **PDF** 由 pdf.js 在面板内自行渲染（ADR-0022）：面板自带一条 42px 工具栏——跟随滚动的 `‹ n / N ›` 页码、10% 步进的缩放、点百分比回到适应宽度；所有页一次画完，连续滚动阅读。不再交给浏览器自带阅读器，那个阅读器会带自己的灰底工具栏、自己的深色皮肤，680px 的侧栏里它长得像另一个应用，iOS Safari 上还只显示第一页。**中文 PDF 不会空白**：未嵌入的中文字体（ReportLab 的 `STSong-Light` 是最常见的一种）要 CMap 表才能画字形，所以 `cMapUrl` / `standardFontDataUrl` / `wasmUrl` 指向 `/pdfjs/` 下的三套资源，构建启动时从 `node_modules/pdfjs-dist` 复制（4.3 MB，已 gitignore，按需请求——那份三页的中文报告只拉了 2 个 `.bcmap`）。worker 的 URL 是 `/pdfjs/pdf.worker.min.mjs?v=<pdfjs 版本>`，由 `vite.config.ts` 注入的 `__PDFJS_VERSION__` 拼出——**不参与内容哈希**，因为哈希换不掉「响应头曾经错了」的那份缓存（ADR-0023）。解析失败时面板给出错误、重试与「在新窗口打开」，后者走 `/api/files/{id}/view`（这个端点只对 PDF 返回 `inline`），退回浏览器自己的阅读器；
- **预览面板与对话之间的分隔线可以拖动**：9px 的拖拽带就是那条线，指针移上去会加粗成主题色，按住左右移动即可分配两栏宽度（预览最窄 300px，对话永远留 420px），双击或聚焦后按 `Home` 回到默认的 `min(42vw, 640px)`，方向键每次 24px。宽度记在 `localStorage` 的 `chat.previewWidth`，下次打开同一浏览器照旧；窗口变小会自动收回，不会把对话挤没。窄于 1100px 时预览本来就是整屏覆盖层，分隔线隐藏。拖动只写 CSS 变量 `--preview-width`、不触发 React 重渲染，所以流式输出期间拖也不会被别的渲染打断；PDF 会跟着重排页宽（`ResizeObserver` 驱动的 `boxWidth`）。
- **Excel / PPT / Word** 不要求浏览器能打开 Office：后端用标准库读出内容——表格、每页幻灯片的标题与要点、文档段落——浏览器按这份只读视图预览，原文件照常下载。

单个文件 2 MB、每轮 20 个条目（图片至多 6 张，免得成批图表把真正的交付物挤出名单）；内容未变的文件不重复入库，只在新的消息下再挂一张卡。凡是写文件或跑过命令的轮次结束时都会扫描工作区（见 [ADR-0012](docs/adr/0012-workspace-writes-and-html-delivery.md)）。

**文件名本身就是下载入口**：产物卡片上的名字是链接，回答正文里提到的文件名也是——模型写 `report.xlsx` 还是 `` `report.xlsx` `` 都能点。只有**这一轮真实产出的名字**才会被链接，正文里提到的别的文件名保持纯文本，免得把一句说明变成坏链接；代码块内不替换（那里文件名是内容本身）。

### 前端流式体验

- **思考 / 行动 / 观察活动条**：运行中叙述当前阶段（正在思考 → 正在调用 *工具* → 正在生成回答 → 已完成 · N 步 · X 秒），步骤以阶段徽章 + 工具名单行呈现，完成后自动折叠。
- **稳定的流式渲染**：已完成的段落按块 memo 渲染，未闭合段落以纯文本增长并带光标，按 ~30fps 合帧；只在用户停留在底部时跟随滚动。
- **产物预览是多标签的**：每打开一个文件占一个标签，可切换、可单独关闭，关掉最后一个才收起面板；标签栏上的 `+` 列出这轮产生但还没打开的其余文件。≥1101px 时面板与对话并排、自动打开；窄视口下预览变成工作区的**另一个视图**而不是覆盖层，标签栏里因此多一个「聊天」项用来切回——打开文件不再把整段会话压在底下（[ADR-0028](docs/adr/0028-preview-opens-as-tabs.md)）。
- **上下文容量环**：输入框右侧常驻一枚环形指示器，显示当前会话已占用上下文占窗口的百分比——70% 起转为提醒色、90% 起转为告警色，与运行时触发压缩的阈值一致。数值取自后端的真实估算，刷新页面从服务端恢复，不会从 0 重新长出来。
- **失败可重试**：上游限流（`429`）、`5xx` 或连接中断由网关自动重试（`MODEL_RETRIES`，指数退避，认服务商的 `Retry-After`），只在**还没有吐出任何内容**之前重试——已经流出的回答不会被重放成两份（[ADR-0019](docs/adr/0019-model-call-retries.md)）。重试全部失败时，气泡里写明是哪一类失败（限流 / 余额 / 密钥 / 连接中断），并保留一颗「重试」按钮：点一下就会丢掉这条失败回答、用同一条提问重发，不需要重新打字，也不额外消耗一轮访客额度。
- 中英文双语，明暗主题，Markdown 与代码复制。
- **品牌名只有一个来源**：`src/shared/config/brand.ts` 的 `APP_NAME` / `APP_SLUG` 供给欢迎页标题与导出文件名；`index.html` 的 `<title>` 在模块执行前就被解析，只能是字面量，两边必须同步改。改名前页面上同时存在 `AI-CHAT`（标签页）、`AICHAT`（欢迎页）和 `ai-chat-`（导出文件名）三种写法。

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
- **`ask_user` 可以带 `options`**：模型把 2–4 条可选路径写成一句话一个选项（从推荐到兜底），前端渲染成按钮，**点一下就把该句原样回给模型**，任务继续跑，不必走"先选再提交"两步。这是工具失败或信息缺失时的推荐出口——运行时提示明确要求模型别停下、别道歉、别重复失败调用，而是给选项。选项与问题同等的闸门：同样走 `forbid_patterns`，同样去重、截断（每条 200 字、上限 8 条），所以刷新页面也不会渲染出另一个样子的选项。
- `GET /api/interventions/policy` 返回解析后的完整策略，回答"系统到底在哪些地方拦我"。
- 交互卡片渲染在输入框上方；交互卡不进上下文，只在消息里留一行可展开的审计轨迹（已批准 / 已拒绝 / 已回复 / 已跳过）。

### 日志与可观测性

后端按 `AGENTFORGE_LOG_LEVEL` / `AGENTFORGE_LOG_FORMAT`（`text` 或 `json`）输出结构化日志：

- 每个请求带一个请求 id，由 ContextVar 贯穿该请求的所有日志行，并在响应头 `X-Request-Id` 回给调用方，一次失败可以整串捞出来。
- 记录：HTTP 方法 / 路径 / 状态码 / 耗时，运行开始与结束（含停止原因与代价），每次工具调用的耗时，审批闸门与结果，上下文压缩。**不记录**工具参数与输出——那是用户数据。
- 异步客户端（httpx / anyio）会把真实错误包进 `ExceptionGroup`，日志前会剥壳，否则一次 401 只会显示成"unhandled errors"。
- 令牌与托管 URL 在日志里一律打码（`redact.py`）；`httpx`、`mcp`、`pymongo`、`uvicorn.access` 等噪声 logger 已静音。

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
├── docs/                 # IMPLEMENTATION、CAPABILITIES 与 ADR 0001-0033
├── mcp-server/           # 独立 MCP 示例服务
├── examples/skills/      # 示例技能包
└── scripts/              # i18n 检查与浏览器验证脚本
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

登录与访客试聊链路可以在真实浏览器里验证（需要后端已用 `ADMIN_PASSWORD` 启动、前端 dev 服务在跑）：

```bash
/usr/bin/python3 scripts/verify_auth_flow.py --password <管理密码>   # 登录/退出/会话持久
/usr/bin/python3 scripts/verify_guest_flow.py  --password <管理密码> # 访客试聊/额度用尽/登录接管
```

邮箱链路本地验收不需要任何云账号：起一个本地 SMTP 收件箱（例如 `docker run -d -p 1025:1025 -p 8025:8025 axllent/mailpit`），后端用 `MAIL_PROVIDER=smtp SMTP_HOST=127.0.0.1 SMTP_PORT=1025 SMTP_TLS=none SMTP_FROM=no-reply@local.test` 启动，登录框发码后到 `http://localhost:8025` 读信取码；或者用 `MAIL_PROVIDER=console MAIL_DEBUG_ECHO=true` 让验证码直接回显在界面上（仅本地，绝不能带到公网）。接真实邮箱只需把 `SMTP_*` 换成为邮箱服务商的主机、账号与授权码，配置不全时发码接口会明确返回 503 而不是静默降级。

Docker 与 MongoDB 用例在缺少外部依赖时会显式跳过；浏览器验证脚本位于 `scripts/`，部分会调用真实模型并产生费用。

## 边界与限制

- 除访客试聊白名单（目录读取、发起对话、应答审批，ADR-0015）外，所有 `/api` 请求都需要登录（`/api/auth/*` 与 `/health` 除外）。管理员账号由 `ADMIN_USER` / `ADMIN_PASSWORD` 在启动时创建；邮箱账号由验证码自助创建（ADR-0018），两者共用同一套会话与归属过滤。手机号端点仍在（ADR-0017），但界面已不提供入口，旧手机号账号需要先绑定邮箱才能继续从界面登录——迁移入口尚未做，见 NOTES Backlog。会话是可撤销的服务端记录，浏览器只持有 HttpOnly Cookie。身份只能来自 Cookie——请求体、工具参数或模型输出都无法指定用户。访客身份同样带 `owner_id`，互相隔离；试聊有每访客与全站每日双重额度。跨用户的资源统一返回 404。
- **沙箱按「会话」隔离，租约再绑定到账号**：每个会话独占一个容器与一个工作区目录，容器只挂载自己的目录，所以会话之间（含不同账号、不同访客）互不可见。租约现在带 `owner`：拿别人的会话 id 也认领不到那台容器和那批文件，池直接拒绝；`/api/sandbox/exec` 的一次性容器同样按调用者记账；每账号同时最多 `SANDBOX_MAX_PER_OWNER`（默认 4）个沙箱，避免一个人占满整池。工作区目录仍是 `workspaces/sandboxes/<sandbox-id>` 扁平存放——预热容器在认领前就已经挂好了目录，事后搬动会让它继续写进已删除的 inode；隔离靠租约而不是路径（见 [ADR-0016](docs/adr/0016-sandbox-lease-ownership.md)）。**「会话 → 目录」的映射同时落盘在 `sandboxes/sessions.json`**，池启动时在清扫之前读回：进程重启（也就是每次部署）后同一会话仍回到自己那批文件，而不是拿到一个空目录；登录时租约随会话一起接管，所以「访客试聊 → 登录 → 继续同一会话」不会以「沙箱属于另一个账号」失败（见 [ADR-0026](docs/adr/0026-a-workspace-belongs-to-the-conversation.md)）。T15.A3 剩余的按用户目录与迁移演练尚未做（令牌已由 `secrets_box` 加密落库）。
- 受限工具不等于 Docker 安全边界，真正隔离由沙箱提供。
- **自动重试不是万能兜底**：只有「一个 token 都还没吐出来」的失败会被重试，所以服务商在回答中途掐断连接时，用户看到的是半截答案加一颗「重试」按钮，而不是自动续写——续写需要重放整轮，代价和语义都不一样（ADR-0019）。重试预算按**每次模型调用**计，不是按轮次计：一个十步的智能体任务最多会重试十次，限流严重时单轮耗时会随之上升。
- **联网只发生在宿主进程，不在容器里**：`web_search` / `web_fetch` 由 harness 发请求、带地址校验，命令执行仍然在离线沙箱内、`curl` 依然不通。远程 MCP 与这两个工具都不进沙箱，风险由“地址校验 + 审批”承担，而不是由沙箱承担（ADR-0024）。
- 产物预览为净化环境，交互脚本只在下载后的文件中运行。

## 文档索引

- [实现与验收记录](docs/IMPLEMENTATION.md) · [待办与决策](NOTES.md) · [项目结构规范](ARCHITECTURE.md)
- [技能与 MCP 能力说明](docs/CAPABILITIES.md)
- ADR：[0001 基础与运行时](docs/adr/0001-base-and-runtime.md) · [0002 SSE 兼容](docs/adr/0002-sse-compatibility.md) · [0003 运行时与检查点](docs/adr/0003-runtime-and-checkpoints.md) · [0004 中间件与审批](docs/adr/0004-middleware-and-approvals.md) · [0005 上下文压缩](docs/adr/0005-context-compaction.md) · [0006 Docker 沙箱](docs/adr/0006-docker-sandbox.md) · [0007 预热池与快照](docs/adr/0007-sandbox-pool-and-snapshots.md) · [0008 沙箱审批 UI](docs/adr/0008-sandbox-runtime-approval-ui.md) · [0009 技能与 MCP](docs/adr/0009-skills-and-mcp-runtime.md) · [0010 技能安装](docs/adr/0010-agent-skill-installation.md) · [0011 模型输出恢复](docs/adr/0011-model-output-recovery.md) · [0012 工作区写入与 HTML 交付](docs/adr/0012-workspace-writes-and-html-delivery.md) · [0013 沙箱自主命令](docs/adr/0013-autonomous-docker-commands.md) · [0014 账号与归属](docs/adr/0014-user-auth-and-ownership.md) · [0015 访客试聊](docs/adr/0015-anonymous-trial.md) · [0016 沙箱租约归属](docs/adr/0016-sandbox-lease-ownership.md) · [0017 手机号注册与短信](docs/adr/0017-phone-signup-and-sms-codes.md) · [0018 邮箱注册与验证码邮件](docs/adr/0018-email-signup-and-smtp-codes.md) · [0019 模型调用重试](docs/adr/0019-model-call-retries.md) · [0020 会话文件系统](docs/adr/0020-conversation-file-system.md) · [0021 PDF 预览交给浏览器](docs/adr/0021-pdf-viewer-is-the-browser.md) · [0022 PDF 由 pdf.js 自绘](docs/adr/0022-pdf-pages-are-drawn-by-pdfjs.md) · [0023 pdf.js worker 按版本寻址](docs/adr/0023-pdfjs-worker-url-is-versioned.md) · [0024 联网是宿主能力](docs/adr/0024-web-access-is-a-harness-capability.md) · [0025 跟随由输入释放](docs/adr/0025-follow-is-released-by-input.md) · [0026 工作区属于会话](docs/adr/0026-a-workspace-belongs-to-the-conversation.md) · [0027 比价读商品库](docs/adr/0027-a-price-lookup-reads-the-catalogue.md) · [0028 预览是标签而不是覆盖](docs/adr/0028-preview-opens-as-tabs.md) · [0029 目录持有地址](docs/adr/0029-the-catalogue-owns-the-address.md) · [0030 目录可以列出别人的服务](docs/adr/0030-a-catalogue-can-list-somebody-elses-services.md) · [0031 见过的货架不被刷新抹掉](docs/adr/0031-a-shelf-once-seen-is-not-emptied-by-a-refresh.md) · [0032 掉线是一句提示](docs/adr/0032-a-dropped-session-is-a-banner.md) · [0033 参考树退役](docs/adr/0033-the-reference-tree-retires.md)

## 常见问题

- **健康检查 `degraded`**：MongoDB 未启动或 `MONGODB_URI` 不对。
- **运行返回 503**：未配置 `DEEPSEEK_API_KEY`，或密钥无对应模型权限。
- **接口 404 / 无流式**：确认前端走的是 Vite 代理（`/api` → 后端），且反向代理未缓冲 SSE、读取超时大于 300 秒。
- **沙箱相关失败**：检查 Docker 是否运行、镜像是否可用；沙箱失败不会回退宿主。
- **Docker Hub 拉取失败**：可用 `public.ecr.aws/docker/library/{mongo:7,python:3.12-slim}` 后本地打 tag。
