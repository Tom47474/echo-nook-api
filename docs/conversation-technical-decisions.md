# 对话功能技术难点与解决方案

更新时间：2026-11-24。本文对应 [开发路线图](conversation-roadmap.md)，记录已经实现的前后端方案、关键代码、验证和边界。路径中的 `echo-nook-web` 指同级前端仓库。代码行为发生变化时，以实际代码和测试为准，及时修订本页，不能把历史方案写成当前实现。

## 如何维护

每完成一个路线图任务（不必等整个阶段结束），在相应阶段追加或更新：问题与约束、前后端各自怎样实现、关键接口和数据流、竞态/失败处理、对应代码与测试、已知限制。同步更新本文日期和路线图状态。改变既有行为时明确写出“原方案 → 当前方案”，避免读者照旧方案操作。这里解释设计原因；[服务端架构](backend-architecture.md)记录当前接口和目录，[数据库指南](database.md)记录操作命令，前端细节另见 [前端架构](../../echo-nook-web/docs/frontend-architecture.md)。不要写入密钥、数据库密码或真实对话正文。

### 后续任务的写作标准

后续每一项技术难点都按本页 S3-1“乐观消息怎样与数据库正式消息对齐”的详细程度撰写，不能只用一句话概括“使用某技术解决某问题”。至少写清以下内容：

1. **问题从何而来。** 给出实际操作场景、触发条件及不处理会导致的具体错误，例如切换会话后旧 SSE 分片进入新会话。
2. **数据如何一步步流动。** 从用户操作或 HTTP 请求开始，按发生顺序写出前端 Hook、请求适配层、服务端路由、数据库/模型以及响应事件各自做了什么；必要时给出请求、SSE、消息状态或序号的简短示例。
3. **核心代码为什么这样写。** 定位到真实文件、函数或接口，解释 ID、状态、事务提交、行锁、取消、版本号等关键选择的目的；说明前端与服务端的职责边界。
4. **失败与并发怎样处理。** 至少覆盖该任务实际相关的错误、取消、乱序或重入场景，并说明页面显示与数据库最终状态；没有覆盖的情况应明确列为限制。
5. **怎样验证、还剩什么。** 记录有针对性的测试或手工验收及结果，注明当前实现与后续阶段的边界。历史实现改变时写明旧行为和新行为，不让已废弃方案冒充当前代码。

写作时用具体例子说明状态变化，例如“临时用户 ID → `message.started` 中的正式 UUID → 刷新后读取同一条数据库消息”；关键流程可以使用短代码块或时序图。每个结论须能从当前代码或测试核对，不猜测供应商行为，也不把计划中的能力写成已实现。

## 当前端到端流程（完成 S3-1 后）

```text
浏览器初始化 → GET 会话列表 → GET 最近会话的最新 100 条消息
发送文本 → 没有会话则 POST 创建 → 本地先展示用户/助手占位
POST /api/v1/conversations/{id}/messages:stream，仅发送本次 message
服务端锁定会话 → 分配 sequence → 同一事务插入 user/completed 和 assistant/streaming
提交并释放锁 → 从数据库读取旧消息 → 组装完整回合 → 结束读事务
SSE message.started（正式 ID/sequence）→ 前端替换临时 ID
模型分片 → SSE message.delta → 前端追加正文，服务端周期性保存草稿
正常/失败/停止 → 服务端保存 completed/failed/cancelled → 前端呈现状态
```

浏览器调试注入的消息只存在内存中，不进入模型历史。旧的无会话 ID 流式接口仍存在，但当前前端使用带 ID 的持久化接口。

## 阶段零：环境隔离与调试工具（S0-1～S0-5）

### 难点：开发分支也需要调试球，而正式环境绝不能显示

Git 分支决定代码版本，不可靠地决定进程运行环境。调试组件保留在共享主线，前端 [page.tsx](../../echo-nook-web/src/app/page.tsx) 在服务端读取 `APP_ENV` 和 `ENABLE_CONVERSATION_DEBUG`：仅 `development` 或 `test` 且开关为 `true` 时把启用标志传入客户端。`production` 即使误设开关也不挂载调试入口。不能靠 URL 参数或浏览器存储开启。服务端 [config.py](../app/core/config.py) 用限定值校验 `APP_ENV`；测试服务器使用 `APP_ENV=test`，生产显式使用 `production`。`.env.example` 是无密钥模板，实际 `.env`/`.env.local` 是各机器的配置，不由 Git 分支自动切换。

具体判断发生在 Next.js 页面渲染时：`page.tsx` 计算 `debugEnabled = (APP_ENV 为 development/test) && (ENABLE_CONVERSATION_DEBUG 为 true)`，沿 `ChannelPage → ConversationPanel → useConversation` 传递。只有标志为真才渲染浮动球。假设开发者从 `master` 拉开发分支，代码仍包含调试工具；本地 `.env.local` 开启开关后能显示。假设正式部署也使用同一份代码，即使误写 `ENABLE_CONVERSATION_DEBUG=true`，由于 `APP_ENV=production`，标志仍为假。`NODE_ENV` 表示构建运行模式，测试服务器也可能运行 production 构建，不能用它代替业务环境。这里的门禁控制界面挂载，不构成阻止有权限修改代码的用户自行调用后端接口的安全边界。

### 难点：稳定复现 SSE 故障，不干扰真实接口

前端 [conversation-debug-panel.tsx](../../echo-nook-web/src/modules/conversation/components/conversation-debug-panel.tsx) 提供本地消息注入、批量消息、长文本、清空等操作；[debug-reply-provider.ts](../../echo-nook-web/src/modules/conversation/services/debug-reply-provider.ts) 包装真实流，模拟发送前阻止、部分内容后中断、缺少完成事件、慢速分片和服务商错误。故障由浏览器适配层制造，服务端没有公开故障注入端点。模拟断流只验证 UI 处理，不等于真实供应商一定立即停止计算或计费。停止操作仍通过真实请求取消路径验证。

Provider 包装层的调用次序是 `Hook → DebugReplyProvider → HttpReplyProvider → FastAPI`。例如“发送前阻止”在请求进入 HTTP Provider 之前抛出结构化网络错误，因此不会创建服务端消息；“慢速流”在每个事件前等待约 800ms，可观察思考状态和取消按钮；“部分内容后断流”在累计收到一定长度的 `delta` 后抛错，检查页面能否保留部分正文。选择的场景在一次发送开始时固定，途中更换下拉框不会改变已启动的请求。手动注入消息只修改 Hook 的本地消息数组，刷新后消失；真实发送的历史由服务端数据库提供，注入内容不会混入模型上下文。

### 验证和边界

阶段零检查了开发/测试启用与生产强制关闭构建，并通过服务端流关闭测试验证上游 Provider 会被 `aclose()`。生产部署仍须正确设置环境和跨域来源；环境门禁不代替未来的身份鉴权。

## 阶段一：对话链路稳定性（S1-1～S1-5）

### 难点：生成失败发生在收到部分正文之后

只显示一个“失败”会丢掉已经阅读的内容。前端 [use-conversation.ts](../../echo-nook-web/src/modules/conversation/hooks/use-conversation.ts) 在 `delta` 时累加正文；失败时保留正文并置 `failed`，将结构化错误交给 [message-item.tsx](../../echo-nook-web/src/modules/conversation/components/message-item.tsx) 展示。服务端 [service.py](../app/conversations/service.py) 将配置缺失、鉴权失败、模型不可用、限流、超时、不可达和一般异常编码成 `message.failed`，不在错误文案中回显上游密钥或响应正文。前端 [conversation-error.ts](../../echo-nook-web/src/modules/conversation/model/conversation-error.ts) 将配置、鉴权和模型不可用标为不可直接重试，其余可再次尝试。

以模型先输出“第一段”后超时为例：`HttpReplyProvider` 解析第一个 `message.delta`，Hook 找到当前助手消息并把文本追加到 `content`；服务端随后产生 `message.failed`，Provider 将 `code` 和安全的提示文案包装为 `ConversationRequestError`；Hook 的 `catch` 只改变该消息的 `status` 和 `error`，不清空 `content`。消息组件仍显示“第一段”，并在下方显示错误说明。服务端 S2-5 后也会把这段正文和 `failed/error_code` 落库，所以重新加载仍可看到残缺回答。若失败发生在第一个分片之前，正文为空，组件显示“未生成可展示的回复”。

错误码与恢复操作分开处理：401/403 映射鉴权失败，404 映射模型不可用，429 映射限流；超时、连接失败和其他上游异常分别归类。配置或权限问题通常需要修改服务端设置，直接重复发送不能解决；网络、限流、超时等可再试。UI 是否展示“再次提问”由错误的 `retryable` 属性决定，而服务端错误码始终可在持久化消息中保留。

### 难点：停止、断流与后续历史的语义

浏览器通过 `AbortController` 中止 `fetch`；[http-reply-provider.ts](../../echo-nook-web/src/modules/conversation/services/http-reply-provider.ts) 在消费者提前结束时取消响应读取器；服务端流生成器在 `finally` 关闭模型 Provider，真实适配器随之释放上游 HTTP 响应。单靠页面标成“已停止”不足以释放连接，因此前后端都处理关闭传播。模型供应商是否立即停止计费不由本项目保证。

实际停止路径依次是：用户点击停止 → Hook 对当前 `AbortController` 调用 `abort()` 并把本地 `streaming` 消息改为 `cancelled` → 浏览器终止流读取 → FastAPI 流生成器关闭 → 服务端在 `finally` 中 `aclose()` 模型流并尝试保存 `cancelled`。网络中断也会走资源关闭路径，但前端可能先显示本地网络错误；后续重新读取数据库消息时，以服务端实际终态为准。`reader.releaseLock()` 只释放 JavaScript 对读取器的占用，不能保证网络连接结束，因此提前退出时还调用 `reader.cancel()`。

在阶段一的纯内存实现中，前端按 `turnId` 保持完整问答；部分失败/取消的助手正文带内部“未完整生成”标记进入后续上下文，空回复只保留用户问题。接入数据库后，这项上下文选择移至服务端 [build_database_history()](../app/conversations/service.py)；浏览器不再上传 `history`。前端仍保留同步 `messagesRef`，使停止后立即发送不会读取 React 尚未提交的旧状态。

例如一个回合只有用户问题和半段已取消的回答，后续模型可以看到问题及半段回答，但半段回答附有系统注记，提示它不是完整结论；如果助手一个字都没有生成，就只带用户问题。按完整回合截取可避免上下文从孤立的助手回答开始。`messagesRef` 的作用是解决 React 批量状态更新：点击停止和立即发送可能发生在一次渲染提交之前，若新发送直接读取闭包里的 `messages`，会重新拿到尚标记为 `streaming` 的旧状态；同步 ref 在每次更新消息时先写入最新数组。

### 难点：重试语义随持久化而改变

S1-2 最初在内存里清空旧助手气泡并原地重新生成，不增加用户消息。S3-1 接入数据库后，服务端还没有覆盖旧回复的接口；继续原地改 UI 会导致刷新后旧回答重现。当前按钮改为“再次提问”，把原问题追加为新的持久化回合，保留旧记录。若今后要求真正原地重新生成，需要先设计服务端版本关系、历史选择和审计规则，不应仅在前端改气泡。

具体例子：旧回合在库中是 `user #4 / assistant #5`。点击当前按钮后，前端找到与 #5 同一 `turnId` 的原问题，再调用普通 `send`；服务端新建 `user #6 / assistant #7`。旧 #4/#5 不被改写，刷新后四条消息仍一致。阶段一内存版可以复用 #4/#5 的前端 ID，因为没有数据库；这个行为已经明确废弃。将来若产品要求“同一问题的多个候选回答”，需要确定原回答是否保留、哪条候选进入历史、如何标识版本，以及是否允许用户切换候选。

### 验证和边界

阶段一测试覆盖部分回答保留、结构化错误、主动停止和连接释放。当前流式消息的持久化终态由 S2-5 实现；断网时本地可先显示失败，数据库最终状态以服务端实际处理和后续重新加载为准。

## 阶段二：数据库与会话消息持久化（S2-1～S2-5）

### S2-1：异步连接、环境隔离与迁移

[Settings.active_database_url()](../app/core/config.py) 在 `APP_ENV=test` 时选 `TEST_DATABASE_URL`，其他环境选 `DATABASE_URL`；地址用 `SecretStr` 避免日志泄密。[DatabaseManager](../app/db/session.py) 验证 `postgresql+asyncpg`，测试环境额外要求库名以 `_test` 结尾；建立异步 Engine、Session 工厂，`pool_pre_ping` 检查失效连接，请求异常时回滚。FastAPI [lifespan](../app/main.py) 启动执行 `SELECT 1`，关闭释放连接池。Alembic 使用同一配置管理 schema；开发、测试库分别升级。这样连接错误在启动时暴露，测试不会意外写入开发库。

连接生命周期可以理解为“一个应用持有一个连接池，每次请求借一个 Session”：`create_app` 把配置保存在 `app.state`；lifespan 创建数据库管理器、执行 ping，然后把管理器交给依赖注入；路由通过 `get_db_session` 取得请求级 Session。写操作由路由显式 `commit()`，抛异常则上下文回滚；应用关闭统一 `dispose()`。`pool_pre_ping` 处理连接池里被 PostgreSQL 关闭的旧连接，不能替代启动时的 `SELECT 1`。测试库门禁核对的是 URL 中解析出的数据库名称，不在日志输出完整 URL。Alembic 迁移从应用配置取得地址，避免在迁移配置文件中重复保存密码；先升级数据库，再启动需要新表的服务代码。

### S2-2：模型约束和可扩展状态

[Conversation / Message](../app/conversations/models.py) 使用 UUIDv7。会话有标题、创建/更新时间和可空 `owner_id`；消息有角色、正文、`sequence`、状态、错误码和时间。数据库约束限制角色与状态、禁止负序号、保证 `(conversation_id, sequence)` 唯一；删除会话通过 `ON DELETE CASCADE` 清除消息。`owner_id` 只是未来身份阶段的预留字段，当前没有用户隔离效果。`0002_conversation_messages` 迁移建立表和约束，不能只定义 ORM 而不执行迁移。

`id` 解决跨请求定位具体会话/消息的问题，`sequence` 解决“同一会话中谁先谁后”的问题，两者用途不同。时间戳适合把最近活跃会话排到顶部，但两条消息可能具有相同数据库时间，不能单独依赖时间戳分页；因此会话内使用整数 `sequence` 和唯一约束。UUIDv7 可以在应用侧生成，且大体随时间递增。`status` 描述助手消息从占位到终态的生命周期：先 `streaming`，最后只能呈现 `completed`、`failed` 或 `cancelled`；`error_code` 为失败恢复提示提供机器可读依据。数据库检查约束和唯一约束是最后一道防线，即使 Python 并发逻辑出错，也不能悄悄写入重复序号。

### S2-3：管理接口和消息分页

[路由](../app/api/routes/conversations.py) 提供创建、列表、查消息、删除；[Repository](../app/conversations/repository.py) 集中 SQL 查询。会话按 `updated_at DESC, id DESC` 稳定排序，用 `offset/limit`；消息用 `before_sequence` 取比游标更早的一页，倒序多取一条判断 `has_more`，响应再反转为阅读正序。不存在的资源返回结构化 `conversation_not_found`；删除依赖数据库级联。当前列表最多请求 100 条，尚未实现列表继续分页；未完成 S5-1 前不能对外开放为多用户接口。

消息分页的例子：数据库有 `sequence=0…120`，客户端第一次请求 `limit=50`，查询按倒序拿 `120…70` 共 51 条；返回最新 50 条 `71…120`（给 UI 正序），因为多取到 #70，所以 `has_more=true`，`next_before_sequence=71`。下一次传 `before_sequence=71`，条件为 `<71`，便得到更早的 `21…70`，不重复 #71。会话列表另外使用 `updated_at` 排序，时间相同再用 ID 打破平局；二者分页方式不同。删除操作返回 204 后，消息由外键级联删除；删除或读取不存在的会话返回稳定的 404 错误码，便于前端区分资源消失和网络故障。

### S2-4：数据库成为模型历史的来源

新的 `POST /api/v1/conversations/{id}/messages:stream` 只接受 `message`，Schema 的 `extra="forbid"` 拒绝客户端传入 `history`。[start_turn()](../app/conversations/repository.py) 通过 `SELECT ... FOR UPDATE` 锁住会话行，在同一事务内计算下一顺序并创建用户/助手消息；提交后才调用慢模型，避免长期占锁。只查询本次用户消息之前、非空且已进入终态的消息；[build_database_history()](../app/conversations/service.py) 按完整回合整理，最多 20 条、单条最多 4000 字符，并标记不完整的助手回答。历史查询后显式结束读事务，模型流期间不占用该数据库连接。旧无 ID 接口暂留作兼容，不能将它误认为持久化路径。

这里有两个独立的事务边界。第一段写事务锁定会话、取最大顺序、插入两条消息、更新会话活跃时间并提交；它结束后另一个请求才可分配后续顺序。第二段读取历史的 SELECT 会自动开启事务，路由组装完 `StreamMessageRequest` 后显式 `rollback()` 结束这个只读事务；慢速模型流随后才开始。若把数据库事务留到模型生成完毕，一个请求可能占连接几十秒，且阻塞同会话写入。历史查询使用 `sequence < 本次用户序号`，所以不会把当前问题或空的助手占位重复塞进历史；最后才将本次 `message` 附加到给模型的消息数组。

构造上下文时先从数据库扫描最近若干条可用消息，再按用户消息切成回合。一个回合中有用户问题和助手回答；若助手失败/停止且保存了部分正文，就在发给模型的正文末尾加“未完整生成”注记，页面展示的原文不变。最多选择 20 条，并保证取舍以回合为单位。这里按字符数和消息条数限制，尚未按模型 token 预算计算，属于后续 S4-1。

### S2-5：SSE 正文、终态与崩溃恢复

同一事务创建 `user/completed` 和空的 `assistant/streaming`，避免只落一半回合；会话行锁和唯一约束共同保护并发 `sequence`。SSE 先发 `message.started` 返回两条正式消息的 UUID 和顺序，再发 `message.delta`。 [stream_persisted_conversation()](../app/conversations/persistence.py) 累加正文：处理新分片时，新增至少 200 字符或距上次保存至少 1 秒才写草稿。每次通过独立短 Session 更新，慢模型流不长期占连接。完成或模型失败时，先保存完整/部分正文及 `completed`/`failed` 和错误码，再发送终态事件；仓储只更新仍为 `streaming` 的行，避免覆盖已结束消息。

并发例子：A、B 同时向同一会话发送。A 先锁住会话，看到最大序号 8，插入用户 #9 和助手 #10；B 必须等 A 提交，之后看到 10，再插入 #11/#12。两次模型生成可以并行，但序号分配不会交叉。数据库唯一约束额外防止错误实现产生重复 #9。

草稿保存例子：模型每次吐出几个字时，立即逐字写库会产生大量事务；当前包装器把 `delta` 累加到内存。某次新分片到来后，如果自上次保存新增至少 200 字符，或距上次保存至少 1 秒，就用新的短 Session 锁定助手行、保存当前完整草稿并提交。计时条件是在收到新分片时判断，没有后台定时写库。收到 `message.completed` 时无论是否达到阈值都保存完整正文和 `completed`，然后才把完成事件交给浏览器；`message.failed` 同样先保存部分正文、`failed` 和错误码。这样前端收到终态时，数据库已经完成对应写入。

客户端断开时，`finally` 关闭模型流，并用 `asyncio.shield` 尽量完成最后一次 `cancelled` 写入。进程强制退出无法执行 `finally`，下一次单进程启动由 [recover_interrupted_generations()](../app/conversations/persistence.py) 把遗留 `assistant/streaming` 改为 `failed`，错误码 `generation_interrupted`。最后一次检查点之后的少量正文可能丢失。若改多实例部署，需先引入生成任务租约/归属，避免一个新实例把其他实例正在生成的消息误判为失败。

取消与进程崩溃并不相同：正常断开时 Python 的生成器通常能运行 `finally`，保存已收到正文并把助手标记 `cancelled`；`asyncio.shield` 避免请求取消信号同时打断最后一次写库。强制杀进程时任何 `finally` 都不会执行，消息可能停留在 `streaming`，启动恢复才会将其改为 `failed/generation_interrupted`。恢复只修复状态，不可能重建尚未到达检查点的正文，也不会向已断开的浏览器补发 SSE。

### 验证和边界

阶段二的服务端测试覆盖迁移、唯一约束、级联删除、分页、并发顺序、历史来源、草稿检查点、完成、失败、停止及启动恢复。S2-5 完成时 28 项测试通过。身份鉴权、token 预算和多实例租约尚未实现。

## 阶段三：前端历史会话体验

### S3-1：服务端会话与浏览器状态同步（已完成）

前端 [conversation-api.ts](../../echo-nook-web/src/modules/conversation/services/conversation-api.ts) 集中调用会话 REST API，把 `created_at`、`conversation_id`、`error_code` 等 DTO 映射为页面模型，并根据相邻消息还原 `turnId`。页面初始化读取会话列表，自动选择最近会话、查询最近 100 条；[conversation-list.tsx](../../echo-nook-web/src/modules/conversation/components/conversation-list.tsx) 提供新建、切换、删除，删除前确认。没有活动会话时，首次发送先创建会话。由于服务端列表按更新时间排序，生成结束会刷新列表，让活动会话回到顶部。

浏览器模型和数据库 JSON 有不同命名：服务端返回 `conversation_id/created_at/error_code`，组件使用 `conversationId/createdAt/error`。转换集中在 `conversation-api.ts`，因此消息气泡和列表不需要处理 HTTP 格式。恢复消息时，遇到用户消息把其正式 ID 设为当前 `turnId`，后续助手消息关联同一回合；失败消息依据 `error_code` 恢复提示。初始化的顺序是获取列表 → 选择服务端排序第一条 → 请求该会话最新 100 条 → 显示。列表为空时不预先创建空会话，首次发送才调用创建接口。

发送时 [use-conversation.ts](../../echo-nook-web/src/modules/conversation/hooks/use-conversation.ts) 先放入临时用户和助手消息，避免等待网络时界面空白。前端 [HttpReplyProvider](../../echo-nook-web/src/modules/conversation/services/http-reply-provider.ts) 向带会话 ID 的接口仅发送本次 `message`，逐个解析 SSE：网络分块可能截断事件，因此缓冲到空行才解帧。收到 `message.started` 后以正式 UUID 和 `sequence` 替换临时 ID；`delta` 追加正文，`completed` 标记结束，`failed` 转成结构化异常并保留已有正文。

#### 乐观消息怎样与数据库正式消息对齐

用户按发送后，Hook 先用 `crypto.randomUUID()` 造两条临时记录：用户消息立即出现在右侧，空助手消息显示“正在思考”。此时服务端可能尚未提交，两个 ID 只是浏览器渲染用的临时标识。服务端在同一事务中创建正式的用户/助手记录后，第一个 SSE 事件 `message.started` 携带 `user_message_id`、`assistant_message_id` 及两个 `sequence`。Hook 逐条匹配临时 ID：把用户消息换成正式用户 ID，把助手消息换成正式助手 ID，两条消息的 `turnId` 统一设为正式用户 ID，并记录序号；后续 `delta` 依据正式助手 ID 追加文本。这样刷新后从数据库加载的消息与刚刚显示的记录使用相同身份，不会出现“本地一条、数据库又一条”的重复。若请求在 `started` 前失败，本地临时消息会标失败；是否已经写库取决于服务端处理到哪一步，刷新后以数据库为准。

#### SSE 为什么需要缓冲和结构化事件

旧 Provider 只返回 `AsyncIterable<string>`，无法告诉 Hook 数据库正式 ID 或确切完成状态。现在事件包括 `started`、`delta`、`completed`，服务端的 `message.failed` 被转换为包含错误码的异常。网络一次 `reader.read()` 可能只拿到 `event: message.del`，下一次才拿到 `ta\ndata: ...\n\n`，也可能一次包含多个事件；Provider 把未读完的字节解码后保留在 `buffer`，只在空行界定完整 SSE 帧时解析 JSON。消费者提前退出时取消读取器，缺失完成事件且并非主动停止时抛 `stream_interrupted`，不会把半截回复误标成功。

切换会话时，Hook 先 `abort()` 旧 SSE，清空旧页面消息并加载目标会话。取消信号不能保证已经排队的 Promise 不执行，所以每个流还保存生成版本号，并在写入前核对当前活动会话 ID；两个条件都满足才更新页面。消息加载另有 `loadVersionRef`，避免先请求 A、后请求 B，却因 A 晚返回而显示错会话。列表刷新用 `listVersionRef` 防止旧列表响应覆盖刚创建或删除的项目。删除当前会话成功后选择剩余第一项；失败时显示错误并保留服务端返回前已有数据。

#### 三种异步竞态分别怎样处理

1. **旧生成流污染新会话。** A 正在生成，用户切换 B；Hook 先 `abort()`，并让旧生成版本失效。每个事件更新前同时检查“仍是本次版本”和“活动会话仍是 A”。即使 A 的网络分片或异常回调在 B 已加载后才到，也不能写 B 的消息数组。`finally` 清理控制器时也只清理自己创建的控制器。
2. **消息请求乱序。** 用户快速点击 A 再点击 B，B 的查询可能先返回。每次加载创建 `AbortController` 并增加 `loadVersionRef`；响应到达时核对版本及活动 ID，A 的晚到数据被丢弃。切换时先清空屏幕旧消息，避免 B 加载中继续展示 A。
3. **列表刷新覆盖新建/删除。** 生成完会刷新按 `updated_at` 排序的列表；若刷新请求尚未返回就创建或删除会话，旧列表响应可能把刚删的会话重新显示。新建/删除增加 `listVersionRef`，刷新结果只有版本仍一致时才提交到 React 状态。

删除使用服务端 `DELETE`，成功后从本地列表去掉该项；若它是活动会话，选择剩余第一项并加载消息。删除按钮先确认，因为数据库会级联删除全部消息。请求失败时保留列表并显示错误，不假装删除成功。

前端适配后的自动化测试覆盖 DTO 映射、SSE 跨块解析和正式 ID、部分失败、停止、切换隔离、删除后选择下一会话；S3-1 完成时前端 12 项测试、lint、production build 通过，服务端 28 项测试通过。当前刷新会自动打开最近会话；URL 指定恢复是 S3-2，超过 100 条消息的向上分页是 S3-3，阅读时的自动滚动优化是 S3-4。服务端还没有原地重新生成接口，因此当前“再次提问”会追加回合。
