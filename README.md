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

## 试用流式接口

以下是当前前端仍在使用的旧兼容接口，历史由客户端传入：

```bash
curl -N -X POST http://localhost:8000/api/v1/conversations/messages:stream \
  -H 'Content-Type: application/json' \
  -d '{"message":"你好","history":[]}'
```

接口返回 `message.delta`、`message.completed` 或 `message.failed` 事件。错误事件会区分配置缺失、鉴权失败、模型不可用、限流、超时、网络不可达和一般生成失败。当前本机 `.env` 会调用真实模型；改为 `LLM_PROVIDER=demo` 才会返回演示文本。前端已通过 `http-reply-provider.ts` 消费这些事件。请求中的 `history` 由客户端提供，尚未保存到数据库。

## 会话管理接口

阶段 S2-3 已提供服务端会话容器管理和历史消息查询：

```text
POST   /api/v1/conversations
GET    /api/v1/conversations?offset=0&limit=20
GET    /api/v1/conversations/{id}/messages?limit=50&before_sequence=100
POST   /api/v1/conversations/{id}/messages:stream
DELETE /api/v1/conversations/{id}
```

会话列表按最近更新时间倒序排列；消息接口默认读取最新一页，但响应内始终按 `sequence` 正序排列。新的会话流式接口只接收本次 `message`：服务端原子创建用户消息和助手占位消息，再从数据库加载最近完整回合并调用模型，客户端不能提交 `history`。助手正文会分段保存，完成、失败和用户停止都会写入最终状态。

新接口首先发送 `message.started`，其中包含用户/助手消息 UUID 和 sequence；随后发送原有的 `message.delta`、`message.completed` 或 `message.failed`。处理新分片时，如果草稿累计新增 200 字符或距上次保存已达 1 秒，就执行一次检查点保存；最终事件前再完整保存。

旧接口 `POST /api/v1/conversations/messages:stream` 暂时保留，供尚未升级的前端继续传入客户端历史；阶段三切换前端后再制定移除计划。

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

当前本机 `.env` 使用 `openai_compatible` 调用方舟的 `/chat/completions` 流式接口。适配器只转发回答正文，不将模型的思考内容展示给前端。代码默认值仍为 `demo`，因此在没有本机 `.env` 的环境中会使用演示回复。不要提交 `.env` 或在日志中打印密钥。

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

每个文件的职责和扩展规则见 [服务端架构与目录说明](docs/backend-architecture.md)。数据库安装、连接、常用命令、备份和排错见 [PostgreSQL 本地开发与测试指南](docs/database.md)。前端在独立的 `echo-nook-web` 仓库，两者通过 HTTP 接口连接。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests
```

服务端已接入 SQLAlchemy、asyncpg 和 Alembic。启动时会连接并检查当前环境的数据库，关闭时释放连接池；数据库不可用或连接配置缺失时启动失败。会话流式接口已经持久化用户问题、助手草稿及 `completed`、`failed`、`cancelled` 最终状态。应用异常退出后遗留的 `streaming` 消息会在下次启动时恢复为 `failed`。

用户停止或浏览器断开 SSE 时，服务端会主动关闭模型 Provider 的异步流并释放上游响应上下文。自动化测试覆盖该关闭传播；模型供应商是否立即停止计算或计费仍取决于供应商实现。

## 对话功能后续规划

开发顺序、任务状态、验收要求和实施记录见 [对话功能开发规划与进度](docs/conversation-roadmap.md)。每完成一个任务，同步更新该文档及受影响的接口、架构和运行说明。
