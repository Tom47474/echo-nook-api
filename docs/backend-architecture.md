# 服务端架构与目录说明

EchoNook API 是独立的 FastAPI 服务。当前版本提供身份管理、单窗口消息持久化、健康检查和文字流式回复。本机通过仓库根目录的 `.env` 选择火山方舟模型；代码也保留演示模型，供未配置真实模型的环境使用。服务端通过 SQLAlchemy 和 asyncpg 接入 PostgreSQL，由 Alembic 管理版本；前端先取得当前身份的唯一窗口，再以内部窗口 ID 查询消息和发起持久化 SSE。

## 目录与文件职责

| 路径 | 作用 |
| --- | --- |
| `app/main.py` | 创建 FastAPI 应用、配置 CORS、注册总路由，并在 lifespan 中检查数据库和释放连接池。 |
| `app/api/router.py` | 汇总各业务路由。 |
| `app/api/routes/health.py` | `/health` 健康检查。 |
| `app/api/routes/identity.py` | 游客初始化、账号注册/登录/退出；登录与退出只切换身份，不迁移消息。 |
| `app/api/routes/conversations.py` | 当前窗口、清空、消息分页和持久化流式接口；旧多会话接口标记为 deprecated。 |
| `app/core/identity.py` | Cookie 凭证签发与摘要、密码哈希、Origin 校验及请求身份解析。 |
| `app/core/config.py` | 定义配置项和默认值，从仓库根目录的 `.env` 加载本机配置；`APP_ENV` 仅接受 development、test、production。 |
| `app/core/dependencies.py` | 从当前 FastAPI 实例读取实际配置，避免测试应用和默认应用混用设置。 |
| `app/conversations/schemas.py` | 对话请求和历史消息的数据格式与长度限制。 |
| `app/conversations/models.py` | 身份、凭证、窗口、消息和模型调用 ORM 模型及数据库约束。 |
| `app/conversations/persistence.py` | 包装模型 SSE，节流保存助手草稿；终态事件前提交调用记录，并以取消安全的清理任务保存停止状态和 usage。 |
| `app/conversations/repository.py` | 唯一当前窗口、清空、归属校验、消息游标和回合创建的 SQLAlchemy 查询。 |
| `app/conversations/summary.py` | 长对话滚动摘要、并发边界和清空版本校验。 |
| `app/conversations/call_records.py` | 保存真实模型调用状态、耗时和供应商用量。 |
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
| `docs/conversation-technical-decisions.md` | 阶段零至当前任务的前后端技术难点、具体解决方案、代码定位和遗留边界；后续每完成一个路线图任务同步更新。 |
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

路由不解析模型供应商返回格式，具体模型适配器也不拼装 FastAPI 响应。对话服务只依赖 `LLMProvider`。`DatabaseManager` 归 FastAPI 应用生命周期所有，请求通过依赖获取独立 Session；变更接口显式提交，异常由 Session 上下文回滚。全部 ORM 模型位于 `app/conversations/models.py`，查询集中在 `repository.py`；持久化流式接口先提交用户消息和助手占位消息，再加载数据库历史并调用模型。完整字段、外键、索引和删除规则见 [PostgreSQL 本地开发与测试指南](database.md)。

## 数据模型

`principals` 统一表示游客和账号；游客没有用户名和密码哈希，账号保存小写用户名与 scrypt 哈希。`identity_sessions` 只保存随机 Cookie 的 SHA-256 摘要、身份 ID 和过期时间，身份删除时级联清除凭证。

`conversations` 使用 UUIDv7 主键，包含标题、创建/更新时间、可空的 `owner_id`、`is_current` 和 `content_epoch`。`owner_id` 外键关联游客或账号身份并用于授权；可空仅为无可信归属的旧数据保留。`is_current=true` 的条件唯一索引保证每个身份最多一个当前窗口，`content_epoch` 阻止清空前的旧摘要回写。

`messages` 使用 UUIDv7 主键，通过外键归属会话，包含 `user`/`assistant` 角色、正文、生成状态、会话内顺序、可选错误码以及创建/更新时间。状态允许 `pending`、`streaming`、`completed`、`failed`、`cancelled`。数据库强制同一会话内 `sequence` 唯一且非负；删除会话会级联删除消息。

`model_calls` 保存真实模型调用的目的、状态、模型名、耗时、token 用量、结束原因及调用时身份快照。它不保存提示词、回复正文或密钥。清空当前窗口的消息时保留调用记录；删除整个内部窗口时按外键级联删除。

当前不根据 `model_calls` 对游客或登录账号实施次数、Token、分钟频率或并发额度；这些记录只供内部成本观察和故障排查。调用接口仍依靠身份归属校验、输入长度与上下文预算保护数据和模型边界。

当前页面只获取唯一窗口并清空其中的消息；旧创建、列表、单会话删除接口暂时兼容旧客户端。流式接口保存完整用户/助手回合、恢复数据库上下文，并持久化助手草稿和最终状态。

## 当前接口契约

`GET /health` 返回 `{"status":"ok"}`。

`GET /api/v1/conversations/current` 原子取得或创建当前身份的唯一窗口。`DELETE /api/v1/conversations/current/messages` 清空当前身份的消息和摘要，调用记录保留。

旧 `POST /api/v1/conversations` 接受可选的 `title`，创建成功返回 201 和会话对象。标题默认“新对话”，会去除首尾空格，长度为 1 至 200；当前前端不再调用。

`GET /api/v1/conversations` 使用 `offset`（默认 0）和 `limit`（默认 20，最大 100），返回 `items`、`total`、`offset`、`limit`。结果按 `updated_at DESC, id DESC` 排列，UUID 作为时间相同情况下的稳定排序键。

`GET /api/v1/conversations/{id}` 仍供旧版客户端按 ID 读取，非本人或不存在时返回结构化 404。当前前端改用 `GET /api/v1/conversations/current`，不再提供会话直达链接。

`GET /api/v1/conversations/{id}/messages` 使用 `limit`（默认 50，最大 100）和可选 `before_sequence`。接口从最新消息向前取一页，但 `items` 始终按 `sequence` 正序返回；`has_more` 和 `next_before_sequence` 指示下一页。

前端 S3-3 首次请求 `limit=100`，用户滚到顶部或点击按钮后用 `before_sequence` 每次请求更早 50 条。前端按消息 ID 去重、修复跨页回合关系，并在插入后补偿滚动高度；服务端接口及数据库结构在本阶段未变。

前端 S3-4 在用户接近消息列表底部时平滑跟随流式新内容；向上阅读时保持位置，并在消息区水平居中、输入框上方提供向下箭头，可平滑回到底部。分页高度补偿和切换会话后的首次定位仍即时完成。此行为只涉及前端消息列表，服务端 SSE 协议不变。

`DELETE /api/v1/conversations/{id}` 成功返回 204，并由数据库级联删除消息。消息查询或删除遇到不存在的会话时返回 404，`detail.code` 为 `conversation_not_found`。UUID 格式或分页参数非法时返回 422。

`POST /api/v1/conversations/{id}/messages:stream` 只接受 `{"message":"..."}`，额外传入 `history` 会返回 422。服务端锁定会话，连续分配用户与助手两个 `sequence`，以 `user/completed` 和 `assistant/streaming` 原子创建回合，然后读取当前问题之前的数据库记录，按完整回合裁剪为最多 20 条上下文，再调用模型。历史查询保留终态助手的空正文，业务层才能判断最后一次尝试是否为空；失败或取消的非空正文附加内部中断标记，空回复只保留用户问题。多个助手终态属于同一用户消息时以最后一次为准；当前“再次提问”创建新的用户/助手回合，不把相同问题自动合并。会话不存在时返回结构化 404。

正常完成或失败时，持久化层先保存助手终态及 `model_calls`，再向浏览器发送 `message.completed` 或 `message.failed`。这是终态协议的一部分：前端收到完成事件会立即取消响应体，如果把调用记录推迟到事件之后，服务端任务可能先被取消而留下“有 completed 消息、没有 usage”的不一致。主动停止或网络断开没有终态事件，服务端将关闭 Provider、保存 `cancelled` 消息和记录模型调用组合成独立的取消安全清理任务；调用记录写入失败只记资源 ID 错误日志，不记录正文、Cookie 或密钥，也不改变用户已收到的回答。

S4-3 由 `0003_conversation_summary` 在 `conversations` 表增加 `summary` 和 `summary_through_sequence`。真实模型模式下，超过最近 10 个回合的旧记录按每次最多 3 回合滚动压缩；摘要覆盖边界之前的原文不再重复送入主模型。摘要放在系统背景信息中，近期消息仍按 S4-1 预算和 S4-2 回合规则选择。摘要生成或保存失败时使用原文继续回答；并发更新以旧覆盖边界和 `content_epoch` 为条件，避免旧请求覆盖新摘要或在清空后回写。演示模型不生成摘要。当前数据库应统一升级到 head `0006_single_window`。

S4-4 由 `0004_model_calls` 增加 `model_calls` 表，按 `purpose=summary|reply` 区分两次真实模型调用。方舟流式请求设置 `stream_options.include_usage=true`，适配器解析可能出现在 `choices=[]` 尾块中的 `usage`，同时读取模型名和 `finish_reason`；模型流关闭时记录耗时。结束状态按服务端实际结果保存为 `completed/failed/cancelled`。供应商未返回的 token 字段及结束原因保持 `NULL`，不使用估算值或推断值替代；演示 Provider 不生成真实调用记录。调用记录不包含密钥和对话正文，写入失败不会修改对话消息的终态。当前 schema head 为 `0006_single_window`。

S5-1 增加 `principals`（游客/账号）和 `identity_sessions`（随机 Cookie 的哈希）表。浏览器先调用 `/api/v1/identity/bootstrap`；注册、登录、退出由 `/api/v1/identity/*` 处理。所有会话 REST 与 SSE 接口从 Cookie 解析身份并按 `owner_id` 校验；对非本人会话返回 404。当前登录和退出只切换到各自独立的窗口，不迁移消息；`model_calls` 的调用时用量归属快照保持不变。前端不展示 token。生产环境需要同站 HTTPS Cookie 部署，服务端允许配置的前端 Origin 携带凭证；新代码需要迁移到 `0006_single_window`。

新流首先发送 `message.started`，携带两条消息的 UUID 和 sequence，供前端把临时消息与数据库记录对齐。正文仍通过 `message.delta` 发送；`message.completed` 发送前保存完整正文和 `completed`，`message.failed` 发送前保存部分正文、`failed` 和结构化 `error_code`。客户端中断或外层生成器关闭时保存 `cancelled`。

草稿不是逐 token 写库：收到新分片时，若累计新增 200 字符或距上次保存达到 1 秒，就使用独立短事务更新 `streaming` 正文。模型流期间不长期持有数据库连接。进程被强制终止时，可能丢失最近一个检查点之后的草稿；下次单进程应用启动会把遗留的 `assistant/streaming` 更新为 `failed`，错误码为 `generation_interrupted`。

同一会话的并发发送通过 PostgreSQL 行锁串行分配消息顺序，提交用户消息后立即释放锁，不在模型流期间持有数据库事务。这样既避免 sequence 冲突，也避免慢模型长期占用锁。

阶段二初版没有身份认证；S5-1 起所有会话接口都按 Cookie 身份过滤，`owner_id` 已用于授权。页面当前只使用唯一窗口接口，旧多会话路由暂供旧版客户端兼容。

`POST /api/v1/conversations/messages:stream` 接受：

```json
{
  "message": "你好",
  "history": [{"role": "user", "content": "我们来聊聊音乐"}]
}
```

响应的 `Content-Type` 是 `text/event-stream`。服务端依次返回 `message.delta` 事件及一个 `message.completed` 事件；生成失败时返回 `message.failed`，不会再发送 `completed`。前端用 `fetch` 的流读取器解析事件。历史最多 20 条，每条消息和本次输入最多 4000 字符。S4-1 进一步按可配置窗口、输出预留和安全余量控制估算输入预算；单独超预算的本次问题在写库前返回 422 `input_budget_exceeded`。方舟请求显式设置 `max_tokens`；供应商实际返回的 token 用量写入 `model_calls`，未返回的值保持 `NULL`。

当前前端 `http-reply-provider.ts` 只向带窗口 ID 的接口提交本次 `message`，不再提交 `history`。服务端从数据库选择最多 20 条历史并处理失败或取消回复的中断标记。用户停止、登录、退出或离开页面时，前端会取消当前 HTTP 请求。

`message.failed` 的 `code` 当前包括 `provider_not_configured`、`provider_auth_failed`、`model_not_available`、`provider_rate_limited`、`provider_timeout`、`provider_unreachable` 和 `generation_failed`。前端据此决定错误提示和是否提供重试；服务端错误文案不包含上游响应正文或密钥。

客户端断开连接时，框架关闭外层 SSE 生成器；`stream_conversation` 在 `finally` 中主动关闭 Provider 异步流，使 `httpx` 离开上游响应上下文。该传播行为有自动化测试覆盖，但模型供应商是否立即停止计算或计费取决于供应商。新接口保存用户问题、助手草稿和最终状态；刷新页面或切换身份后按对应窗口恢复。

上面的无会话 ID 接口是旧版兼容接口，仍由调用方提供 `history`，也不会写入数据库。当前前端已经切换到带会话 ID 的接口；旧接口只为兼容和独立调试暂时保留，后续评估后移除。

## 环境隔离与调试

`APP_ENV` 明确区分 `development`、`test` 和 `production`，非法值会在启动配置校验时失败。`development` 和 `production` 使用 `DATABASE_URL`，`test` 使用 `TEST_DATABASE_URL`，且测试数据库名必须以 `_test` 结尾。复制 `.env.example` 后按实际部署修改；`test` 分支部署使用 `APP_ENV=test`，正式环境必须使用 `APP_ENV=production`，并配置准确的 `FRONTEND_ORIGIN`。服务端目前不提供故障注入接口，所有阶段零故障模拟都位于前端浏览器内，避免正式环境意外暴露调试端点。

## 使用火山方舟模型

1. 在仓库根目录的 `.env` 中配置 `LLM_PROVIDER=openai_compatible`、`LLM_API_KEY`、`LLM_API_BASE_URL`、`LLM_MODEL` 和 `LLM_REASONING_EFFORT`。密钥只写入服务端 `.env`。
2. `LLM_MODEL` 没有代码默认值，必须在 `.env` 中填写。当前本机使用 `kimi-k3-1-pro-261024`，已通过实际流式请求验证。切换模型时修改这个配置为其他可用模型 ID 或接入点 ID，并核对 `LLM_CONTEXT_WINDOW_TOKENS`、`LLM_MAX_OUTPUT_TOKENS`、`LLM_TOKEN_SAFETY_MARGIN`，然后重启服务端；无需修改 Python 代码。
3. `app/llm/openai_compatible.py` 发送系统提示词和历史消息，逐段读取方舟 SSE，只输出 `delta.content`；`app/conversations/service.py` 将其包装成前端统一事件。

密钥缺失、鉴权失败、模型不可用或上游调用失败时，统一事件流返回 `message.failed`。不要在代码或前端仓库中提交真实密钥。删除 `.env` 后，代码默认使用演示模型。

## 后续阶段

后续按“对话链路稳定性 → 会话与消息持久化 → 前端历史会话体验 → 模型上下文管理 → 开放使用前的运行保障”推进。测试随开发同步进行，身份与归属校验等保障在向其他用户开放前完成。详细任务、验收要求和进度见 [对话功能开发规划与进度](conversation-roadmap.md)，设计原因和前后端实现见 [技术难点与解决方案](conversation-technical-decisions.md)。每完成一个任务，同步更新两份文档及受影响的当前架构说明。语音、知识库和长期记忆另行规划。

## 当前单窗口行为（2026-11-25）

`GET /api/v1/conversations/current` 由 Cookie 身份取得或原子创建唯一当前窗口；`DELETE /api/v1/conversations/current/messages` 在行锁事务中清空该身份消息和摘要，保留内部模型调用记录。`0006_single_window` 迁移合并同身份旧会话并建立条件唯一索引。登录与退出只切换身份窗口，不导入游客消息。前端页头管理身份，`use-current-conversation.ts` 处理唯一窗口的消息恢复、SSE、分页与清空；旧多会话路由暂供旧版客户端兼容。更完整的并发与迁移说明见 [技术难点文档](conversation-technical-decisions.md)。
