# 服务端架构与目录说明

EchoNook API 是独立的 FastAPI 服务。当前版本提供健康检查和文字流式回复。本机通过仓库根目录的 `.env` 选择火山方舟模型；代码也保留演示模型，供未配置真实模型的环境使用。服务端已通过 SQLAlchemy 和 asyncpg 接入 PostgreSQL，并由 Alembic 管理版本；会话和消息表已经建立，但当前接口尚未读写它们，前端仍需在请求中传入近期对话历史。

## 目录与文件职责

| 路径 | 作用 |
| --- | --- |
| `app/main.py` | 创建 FastAPI 应用、配置 CORS、注册总路由，并在 lifespan 中检查数据库和释放连接池。 |
| `app/api/router.py` | 汇总各业务路由。 |
| `app/api/routes/health.py` | `/health` 健康检查。 |
| `app/api/routes/conversations.py` | 对话 HTTP 接口，负责校验后的请求与流式响应封装。 |
| `app/core/config.py` | 定义配置项和默认值，从仓库根目录的 `.env` 加载本机配置；`APP_ENV` 仅接受 development、test、production。 |
| `app/conversations/schemas.py` | 对话请求和历史消息的数据格式与长度限制。 |
| `app/conversations/models.py` | 会话和消息 ORM 模型、角色/状态定义、顺序与数据约束。 |
| `app/conversations/service.py` | 组装消息、设置角色提示词、把文本片段编码为 SSE 事件。 |
| `app/db/base.py` | SQLAlchemy 声明式模型和 Alembic 共享的元数据基类。 |
| `app/db/session.py` | 校验连接地址、创建异步 Engine/Session、连接检查及连接池释放。 |
| `app/db/dependencies.py` | 为 FastAPI 请求提供应用级数据库管理器和请求级异步 Session。 |
| `app/llm/base.py` | 模型适配器统一接口 `LLMProvider`。 |
| `app/llm/demo.py` | 演示模型，逐段返回明确标识的示例文字。 |
| `app/llm/openai_compatible.py` | 火山方舟 Chat Completions 流式调用与响应解析。 |
| `app/llm/factory.py` | 根据配置选择演示或真实模型适配器。 |
| `tests/` | 对话事件格式、方舟流解析和模型不可用时的错误处理测试。 |
| `.env` | 本机环境配置，包含模型密钥和数据库凭据；被 Git 忽略。 |
| `.env.example` | 不含密钥的环境配置模板，默认使用演示模型。 |
| `.gitignore` | 排除 `.env`、虚拟环境和 Python 缓存等本机文件。 |
| `docs/database.md` | PostgreSQL 本地配置、连接、常用命令、备份、迁移约定和排错说明。 |
| `alembic/`、`alembic.ini` | 异步数据库迁移环境、模板和版本脚本；连接地址从 `.env` 读取。 |
| `pyproject.toml` | Python 版本、依赖和项目元数据。 |

`__init__.py` 标记 Python 包。`.venv/` 是本机依赖环境。项目只使用 `.env` 作为本机配置文件；`.venv/` 和 `.env` 都被 Git 忽略。

## 依赖方向

```text
HTTP 路由 → 对话服务 → LLMProvider 接口 → 演示/真实模型适配器
                  ↑
              请求 Schema

FastAPI lifespan → DatabaseManager → async Engine → PostgreSQL
HTTP 路由       → 请求级 AsyncSession ────────────────┘
```

路由不处理模型供应商返回格式。具体适配器不处理 HTTP 响应。对话服务只使用 `LLMProvider` 接口。`DatabaseManager` 归 FastAPI 应用生命周期所有，请求通过依赖获取独立 Session；业务层决定事务提交。会话和消息模型位于 `app/conversations/models.py`，S2-3 将增加存储服务与管理接口，S2-4 再让流式接口按会话 ID 加载历史。当前数据库操作方式见 [PostgreSQL 本地开发与测试指南](database.md)。

## 数据模型

`conversations` 使用 UUIDv7 主键，包含标题、创建/更新时间以及可空的 `owner_id`。`owner_id` 只为 S5-1 身份与归属校验预留，目前没有用户表、外键或授权作用，开放给其他用户前必须补齐身份绑定和历史数据归属策略。

`messages` 使用 UUIDv7 主键，通过外键归属会话，包含 `user`/`assistant` 角色、正文、生成状态、会话内顺序、可选错误码以及创建/更新时间。状态允许 `pending`、`streaming`、`completed`、`failed`、`cancelled`。数据库强制同一会话内 `sequence` 唯一且非负；删除会话会级联删除消息。

当前数据表只是持久化基础，现有接口行为没有变化：尚不会创建会话、保存消息或恢复历史。

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

`APP_ENV` 明确区分 `development`、`test` 和 `production`，非法值会在启动配置校验时失败。`development` 和 `production` 使用 `DATABASE_URL`，`test` 使用 `TEST_DATABASE_URL`，且测试数据库名必须以 `_test` 结尾。复制 `.env.example` 后按实际部署修改；`test` 分支部署使用 `APP_ENV=test`，正式环境必须使用 `APP_ENV=production`，并配置准确的 `FRONTEND_ORIGIN`。服务端目前不提供故障注入接口，所有阶段零故障模拟都位于前端浏览器内，避免正式环境意外暴露调试端点。

## 使用火山方舟模型

1. 在仓库根目录的 `.env` 中配置 `LLM_PROVIDER=openai_compatible`、`LLM_API_KEY`、`LLM_API_BASE_URL`、`LLM_MODEL` 和 `LLM_REASONING_EFFORT`。密钥只写入服务端 `.env`。
2. `LLM_MODEL` 没有代码默认值，必须在 `.env` 中填写。当前本机使用 `kimi-k3-1-pro-261024`，已通过实际流式请求验证。切换模型时修改这个配置为其他可用模型 ID 或接入点 ID，然后重启服务端；无需修改 Python 代码。
3. `app/llm/openai_compatible.py` 发送系统提示词和历史消息，逐段读取方舟 SSE，只输出 `delta.content`；`app/conversations/service.py` 将其包装成前端统一事件。

密钥缺失、鉴权失败、模型不可用或上游调用失败时，统一事件流返回 `message.failed`。不要在代码或前端仓库中提交真实密钥。删除 `.env` 后，代码默认使用演示模型。

## 后续阶段

后续按“对话链路稳定性 → 会话与消息持久化 → 前端历史会话体验 → 模型上下文管理 → 开放使用前的运行保障”推进。测试随开发同步进行，身份与归属校验等保障在向其他用户开放前完成。详细任务、验收要求和进度见 [对话功能开发规划与进度](conversation-roadmap.md)。每完成一个任务，更新规划状态、实施记录及受影响的当前架构说明。语音、知识库和长期记忆另行规划。
