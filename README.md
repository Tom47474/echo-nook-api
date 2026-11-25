# EchoNook API

EchoNook 虚拟陪伴频道的独立服务端仓库。基于 Python 3.14、FastAPI 和 Pydantic，提供健康检查与文字对话流式接口。支持演示回复和火山方舟模型的流式回复，模型 ID 由 `.env` 配置。

## 本地启动

在仓库根目录执行：

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/alembic upgrade head
.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

如果已激活 `.venv`，可把命令中的 `.venv/bin/python` 换成 `python`。打开 <http://localhost:8000/docs> 查看交互式接口文档，或访问 <http://localhost:8000/health> 检查服务。

S5-1 后浏览器先调用 `POST /api/v1/identity/bootstrap` 建立游客身份，再携带服务端签发的 HttpOnly Cookie 访问会话接口。账号可通过用户名和密码注册/登录；登录后直接显示账号的独立窗口，退出后恢复该浏览器的游客窗口；两者消息不会合并。直接用 curl 调试会话接口时也需要保留 Cookie，例如 `curl -c /tmp/echo-nook-cookies -b /tmp/echo-nook-cookies -X POST http://localhost:8000/api/v1/identity/bootstrap`，后续请求加 `-b /tmp/echo-nook-cookies`。旧版无归属会话的本地恢复方式见 [数据库指南](docs/database.md)。

身份接口包括 `POST /api/v1/identity/bootstrap`、`/register`、`/login` 和 `/logout`。注册/登录接收 `{"username":"...","password":"..."}`；旧游客导入接口已移除。游客和账号均可使用会话接口，但只能访问本身份的会话。账号密码找回、登录失败限流和正式部署联调仍属于阶段五后续工作。

## 试用流式接口

先取得当前身份唯一窗口的 `id`：

```bash
curl -b /tmp/echo-nook-cookies http://localhost:8000/api/v1/conversations/current
```

再把下面的 `{conversation_id}` 替换为该 ID：

```bash
curl -N -X POST http://localhost:8000/api/v1/conversations/{conversation_id}/messages:stream \
  -b /tmp/echo-nook-cookies \
  -H 'Content-Type: application/json' \
  -d '{"message":"你好"}'
```

接口先返回 `message.started`，再返回 `message.delta`、`message.completed` 或 `message.failed`。错误事件会区分配置缺失、鉴权失败、模型不可用、限流、超时、网络不可达和一般生成失败。`LLM_PROVIDER=openai_compatible` 调用已配置的真实模型，`LLM_PROVIDER=demo` 返回演示文本；实际使用哪一种取决于启动时读取的环境配置。前端已通过 `http-reply-provider.ts` 使用该持久化接口，历史由服务端数据库提供。

## 会话管理接口

当前页面使用单窗口接口；旧版多会话接口仍暂保留兼容，但新前端不会调用：

```text
GET    /api/v1/conversations/current
DELETE /api/v1/conversations/current/messages
POST   /api/v1/conversations                  # 旧版兼容
GET    /api/v1/conversations?offset=0&limit=20 # 旧版兼容
GET    /api/v1/conversations/{id}
GET    /api/v1/conversations/{id}/messages?limit=50&before_sequence=100
POST   /api/v1/conversations/{id}/messages:stream
DELETE /api/v1/conversations/{id}
```

当前身份在页面中只有一个持久化窗口；消息接口默认读取最新一页，但响应内始终按 `sequence` 正序排列。新的会话流式接口只接收本次 `message`：服务端原子创建用户消息和助手占位消息，再从数据库加载最近完整回合并调用模型，客户端不能提交 `history`。助手正文会分段保存，完成、失败和用户停止都会写入最终状态。

长对话在真实模型模式下按需更新会话摘要：保留最近 10 个问答回合原文，每次最多将更早的 3 个回合并入摘要；摘要和近期原文共同进入模型上下文，失败时回退到原文。摘要生成会额外调用一次模型，摘要只用于服务端上下文，不作为消息展示。首次使用前执行 `.venv/bin/alembic upgrade head`（测试库使用 `APP_ENV=test`）。

S4-4 在 `model_calls` 表分别记录摘要与主回复的模型 ID、调用耗时、结束原因和供应商返回的 token 用量。方舟流式请求显式启用 `stream_options.include_usage`；供应商未返回的字段保持 `NULL`，不使用 S4-1 估算值填充。表中不保存提示词、回复正文或 API Key。当前数据库版本为 `0006_single_window`；升级服务前先执行 `alembic upgrade head`。

前端不再在 URL 保存会话 ID。刷新页面时按当前游客或账号身份读取唯一窗口及其消息。首次读取最新 100 条消息，滚到顶部或点击按钮后使用 `before_sequence` 每次加载更早 50 条。

新接口首先发送 `message.started`，其中包含用户/助手消息 UUID 和 sequence；随后发送原有的 `message.delta`、`message.completed` 或 `message.failed`。处理新分片时，如果草稿累计新增 200 字符或距上次保存已达 1 秒，就执行一次检查点保存；最终事件前再完整保存。

旧接口 `POST /api/v1/conversations/messages:stream` 暂时保留用于兼容和独立调试；当前前端已经不再调用它。旧接口不会持久化消息，后续在完成兼容性评估后移除。

## 环境配置与火山方舟

服务端只使用仓库根目录的 `.env`。该文件已被 Git 忽略；可复制 `.env.example` 后在本机编辑，示例文件不包含密钥。

| 变量 | 当前值及用途 |
| --- | --- |
| `APP_NAME` | 接口文档中的服务名称。 |
| `APP_ENV` | 环境标识，只接受 `development`、`test`、`production`，默认 `development`。 |
| `FRONTEND_ORIGIN` | 允许跨域访问的前端地址，默认 `http://localhost:3000`。 |
| `DATABASE_URL` | 开发和正式环境的异步 PostgreSQL 连接；本地开发使用 `echo_nook_dev`。 |
| `TEST_DATABASE_URL` | `APP_ENV=test` 和自动化数据库测试使用；数据库名必须以 `_test` 结尾。 |
| `DATABASE_ECHO` | 是否输出 SQLAlchemy SQL 日志，默认 `false`。 |
| `LLM_PROVIDER` | `demo` 为演示；`openai_compatible` 调用火山方舟。 |
| `LLM_API_BASE_URL` | 方舟 API 地址，默认 `https://ark.cn-beijing.volces.com/api/v3`。 |
| `LLM_API_KEY` | 火山方舟 API Key，只放在服务端 `.env`。 |
| `LLM_MODEL` | 要调用的模型 ID 或推理接入点 ID，必须在 `.env` 中填写；代码不预设模型名称。 |
| `LLM_REASONING_EFFORT` | `low`、`high` 或 `max`；默认 `low`，适合交互式聊天。 |
| `LLM_CONTEXT_WINDOW_TOKENS` | 当前模型的上下文窗口，默认保守使用 `32768`；切换模型时按供应商资料核对。 |
| `LLM_MAX_OUTPUT_TOKENS` | 为回答（含可能的思考过程）预留并传给模型的 `max_tokens`，默认 `4096`。 |
| `LLM_TOKEN_SAFETY_MARGIN` | 输入估算误差的安全余量，默认 `1024`。 |

S4-1 使用 `上下文窗口 - 最大输出 - 安全余量` 作为输入预算，先扣除系统提示词和本次问题，再从近到远按完整回合选择历史。发送前按 UTF-8 字节数及每条消息 16 token 的包装余量保守估算；它不是供应商真实分词结果，极端内容仍可能被上游拒绝。本次问题单独超预算会返回 422 `input_budget_exceeded`，不会创建消息。S4-2 按消息顺序和终态组装问答：失败或取消的部分正文附内部中断标记，空回复只保留用户问题；同一问题的“再次提问”仍是独立回合。实际用量读取和记录属于 S4-4。

使用 `openai_compatible` 时，适配器调用方舟的 `/chat/completions` 流式接口，只转发回答正文，不将模型的思考内容展示给前端。代码默认值为 `demo`，未显式设置 `LLM_PROVIDER` 时会使用演示回复；即使配置了 API Key 和模型 ID，只要 Provider 仍是 `demo`，也不会调用真实模型。不要提交 `.env` 或在日志中打印密钥。

切换模型时，只需把 `.env` 中的 `LLM_MODEL` 改为目标模型 ID 或接入点 ID，然后重启服务端；模型名称不会写在请求处理代码中。当前本机配置的 `kimi-k3-1-pro-261024` 已通过实际流式请求验证。

如果返回 `model_not_available`，请在方舟控制台确认该 API Key 所属账号已开通所选模型，并把控制台实际可用的模型 ID 或推理接入点 ID 填入 `LLM_MODEL`。本账号使用别名 `kimi-k3-1-pro` 时返回 404，版本化 ID `kimi-k3-1-pro-261024` 已通过实际请求验证。

## 代码结构

```text
app/
├── api/                 HTTP 路由与总路由
├── conversations/       对话 Schema、提示词与事件流业务
├── core/                环境配置
├── db/                  SQLAlchemy 基类、异步连接与请求会话
├── llm/                 模型接口、演示实现和火山方舟适配器
└── main.py              应用入口与 CORS
alembic/                  数据库迁移环境和版本脚本
tests/                   基础测试
docs/                    架构和目录说明
```

每个文件的职责和扩展规则见 [服务端架构与目录说明](docs/backend-architecture.md)。前后端各阶段技术难点、具体实现和取舍见 [对话功能技术难点与解决方案](docs/conversation-technical-decisions.md)。数据库安装、连接、常用命令、备份和排错见 [PostgreSQL 本地开发与测试指南](docs/database.md)。前端在独立的 `echo-nook-web` 仓库，两者通过 HTTP 接口连接。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests
```

服务端已接入 SQLAlchemy、asyncpg 和 Alembic。启动时会连接并检查当前环境的数据库，关闭时释放连接池；数据库不可用或连接配置缺失时启动失败。会话流式接口已经持久化用户问题、助手草稿及 `completed`、`failed`、`cancelled` 最终状态。应用异常退出后遗留的 `streaming` 消息会在下次启动时恢复为 `failed`。

用户停止或浏览器断开 SSE 时，服务端会主动关闭模型 Provider 的异步流并释放上游响应上下文。自动化测试覆盖该关闭传播；模型供应商是否立即停止计算或计费仍取决于供应商实现。

## 对话功能后续规划

开发顺序、任务状态、验收要求和实施记录见 [对话功能开发规划与进度](docs/conversation-roadmap.md)。每完成一个任务，同步更新路线图、[技术难点与解决方案](docs/conversation-technical-decisions.md)及受影响的接口、架构和运行说明。
