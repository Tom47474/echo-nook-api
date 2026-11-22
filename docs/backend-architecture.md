# 服务端架构与目录说明

EchoNook API 是独立的 FastAPI 服务。当前版本提供健康检查和文字流式回复。本机通过仓库根目录的 `.env` 选择火山方舟模型；代码也保留演示模型，供未配置真实模型的环境使用。当前没有数据库与持久化，前端需在请求中传入近期对话历史。

## 目录与文件职责

| 路径 | 作用 |
| --- | --- |
| `app/main.py` | 创建 FastAPI 应用、配置 CORS、注册总路由。 |
| `app/api/router.py` | 汇总各业务路由。 |
| `app/api/routes/health.py` | `/health` 健康检查。 |
| `app/api/routes/conversations.py` | 对话 HTTP 接口，负责校验后的请求与流式响应封装。 |
| `app/core/config.py` | 定义配置项和默认值，从仓库根目录的 `.env` 加载本机配置。 |
| `app/conversations/schemas.py` | 对话请求和历史消息的数据格式与长度限制。 |
| `app/conversations/service.py` | 组装消息、设置角色提示词、把文本片段编码为 SSE 事件。 |
| `app/llm/base.py` | 模型适配器统一接口 `LLMProvider`。 |
| `app/llm/demo.py` | 演示模型，逐段返回明确标识的示例文字。 |
| `app/llm/openai_compatible.py` | 火山方舟 Chat Completions 流式调用与响应解析。 |
| `app/llm/factory.py` | 根据配置选择演示或真实模型适配器。 |
| `tests/` | 对话事件格式、方舟流解析和模型不可用时的错误处理测试。 |
| `.env` | 本机环境配置，包含模型密钥；被 Git 忽略。 |
| `.gitignore` | 排除 `.env`、虚拟环境和 Python 缓存等本机文件。 |
| `pyproject.toml` | Python 版本、依赖和项目元数据。 |

`__init__.py` 标记 Python 包。`.venv/` 是本机依赖环境。项目只使用 `.env` 作为本机配置文件；`.venv/` 和 `.env` 都被 Git 忽略。

## 依赖方向

```text
HTTP 路由 → 对话服务 → LLMProvider 接口 → 演示/真实模型适配器
                  ↑
              请求 Schema
```

路由不处理模型供应商返回格式。具体适配器不处理 HTTP 响应。对话服务只使用 `LLMProvider` 接口。以后增加数据库时，在 `app/db/` 建立连接与迁移，在 `app/conversations/` 增加持久化层；路由只传入会话 ID。

## 当前接口契约

`GET /health` 返回 `{"status":"ok"}`。

`POST /api/v1/conversations/messages:stream` 接受：

```json
{
  "message": "你好",
  "history": [{"role": "user", "content": "我们来聊聊音乐"}]
}
```

响应的 `Content-Type` 是 `text/event-stream`。服务端依次返回 `message.delta` 事件及一个 `message.completed` 事件；生成失败时返回 `message.failed`，不会再发送 `completed`。前端用 `fetch` 的流读取器解析事件。历史最多 20 条，每条消息和本次输入最多 4000 字符。

前端 `http-reply-provider.ts` 把本次用户输入放在 `message`，将已完成的消息放在 `history`；用户停止生成时会取消 HTTP 请求。

客户端断开连接时，框架应取消流式任务。此阶段不保存历史，刷新后会话不会恢复。

## 使用火山方舟模型

1. 在仓库根目录的 `.env` 中配置 `LLM_PROVIDER=openai_compatible`、`LLM_API_KEY`、`LLM_API_BASE_URL`、`LLM_MODEL` 和 `LLM_REASONING_EFFORT`。密钥只写入服务端 `.env`。
2. `LLM_MODEL` 没有代码默认值，必须在 `.env` 中填写。当前本机使用 `kimi-k3-1-pro-261024`，已通过实际流式请求验证。切换模型时修改这个配置为其他可用模型 ID 或接入点 ID，然后重启服务端；无需修改 Python 代码。
3. `app/llm/openai_compatible.py` 发送系统提示词和历史消息，逐段读取方舟 SSE，只输出 `delta.content`；`app/conversations/service.py` 将其包装成前端统一事件。

密钥缺失、鉴权失败、模型不可用或上游调用失败时，统一事件流返回 `message.failed`。不要在代码或前端仓库中提交真实密钥。删除 `.env` 后，代码默认使用演示模型。

## 后续阶段

前端联调完成后，可加入 PostgreSQL、SQLAlchemy、Alembic，实现服务端会话与消息保存。再增加用户认证、语音合成和知识库；这些能力分别建模块，不放入现有路由文件。
