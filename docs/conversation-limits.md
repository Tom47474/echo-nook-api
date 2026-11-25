# 对话额度、频率、并发与模型超时配置

本文是 S5-2 的运行配置手册，说明 `.env` 中对话限额和模型超时变量的准确含义、相互关系、触发结果以及当前边界。实现原理和技术取舍见[对话功能技术难点与解决方案](conversation-technical-decisions.md#s5-2可配置额度并发租约与模型超时已完成)。

## 先区分四类控制

这些配置不是同一种“额度”：

| 类型 | 解决的问题 | 当前是否持久化 | 是否受总开关控制 |
| --- | --- | --- | --- |
| 每日主回复次数 | 单个游客或账号一天最多发起多少次真实主回复 | 是，读取 `model_calls` | 是 |
| 每分钟频率 | 防止单个身份短时间连续发送 | 否，当前进程内存 | 是 |
| 并发限制 | 防止同一时间存在过多模型流 | 否，当前进程内存 | 是 |
| 模型超时 | 防止上游连接或流永久占用资源 | 不适用 | 否，始终生效 |

当前每日额度按**主回复调用次数**计算，不是 Token、金额、火山方舟账号余额或供应商 RPM/TPM。供应商账号的总余额和硬限制仍由供应商控制；应用目前只有进程级全局并发保护，尚未实现全局每日金额、全局 Token 或套餐额度。

## 当前完整配置

```dotenv
# false 时不限额；true 时启用游客/账号额度、频率、并发和登录失败保护。
CONVERSATION_LIMIT_ENABLED=false

CONVERSATION_GUEST_DAILY_LIMIT=20
CONVERSATION_ACCOUNT_DAILY_LIMIT=100
CONVERSATION_GUEST_MINUTE_LIMIT=3
CONVERSATION_ACCOUNT_MINUTE_LIMIT=10
CONVERSATION_GLOBAL_CONCURRENCY_LIMIT=8

LOGIN_FAILURE_LIMIT=5
LOGIN_FAILURE_WINDOW_SECONDS=900

# 模型超时始终生效，不受 CONVERSATION_LIMIT_ENABLED 影响。
LLM_CONNECT_TIMEOUT_SECONDS=10
LLM_READ_TIMEOUT_SECONDS=120
LLM_REPLY_TIMEOUT_SECONDS=180
LLM_SUMMARY_TIMEOUT_SECONDS=60
```

所有变量都在服务启动时读取。修改 `.env` 后必须重启 API；只刷新浏览器不会生效。所有数量和秒数必须是大于 0 的值，非法值会使配置校验失败，服务拒绝启动。

## 总开关

### `CONVERSATION_LIMIT_ENABLED`

控制 S5-2 的业务限制：

- `false`：不检查游客/账号日额度、分钟频率、同身份在途生成、全局并发和登录失败次数。
- `true`：启用上述全部限制，并使用其余变量的配置值。

该开关**不关闭模型超时**，也不改变单次输入预算、最大输出 Token 和数据库授权校验。本地开发可以保持 `false`；需要验证限额的开发/测试环境以及对外开放的环境应显式设置为 `true`。

启用后，旧版 `POST /api/v1/conversations/messages:stream` 返回 410。该接口不保存可靠的身份用量归属，如果继续开放会绕过每日额度。当前前端使用的带会话 ID 接口不受影响。

## 游客和账号每日额度

### `CONVERSATION_GUEST_DAILY_LIMIT`

单个游客身份在一个 UTC 自然日内允许的主回复调用数。示例值 `20` 表示同一个游客身份当天最多累计 20 条 `purpose='reply'` 的模型调用记录。

### `CONVERSATION_ACCOUNT_DAILY_LIMIT`

单个登录账号在一个 UTC 自然日内允许的主回复调用数。示例值 `100` 表示该账号当天最多累计 100 条主回复调用记录；同一账号换浏览器或设备登录仍共享该额度。

每日额度的具体规则：

- 按 `model_calls.usage_owner_id` 的调用时身份快照归属，登录、退出不会转移旧额度。
- 按 UTC 00:00 重置，不按浏览器所在时区重置。
- 只统计 `purpose='reply'`；长对话的 `purpose='summary'` 不占“可发送次数”。
- 清空消息保留 `model_calls`，所以不能通过清空窗口重置额度。
- 真实 Provider 已开始且留下记录的失败或取消调用也可能计一次，因为上游可能已经产生费用。
- `demo` Provider 不创建真实模型调用记录，因此不会累计持久化日额度；分钟频率和并发限制仍然生效。
- 达到额度后返回 HTTP 429、错误码 `conversation_daily_limit`，并且在创建用户消息和助手占位消息之前拒绝。

当前没有配置每日 Token 上限。原因是中断、超时或供应商未发送 usage 尾块时，真实 Token 可能未知；直接把 `NULL` 当 0 会产生绕过。生产场景若需要按成本限制，应在次数限制之外增加 Token/金额的预占与结算机制，而不是把当前变量误认为供应商总额度。

## 每分钟频率

### `CONVERSATION_GUEST_MINUTE_LIMIT`

单个游客身份在任意连续 60 秒滑动窗口内允许开始的发送次数。示例值 `3` 表示第 4 次请求会被拒绝，直到最早一次请求离开 60 秒窗口。

### `CONVERSATION_ACCOUNT_MINUTE_LIMIT`

单个登录账号在任意连续 60 秒内允许开始的发送次数。示例值 `10`。登录账号按账号身份统计，不按浏览器标签页分别统计。

分钟限制不是“每到整分钟清零”，而是滑动窗口。达到阈值时返回 HTTP 429、错误码 `conversation_rate_limited`，并在 `Retry-After` 响应头中给出建议等待秒数。通过频率校验后，即使模型随后配置错误或生成失败，该次尝试也已经占用当前分钟窗口；日额度是否增加仍以是否产生真实主回复调用记录为准。

分钟窗口当前保存在 API 进程内存中：重启进程会清空，多个 worker 之间也不会共享。因此当前生产约束是单个 API worker；多进程或多实例部署前需要迁移到 Redis 等共享协调器。

## 并发保护

### 同身份并发

启用总开关后，同一个游客或账号同一时间只允许一个回复生成。该规则目前固定为 1，没有单独环境变量。第二个请求返回 HTTP 429、错误码 `conversation_in_progress`。

并发名额覆盖完整 SSE 生命周期，而不只是 FastAPI 路由返回前的准备阶段。正常完成、模型失败、总超时、用户停止或浏览器断开都会在 `finally` 中释放名额。

### `CONVERSATION_GLOBAL_CONCURRENCY_LIMIT`

当前单个 API 进程同时允许的模型主回复流数量。示例值 `8` 表示第 9 个不同身份的在途生成会被拒绝，返回 HTTP 429、错误码 `conversation_server_busy` 和 `Retry-After: 3`。

它不是：

- 每个用户的次数额度；
- 火山方舟账号的 RPM/TPM；
- 全部服务器共享的集群并发；
- 每天或每月可消费的模型余额。

摘要发生在同一次用户发送流程中，不会额外取得一份全局生成租约，但摘要本身仍会调用模型并占用该流程持有名额的时间。当前并发计数也是进程内状态，多 worker 部署时每个 worker 都会各自允许配置数量。

## 登录失败保护

### `LOGIN_FAILURE_LIMIT`

同一“客户端来源地址 + 小写用户名”组合在统计窗口内允许的密码校验失败次数。示例值 `5`：前 5 次返回正常的 401，下一次在执行昂贵的 scrypt 密码验证前返回 429 `login_rate_limited`。

### `LOGIN_FAILURE_WINDOW_SECONDS`

登录失败统计窗口，单位是秒。示例值 `900` 等于 15 分钟。窗口内登录成功会清除该组合的失败记录；参数格式错误、注册失败或服务端异常不计为密码失败。

登录失败记录当前也在进程内存中。生产部署若经过反向代理，需要确认应用取得的是可信客户端地址；不能直接信任外部用户可伪造的转发头。

## 模型超时

四个超时均以秒为单位，可以使用正数小数。它们始终生效，与 `CONVERSATION_LIMIT_ENABLED` 无关。

### `LLM_CONNECT_TIMEOUT_SECONDS`

默认 `10`。用于上游 HTTP 建连等待，同时作为写入请求和从连接池取得连接的超时边界。它保护 DNS、TCP/TLS 建连、请求发送或连接池等待长期卡住的场景，不限制整段回答时间。

### `LLM_READ_TIMEOUT_SECONDS`

默认 `120`。限制相邻两次上游网络数据到达之间最多等待多久。模型已经开始流式返回后，如果连续 120 秒没有任何新数据，httpx 会触发读取超时。

它不是回答总时长：只要模型持续在小于 120 秒的间隔内返回数据，一个回答可以持续更久，因此还需要主回复总超时。

### `LLM_REPLY_TIMEOUT_SECONDS`

默认 `180`。限制一次主回复从开始迭代模型流到结束的总时间，包括等待首个正文片段和接收后续全部片段。达到后服务端关闭 Provider 流，发送 `message.failed`，错误码为 `provider_timeout`，并把助手消息保存为 `failed`。前端可以再次提问。

当读取超时和总超时都配置时，先达到的那个生效。例如连续 120 秒没有数据时读取超时先触发；模型每隔几十秒持续返回但总时长超过 180 秒时主回复总超时触发。

### `LLM_SUMMARY_TIMEOUT_SECONDS`

默认 `60`。限制长对话摘要的一次模型调用总时间。摘要是增强功能；达到超时后不会让本次用户问题失败，而是保留旧摘要或回退到近期原文，随后继续主回复。摘要超时仍可能留下内部 `summary` 调用记录用于成本与故障分析。

## 相关但不是 S5-2 额度的模型配置

以下变量也会影响单次模型调用，但用途不同：

| 变量 | 作用 |
| --- | --- |
| `LLM_PROVIDER` | `demo` 使用演示回复；`openai_compatible` 调用真实模型。 |
| `LLM_API_BASE_URL` | OpenAI 兼容模型接口基础地址。 |
| `LLM_API_KEY` | 服务端模型密钥，不能提交 Git 或传到前端。 |
| `LLM_MODEL` | 模型或推理接入点 ID。 |
| `LLM_REASONING_EFFORT` | 模型推理强度。 |
| `LLM_CONTEXT_WINDOW_TOKENS` | 当前模型允许的上下文窗口。 |
| `LLM_MAX_OUTPUT_TOKENS` | 单次回答允许的最大输出 Token，同时用于输入预算预留。 |
| `LLM_TOKEN_SAFETY_MARGIN` | 本地输入 Token 估算的安全余量。 |

这三个 Token 预算变量控制“单次请求能放多少上下文”，不控制“用户一天能使用多少 Token”。供应商响应中的真实 usage 写入 `model_calls`，目前用于成本分析和身份归属，不直接作为日额度。

## 常见配置场景

### 本地开发不限额

```dotenv
CONVERSATION_LIMIT_ENABLED=false
```

其余额度值可以保留；开关关闭时不会执行。四个模型超时仍生效。

### 测试环境验证拒绝行为

```dotenv
CONVERSATION_LIMIT_ENABLED=true
CONVERSATION_GUEST_DAILY_LIMIT=3
CONVERSATION_ACCOUNT_DAILY_LIMIT=5
CONVERSATION_GUEST_MINUTE_LIMIT=2
CONVERSATION_ACCOUNT_MINUTE_LIMIT=3
CONVERSATION_GLOBAL_CONCURRENCY_LIMIT=2
LOGIN_FAILURE_LIMIT=3
LOGIN_FAILURE_WINDOW_SECONDS=60
```

低阈值适合人工验证 429、`Retry-After`、前端错误文案和并发释放。测试完成后恢复目标环境配置并重启服务。

### 当前单进程开放环境

可以以 `.env.example` 的 20/100 次作为保守起点，但它们只是初始产品限制，不是根据供应商真实价格计算出的正式运营额度。开放后应通过 S5-3 日志观察主回复用量、摘要比例、失败率和高峰并发，再调整阈值。

正式场景最终建议同时具备：供应商硬限制、应用全局日/月成本上限、游客/账号或套餐的次数与成本额度、分钟 RPM/TPM、并发限制，以及 usage 缺失时的保守结算。当前实现只完成其中的身份次数、频率、单进程并发和超时保护。

## 错误响应速查

| HTTP | `detail.code` | 含义 | 是否立即重试 |
| --- | --- | --- | --- |
| 429 | `conversation_daily_limit` | 当前身份 UTC 当日主回复次数已用完 | 否，等待次日或调整配置 |
| 429 | `conversation_rate_limited` | 当前身份最近 60 秒发送过多 | 按 `Retry-After` 等待 |
| 429 | `conversation_in_progress` | 同一身份已有回复生成中 | 等待完成或先停止 |
| 429 | `conversation_server_busy` | 当前 API 进程全局生成已满 | 稍后重试 |
| 429 | `login_rate_limited` | 登录失败次数达到窗口阈值 | 按 `Retry-After` 等待 |
| 410 | `legacy_stream_disabled` | 启用额度后旧无归属流接口被关闭 | 改用带会话 ID 的接口 |
| SSE | `provider_timeout` | 上游读取或主回复总时间超限 | 可以重新提问 |

## 修改配置后的检查

1. 修改目标环境的服务端 `.env` 或部署平台环境变量。
2. 重启 API 进程。
3. 查看启动日志，确认没有 Pydantic 配置校验错误。
4. 测试环境用较小额度验证 429 和 `Retry-After`。
5. 确认停止、断流和超时后可以再次发送，说明并发租约已释放。
6. 正式环境确认只运行一个 API worker；多实例前先建设共享限额存储。

不要把真实 API Key、数据库密码或生产额度策略提交到仓库中的 `.env.example`。示例文件只提供安全的字段结构和开发默认值。
