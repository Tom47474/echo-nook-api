# PostgreSQL 本地开发与测试指南

本文说明 EchoNook API 的本地 PostgreSQL 配置、日常使用和排错方式。命令默认在 macOS、Homebrew PostgreSQL 18 环境执行。

## 当前状态

截至 2026-11-23，本机数据库基础设施已准备完成：

| 项目 | 当前状态 |
| --- | --- |
| PostgreSQL | Homebrew PostgreSQL 18.7，监听 `127.0.0.1:5432` |
| 应用账号 | `echo_nook`，普通账号，不是超级用户 |
| 开发数据库 | `echo_nook_dev` |
| 测试数据库 | `echo_nook_test` |
| 业务表 | `conversations`、`messages`；迁移版本为 `0002_conversation_messages` |
| 代码接入 | 已接入 SQLAlchemy 2.1、asyncpg 0.32 和 Alembic 1.20 |

开发库和测试库运行在同一个 PostgreSQL 服务中，但数据彼此隔离。日常本地运行使用 `echo_nook_dev`；自动化测试和可能清空数据的测试操作只能使用 `echo_nook_test`。

## 当前表结构

`conversations` 保存会话容器：

- UUIDv7 `id`、标题、创建时间和更新时间。
- 可空 `owner_id` 为后续用户归属预留；当前不能作为权限控制依据。
- `(owner_id, updated_at)` 索引用于后续按用户列出最近会话。

`messages` 保存会话内消息：

- UUIDv7 `id`、`conversation_id`、角色、正文、状态、顺序和时间。
- `error_code` 保存可选的结构化生成错误类型。
- 同一会话内 `sequence` 唯一且必须非负。
- 角色只允许 `user`、`assistant`；状态只允许 `pending`、`streaming`、`completed`、`failed`、`cancelled`。
- 删除会话时由 PostgreSQL 外键级联删除对应消息。

在 DBeaver 中右键 `Schemas > public > Tables` 并选择刷新，即可看到 `alembic_version`、`conversations` 和 `messages`。表已经存在不代表当前聊天会自动保存；接口与存储服务将在 S2-3 至 S2-5 接入。

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

先确认 PostgreSQL 正常运行、当前环境对应的连接变量存在，并执行 `.venv/bin/alembic upgrade head`。应用启动时会执行数据库连通性检查；可用 `.venv/bin/alembic current` 确认当前版本为 `0002_conversation_messages`。
