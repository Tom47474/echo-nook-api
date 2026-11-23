# EchoNook API

EchoNook 虚拟陪伴频道的独立服务端仓库。基于 Python 3.14、FastAPI 和 Pydantic，提供健康检查与文字对话流式接口。支持演示回复和火山方舟模型的流式回复，模型 ID 由 `.env` 配置。

## 本地启动

在仓库根目录执行：

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

如果已激活 `.venv`，可把命令中的 `.venv/bin/python` 换成 `python`。打开 <http://localhost:8000/docs> 查看交互式接口文档，或访问 <http://localhost:8000/health> 检查服务。

## 试用流式接口

```bash
curl -N -X POST http://localhost:8000/api/v1/conversations/messages:stream \
  -H 'Content-Type: application/json' \
  -d '{"message":"你好","history":[]}'
```

接口返回 `message.delta`、`message.completed` 或 `message.failed` 事件。当前本机 `.env` 会调用真实模型；改为 `LLM_PROVIDER=demo` 才会返回演示文本。前端已通过 `http-reply-provider.ts` 消费这些事件。请求中的 `history` 由客户端提供，尚未保存到数据库。

## 环境配置与火山方舟

服务端只使用仓库根目录的 `.env`。该文件已被 Git 忽略；可复制 `.env.example` 后在本机编辑，示例文件不包含密钥。

| 变量 | 当前值及用途 |
| --- | --- |
| `APP_NAME` | 接口文档中的服务名称。 |
| `APP_ENV` | 环境标识，只接受 `development`、`test`、`production`，默认 `development`。 |
| `FRONTEND_ORIGIN` | 允许跨域访问的前端地址，默认 `http://localhost:3000`。 |
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
├── llm/                 模型接口、演示实现和火山方舟适配器
└── main.py              应用入口与 CORS
tests/                   基础测试
docs/                    架构和目录说明
```

每个文件的职责和扩展规则见 [服务端架构与目录说明](docs/backend-architecture.md)。前端在独立的 `echo-nook-web` 仓库，两者通过 HTTP 接口连接。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests
```

当前阶段没有数据库。加入持久化时再配置 PostgreSQL、SQLAlchemy 和 Alembic。

用户停止或浏览器断开 SSE 时，服务端会主动关闭模型 Provider 的异步流并释放上游响应上下文。自动化测试覆盖该关闭传播；模型供应商是否立即停止计算或计费仍取决于供应商实现。

## 对话功能后续规划

开发顺序、任务状态、验收要求和实施记录见 [对话功能开发规划与进度](docs/conversation-roadmap.md)。每完成一个任务，同步更新该文档及受影响的接口、架构和运行说明。
