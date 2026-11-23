# 服务端架构与目录说明

EchoNook API 是独立的 FastAPI 服务。当前版本提供健康检查和文字流式回复。本机通过仓库根目录的 `.env` 选择火山方舟模型；代码也保留演示模型，供未配置真实模型的环境使用。本机 PostgreSQL 服务以及开发、测试数据库已经准备好，但代码尚未接入数据库、没有业务表和持久化，前端仍需在请求中传入近期对话历史。

## 目录与文件职责

| 路径 | 作用 |
| --- | --- |
| `app/main.py` | 创建 FastAPI 应用、配置 CORS、注册总路由。 |
| `app/api/router.py` | 汇总各业务路由。 |
| `app/api/routes/health.py` | `/health` 健康检查。 |
| `app/api/routes/conversations.py` | 对话 HTTP 接口，负责校验后的请求与流式响应封装。 |
| `app/core/config.py` | 定义配置项和默认值，从仓库根目录的 `.env` 加载本机配置；`APP_ENV` 仅接受 development、test、production。 |
| `app/conversations/schemas.py` | 对话请求和历史消息的数据格式与长度限制。 |
| `app/conversations/service.py` | 组装消息、设置角色提示词、把文本片段编码为 SSE 事件。 |
| `app/llm/base.py` | 模型适配器统一接口 `LLMProvider`。 |
| `app/llm/demo.py` | 演示模型，逐段返回明确标识的示例文字。 |
| `app/llm/openai_compatible.py` | 火山方舟 Chat Completions 流式调用与响应解析。 |
| `app/llm/factory.py` | 根据配置选择演示或真实模型适配器。 |
| `tests/` | 对话事件格式、方舟流解析和模型不可用时的错误处理测试。 |
| `.env` | 本机环境配置，包含模型密钥和数据库凭据；被 Git 忽略。 |
| `.env.example` | 不含密钥的环境配置模板，默认使用演示模型。 |
| `.gitignore` | 排除 `.env`、虚拟环境和 Python 缓存等本机文件。 |
| `docs/database.md` | PostgreSQL 本地配置、连接、常用命令、备份、迁移约定和排错说明。 |
| `pyproject.toml` | Python 版本、依赖和项目元数据。 |

`__init__.py` 标记 Python 包。`.venv/` 是本机依赖环境。项目只使用 `.env` 作为本机配置文件；`.venv/` 和 `.env` 都被 Git 忽略。

## 依赖方向

```text
HTTP 路由 → 对话服务 → LLMProvider 接口 → 演示/真实模型适配器
                  ↑
              请求 Schema
```

路由不处理模型供应商返回格式。具体适配器不处理 HTTP 响应。对话服务只使用 `LLMProvider` 接口。阶段二将在 `app/db/` 建立连接与迁移，在 `app/conversations/` 增加持久化层；路由只传入会话 ID。当前数据库准备情况和操作方式见 [PostgreSQL 本地开发与测试指南](database.md)。

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

前端 `http-reply-provider.ts` 把本次用户输入放在 `message`，按完整回合构造最多 20 条 `history`。完成回复原样传入；失败或取消的部分回复带内部中断标记传入，空回复只保留对应用户消息。用户停止生成时会取消 HTTP 请求。

`message.failed` 的 `code` 当前包括 `provider_not_configured`、`provider_auth_failed`、`model_not_available`、`provider_rate_limited`、`provider_timeout`、`provider_unreachable` 和 `generation_failed`。前端据此决定错误提示和是否提供重试；服务端错误文案不包含上游响应正文或密钥。

客户端断开连接时，框架关闭外层 SSE 生成器；`stream_conversation` 在 `finally` 中主动关闭 Provider 异步流，使 `httpx` 离开上游响应上下文。该传播行为有自动化测试覆盖，但模型供应商是否立即停止计算或计费取决于供应商。此阶段不保存历史，刷新后会话不会恢复。

## 环境隔离与调试

`APP_ENV` 明确区分 `development`、`test` 和 `production`，非法值会在启动配置校验时失败。复制 `.env.example` 后按实际部署修改；`test` 分支部署使用 `APP_ENV=test`，正式环境必须使用 `APP_ENV=production`，并配置准确的 `FRONTEND_ORIGIN`。服务端目前不提供故障注入接口，所有阶段零故障模拟都位于前端浏览器内，避免正式环境意外暴露调试端点。

## 使用火山方舟模型

1. 在仓库根目录的 `.env` 中配置 `LLM_PROVIDER=openai_compatible`、`LLM_API_KEY`、`LLM_API_BASE_URL`、`LLM_MODEL` 和 `LLM_REASONING_EFFORT`。密钥只写入服务端 `.env`。
2. `LLM_MODEL` 没有代码默认值，必须在 `.env` 中填写。当前本机使用 `kimi-k3-1-pro-261024`，已通过实际流式请求验证。切换模型时修改这个配置为其他可用模型 ID 或接入点 ID，然后重启服务端；无需修改 Python 代码。
3. `app/llm/openai_compatible.py` 发送系统提示词和历史消息，逐段读取方舟 SSE，只输出 `delta.content`；`app/conversations/service.py` 将其包装成前端统一事件。

密钥缺失、鉴权失败、模型不可用或上游调用失败时，统一事件流返回 `message.failed`。不要在代码或前端仓库中提交真实密钥。删除 `.env` 后，代码默认使用演示模型。

## 后续阶段

后续按“对话链路稳定性 → 会话与消息持久化 → 前端历史会话体验 → 模型上下文管理 → 开放使用前的运行保障”推进。测试随开发同步进行，身份与归属校验等保障在向其他用户开放前完成。详细任务、验收要求和进度见 [对话功能开发规划与进度](conversation-roadmap.md)。每完成一个任务，更新规划状态、实施记录及受影响的当前架构说明。语音、知识库和长期记忆另行规划。
