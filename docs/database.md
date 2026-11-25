# PostgreSQL 本地开发与测试指南

本文说明 EchoNook API 的本地 PostgreSQL 配置、日常使用和排错方式。命令默认在 macOS、Homebrew PostgreSQL 18 环境执行。

## 当前状态

截至 2026-11-25，本机数据库基础设施与单窗口结构已准备完成：

| 项目 | 当前状态 |
| --- | --- |
| PostgreSQL | Homebrew PostgreSQL 18.7，监听 `127.0.0.1:5432` |
| 应用账号 | `echo_nook`，普通账号，不是超级用户 |
| 开发数据库 | `echo_nook_dev` |
| 测试数据库 | `echo_nook_test` |
| 业务表 | `principals`、`identity_sessions`、`conversations`、`messages`、`model_calls`；当前迁移版本为 `0007_s5_limits` |
| 代码接入 | 已接入 SQLAlchemy 2.1、asyncpg 0.32 和 Alembic 1.20 |

开发库和测试库运行在同一个 PostgreSQL 服务中，但数据彼此隔离。日常本地运行使用 `echo_nook_dev`；自动化测试和可能清空数据的测试操作只能使用 `echo_nook_test`。

`model_calls` 只保存模型调用元数据，`purpose` 区分 `summary` 与 `reply`，`status` 记录服务端观察到的完成、失败或取消。`prompt_tokens`、`completion_tokens`、`total_tokens` 为供应商实际返回的用量；`NULL` 表示本次调用未取得该值，不等于 0。DBeaver 或 `psql` 可用以下只读查询检查最近调用：

```sql
SELECT created_at, purpose, status, model, duration_ms,
       prompt_tokens, completion_tokens, total_tokens, finish_reason
FROM model_calls
ORDER BY created_at DESC
LIMIT 20;
```

该表不保存提示词、回复正文或密钥；删除会话时调用记录由外键级联删除。摘要调用和主回复调用分别占一行，不能把一次对话的两次调用误当成同一笔用量。

## 数据库由哪些表组成

当前有 5 张业务表和 1 张 Alembic 管理表。表的数量表示数据职责被拆分，并不表示页面上有多个对话窗口。

```mermaid
erDiagram
    principals ||--o{ identity_sessions : "签发会话凭证"
    principals o|--o| conversations : "拥有当前窗口"
    conversations ||--o{ messages : "包含消息"
    conversations ||--o{ model_calls : "产生模型调用"
```

- `principals`：游客和注册账号的统一身份。
- `identity_sessions`：浏览器身份 Cookie 对应的服务端会话记录。
- `conversations`：内部对话窗口和长对话摘要；当前页面每个身份只有一个当前窗口。
- `messages`：用户消息、助手回复及生成状态。
- `model_calls`：真实模型调用的耗时、状态和供应商用量。
- `alembic_version`：Alembic 记录当前 schema 版本，不是业务数据。

在 DBeaver 中右键 `Schemas > public > Tables` 并刷新即可看到这些表。表名右侧的 `40K`、`96K` 等值是 PostgreSQL 估算的存储空间，不是记录条数；查看记录数应执行 `SELECT count(*) FROM 表名`。

## `principals`：游客与账号身份

| 字段 | 类型与空值 | 用途 |
| --- | --- | --- |
| `id` | `uuid`，主键，非空 | 身份 ID，由应用生成 UUIDv7；浏览器不能自行指定。 |
| `kind` | `varchar(16)`，非空 | 当前应用只写入 `guest` 或 `account`。数据库目前没有该字段的 CHECK 约束。 |
| `username` | `varchar(100)`，可空，唯一 | 游客为 `NULL`；账号保存转为小写后的用户名。PostgreSQL 唯一约束负责处理并发重名。 |
| `password_hash` | `varchar(200)`，可空 | 游客为 `NULL`；账号保存带独立盐的 scrypt 哈希，不保存明文密码。 |
| `created_at` | `timestamptz`，非空，默认 `now()` | 身份创建时间。 |

删除身份会级联删除它的 `identity_sessions`。`conversations.owner_id` 没有设置级联删除，所以不能直接删除仍拥有窗口的身份；应由业务流程先处理其窗口。

## `identity_sessions`：浏览器身份凭证

| 字段 | 类型与空值 | 用途 |
| --- | --- | --- |
| `token_hash` | `varchar(64)`，主键，非空 | 随机 Cookie 原值的 SHA-256 摘要；数据库不保存原始凭证。 |
| `principal_id` | `uuid`，非空，外键 | 指向 `principals.id`；身份删除时通过 `ON DELETE CASCADE` 删除会话记录。 |
| `expires_at` | `timestamptz`，非空 | 服务端会话失效时间；当前签发期为 30 天。 |

`ix_identity_sessions_principal_id` 支持按身份查找会话。浏览器同时使用活跃身份 Cookie 和游客 Cookie：登录只切换活跃身份，原游客凭证保留，退出后可恢复原游客窗口。

## `conversations`：当前对话窗口

| 字段 | 类型与空值 | 用途 |
| --- | --- | --- |
| `id` | `uuid`，主键，非空 | 内部窗口 ID，由应用生成 UUIDv7。前端取得后用于消息查询和 SSE。 |
| `owner_id` | `uuid`，可空，外键 | 指向 `principals.id`，所有读写接口用它校验归属；仅兼容期遗留数据可能为 `NULL`。 |
| `title` | `varchar(200)`，非空，默认“新对话” | 旧多会话接口保留的标题；当前单窗口页面不展示标题列表。 |
| `created_at` | `timestamptz`，非空，默认 `now()` | 窗口创建时间。 |
| `updated_at` | `timestamptz`，非空，默认 `now()` | 最近消息或生成状态更新时间，也供旧接口排序。 |
| `summary` | `text`，非空，默认空字符串 | 较早对话的滚动摘要，只供服务端构造模型上下文。 |
| `summary_through_sequence` | `integer`，非空，默认 `-1` | 摘要已经覆盖到的消息序号；`-1` 表示尚未覆盖任何消息。 |
| `is_current` | `boolean`，非空，默认 `false` | `true` 表示该身份当前使用的唯一窗口。 |
| `content_epoch` | `integer`，非空，默认 `0` | 清空消息时递增；旧摘要任务只有版本仍相同时才能回写。 |

关键索引如下：

- `uq_conversations_current_owner` 是条件唯一索引，只约束 `is_current=true` 的记录，保证每个非空 `owner_id` 最多一个当前窗口。
- `ix_conversations_owner_updated_at` 支持旧兼容接口按身份和最近活动时间查询。

`0006_single_window` 曾把同一身份的旧版多个会话合并进一个当前窗口，并重新排列消息序号；不同身份从不合并。`GET /api/v1/conversations/current` 取得或创建当前身份窗口。`DELETE /api/v1/conversations/current/messages` 保留这条窗口记录，只清空其消息和摘要并递增 `content_epoch`。

## `messages`：对话消息

| 字段 | 类型与空值 | 用途 |
| --- | --- | --- |
| `id` | `uuid`，主键，非空 | 消息 ID，由应用生成 UUIDv7。 |
| `conversation_id` | `uuid`，非空，外键 | 指向 `conversations.id`；删除整个窗口时通过 `ON DELETE CASCADE` 删除消息。 |
| `role` | `varchar(16)`，非空 | CHECK 约束只允许 `user` 或 `assistant`。 |
| `content` | `text`，非空，默认空字符串 | 用户输入或助手已保存正文；流式生成期间可能暂时为空。 |
| `status` | `varchar(16)`，非空，默认 `pending` | CHECK 约束允许 `pending`、`streaming`、`completed`、`failed`、`cancelled`。 |
| `sequence` | `integer`，非空 | 窗口内顺序，必须大于等于 0；与 `conversation_id` 组成唯一约束。 |
| `error_code` | `varchar(64)`，可空 | 失败消息的结构化错误码，例如 `generation_interrupted`。 |
| `created_at` | `timestamptz`，非空，默认 `now()` | 消息创建时间。 |
| `updated_at` | `timestamptz`，非空，默认 `now()` | 草稿检查点或最终状态更新时间。 |

每轮发送在同一会话行锁事务中连续创建 `user/completed` 和 `assistant/streaming` 两条记录。助手正文累计新增 200 字符或距上次保存达到 1 秒时写一次检查点，最终写入 `completed`、`failed` 或 `cancelled`。应用启动会把进程异常退出后遗留的 `assistant/streaming` 改为 `failed`，错误码设为 `generation_interrupted`。不要在 DBeaver 中手工修改这些状态来代替业务流程。

## `model_calls`：模型调用与真实用量

| 字段 | 类型与空值 | 用途 |
| --- | --- | --- |
| `id` | `uuid`，主键，非空 | 调用记录 ID，由应用生成 UUIDv7。 |
| `conversation_id` | `uuid`，非空，外键 | 指向 `conversations.id`；删除整个窗口时通过 `ON DELETE CASCADE` 删除调用记录。 |
| `assistant_message_id` | `uuid`，可空 | 主回复关联的助手消息 ID；摘要调用通常为空。当前没有外键，因此清空消息后调用记录仍可保留。 |
| `purpose` | `varchar(16)`，非空 | 应用写入 `summary` 或 `reply`；数据库目前没有 CHECK 约束。 |
| `status` | `varchar(16)`，非空 | 服务端观察到的 `completed`、`failed` 或 `cancelled`；数据库目前没有 CHECK 约束。 |
| `model` | `varchar(200)`，可空 | 供应商实际返回的模型名；未取得时为 `NULL`。 |
| `duration_ms` | `integer`，可空 | 从真实调用开始到上游流关闭的耗时。 |
| `prompt_tokens` | `integer`，可空 | 供应商返回的输入 token；未知保持 `NULL`。 |
| `completion_tokens` | `integer`，可空 | 供应商返回的输出 token；未知保持 `NULL`。 |
| `total_tokens` | `integer`，可空 | 供应商返回的总 token；未知保持 `NULL`，不按 0 处理。 |
| `finish_reason` | `varchar(64)`，可空 | 供应商结束原因；断流或取消时可能没有。 |
| `created_at` | `timestamptz`，非空，默认 `now()` | 调用记录写入时间。 |
| `usage_owner_id` | `uuid`，可空 | 调用发生时的身份 ID 快照，故意不设外键，避免身份变化影响历史归因。 |
| `usage_owner_kind` | `varchar(16)`，可空 | 调用发生时的 `guest`/`account` 快照。 |

`ix_model_calls_conversation_created` 支持按窗口和时间查询。`0007_s5_limits` 新增 `ix_model_calls_usage_owner_purpose_created`，用于按调用时身份、用途和时间统计 UTC 当日主回复额度，避免每次发送前全表扫描。摘要调用和主回复调用分别占一行。该表不保存提示词、回复正文或 API Key。页面执行“清空对话”时保留调用记录，用于成本核算、额度和排错；只有删除整个 `conversations` 容器时才会级联删除。

## `alembic_version`：迁移版本

| 字段 | 类型与空值 | 用途 |
| --- | --- | --- |
| `version_num` | `varchar(32)`，主键，非空 | 当前 schema 版本；本项目当前为 `0007_s5_limits`。 |

该表由 Alembic 管理。不要手工修改版本号来伪造迁移完成状态，应执行 `alembic upgrade head`。

## 当前删除与清空规则

| 操作 | 删除内容 | 保留内容 |
| --- | --- | --- |
| 页面“清空对话” | 当前身份窗口的 `messages`、`summary` 和摘要覆盖位置 | `principals`、`identity_sessions`、`conversations` 容器、`model_calls` |
| 删除整个内部窗口（旧兼容接口） | 该 `conversation`，并级联删除其 `messages` 和 `model_calls` | 身份和登录会话 |
| 删除身份 | 级联删除该身份的 `identity_sessions` | 有关联窗口时数据库会拒绝直接删除，需先处理窗口 |
| 登录或退出 | 不删除业务记录 | 游客窗口与账号窗口分别保留，只切换当前显示身份 |

清空业务数据时应保留表结构和 `alembic_version`。清空后刷新前端会重新创建游客身份及其当前窗口，因此记录数会随使用实时变化。

## 环境变量

仓库只提交不含真实密码的 `.env.example`。本机连接信息应写入被 Git 忽略的 `.env`：

```dotenv
DATABASE_URL=postgresql+asyncpg://echo_nook:你的密码@127.0.0.1:5432/echo_nook_dev
TEST_DATABASE_URL=postgresql+asyncpg://echo_nook:你的密码@127.0.0.1:5432/echo_nook_test
DATABASE_ECHO=false
```

- `DATABASE_URL`：`development` 和 `production` 环境的数据库；本地开发指向 `echo_nook_dev`。
- `TEST_DATABASE_URL`：`APP_ENV=test` 和数据库测试专用连接；代码强制数据库名以 `_test` 结尾。
- `DATABASE_ECHO`：控制是否输出 SQLAlchemy SQL。默认关闭，避免日志过于嘈杂或意外暴露业务数据。

连接串中的特殊字符需要进行 URL 编码。不要把真实密码提交到 `.env.example`、文档、日志或 Git。服务端启动时读取当前环境的连接串并执行连接检查；失败时拒绝启动，关闭时释放 SQLAlchemy 连接池。配置使用 `SecretStr` 保存，避免常规日志和对象表示泄露完整连接串。

测试服务器应使用独立的服务器端环境变量和数据库，不要复制本机 `.env`。正式环境将来同样使用独立凭据，并遵循最小权限原则。

## 服务管理命令

查看 Homebrew 服务：

```bash
brew services list
```

启动、停止或重启 PostgreSQL 18：

```bash
brew services start postgresql@18
brew services stop postgresql@18
brew services restart postgresql@18
```

检查服务是否接受连接：

```bash
pg_isready -h 127.0.0.1 -p 5432
```

查看 5432 端口监听进程：

```bash
lsof -nP -iTCP:5432 -sTCP:LISTEN
```

## 连接与检查

连接开发库或测试库：

```bash
psql -h 127.0.0.1 -p 5432 -U echo_nook -d echo_nook_dev
psql -h 127.0.0.1 -p 5432 -U echo_nook -d echo_nook_test
```

`psql` 内常用命令：

```text
\conninfo        显示当前连接
\l               列出数据库
\du              列出角色
\dt              列出当前 schema 的表
\d 表名          查看表结构
\x               切换扩展显示
\q               退出
```

检查当前数据库和账号：

```sql
SELECT current_database(), current_user;
```

不进入交互模式也可以执行检查：

```bash
psql -h 127.0.0.1 -p 5432 -U echo_nook -d echo_nook_dev \
  -c 'SELECT current_database(), current_user;'
```

## 首次创建或重建账号和数据库

以下命令只用于新的 PostgreSQL 实例，现有本机环境已经完成，不需要重复执行：

```sql
CREATE ROLE echo_nook WITH LOGIN PASSWORD '替换为强密码';
CREATE DATABASE echo_nook_dev OWNER echo_nook;
CREATE DATABASE echo_nook_test OWNER echo_nook;
```

可先以本机 PostgreSQL 管理账号连接默认数据库，再执行以上 SQL：

```bash
psql postgres
```

不要把应用账号设置为超级用户。修改密码可执行：

```sql
ALTER ROLE echo_nook WITH PASSWORD '替换为新的强密码';
```

修改密码后要同步更新本机 `.env`，并重启服务端。

## 备份与恢复

备份开发库：

```bash
pg_dump -h 127.0.0.1 -p 5432 -U echo_nook \
  -d echo_nook_dev -Fc -f echo_nook_dev.dump
```

恢复前应创建一个新的空数据库，避免误覆盖正在使用的数据：

```bash
createdb -h 127.0.0.1 -p 5432 -U echo_nook echo_nook_restore
pg_restore -h 127.0.0.1 -p 5432 -U echo_nook \
  -d echo_nook_restore --no-owner echo_nook_dev.dump
```

`*.dump` 已被 Git 忽略，但备份仍可能包含真实对话内容，应按敏感数据保存。

## Alembic 迁移命令

依赖安装后，先把当前环境升级到最新数据库版本：

```bash
.venv/bin/alembic upgrade head
.venv/bin/alembic current
.venv/bin/alembic history
.venv/bin/alembic downgrade -1
```

`APP_ENV=test` 时 Alembic 自动使用 `TEST_DATABASE_URL`：

```bash
APP_ENV=test .venv/bin/alembic upgrade head
APP_ENV=test .venv/bin/alembic current
```

生成迁移文件应在确认模型变更后执行，并人工审查生成内容：

```bash
.venv/bin/alembic revision --autogenerate -m "describe schema change"
```

迁移文件要提交 Git，数据库文件、密码和备份文件不要提交。测试和部署环境均通过 `alembic upgrade head` 重放同一套迁移，不能手工修改业务表来替代迁移。

## 常见问题

### `connection refused`

先运行 `brew services list` 和 `pg_isready`。如果服务未启动，执行 `brew services start postgresql@18`。

### `password authentication failed`

确认连接串的账号是 `echo_nook`、密码正确，且特殊字符已经 URL 编码。不要通过打印完整连接串来排错。

### `database does not exist`

用 `psql postgres` 后执行 `\l`，确认 `echo_nook_dev` 和 `echo_nook_test` 存在且拼写一致。

### 测试误连开发库

数据库测试启动时必须检查连接目标；只要数据库名不是 `echo_nook_test` 就立即失败。任何清表、回滚或重建 schema 的操作都不得针对开发库执行。

### 应用启动时数据库连接失败

先确认 PostgreSQL 正常运行、当前环境对应的连接变量存在，并执行 `.venv/bin/alembic upgrade head`。应用启动时会执行数据库连通性检查；可用 `.venv/bin/alembic current` 确认当前版本为 `0007_s5_limits`。其他部署环境需各自执行迁移。

### 开发库旧会话认领

身份功能上线前遗留的会话可能存在 `owner_id=NULL`，新接口不会把它们自动交给第一个访问者。可先用 `SELECT count(*) FROM conversations WHERE owner_id IS NULL` 检查；结果为 0 时无需认领。确有可信旧数据时，先启动前后端，在浏览器 Network 面板查看 `POST /api/v1/identity/bootstrap` 的响应 `id`，确认这是自己的游客身份。然后在服务端仓库运行下面两条命令，第二条只有确认目标身份后才执行：

```bash
.venv/bin/python -m app.scripts.claim_legacy --owner <bootstrap 返回的 id>
.venv/bin/python -m app.scripts.claim_legacy --owner <bootstrap 返回的 id> --apply
```

命令只允许 `APP_ENV=development`，默认仅预览数量；执行时只认领仍为 `NULL` 的旧会话，并把旧消息、调用记录安全并入目标身份的当前窗口，不改动其他身份的归属。若该身份已有窗口，原消息按时间顺序重新编号，摘要重新生成；先确认目标身份确实属于自己。刷新前端即可在唯一窗口看到被认领的消息。测试库和正式库不得用此开发命令；部署时应分别规划可信的历史归属迁移。
