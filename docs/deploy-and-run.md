# 部署与运行指南（MVP）

本文说明如何以**微服务方式**启动 Customer Profile 后端：HTTP API 与异步 Worker 分离部署，数据库与 Dify 使用外部环境变量配置。

## 1. 前置条件

- Python **3.11+**（本地运行）或 **Docker / Docker Compose**（容器运行）
- 可访问的 **MySQL 8.x**，并已创建业务库（如 `customer_profile`）
- 可访问的 **Dify**（Worker 消费任务时需要）
- 数据库迁移：**docker compose 启动时由独立的 `migrate` 容器自动执行**（见 §4 / §5）；本地裸跑可手动执行 `customer-profile-migrate`。

## 2. 环境变量

复制并编辑项目根目录下的 `.env`：

```bash
cp .env.example .env
```

启动 API / Worker / uvicorn 时，`load_config()` 会**自动读取项目根目录的 `.env`**（依赖 `python-dotenv`）。若已在操作系统里 `set` / `export` 了同名变量，则以系统环境为准（不覆盖）。

若未安装依赖，请先执行：`pip install -e .` 或 `pip install python-dotenv`。

必填项见 `.env.example`：`DIFY_*`、`MYSQL_*`。可选：

| 变量 | 说明 | 默认 |
|------|------|------|
| `API_HOST` | API 监听地址 | `0.0.0.0` |
| `API_PORT` | API 监听端口 | `8000` |
| `APP_ENV` | 环境标识 | `local` |
| `LOG_LEVEL` | 日志级别 | `INFO` |
| `WORKER_CONCURRENCY` | Worker 并发线程数 | `8` |
| `WORKFLOW_PLATFORM` | 工作流平台选择：`dify`、`qingtian` 或 `local` | `dify` |
| `INVITATION_STRATEGY_PLATFORM` | `invitation_strategy` 任务用哪个平台；`WORKFLOW_PLATFORM=local` 时**必填**（`dify` 或 `qingtian`），其它情况缺省跟随 `WORKFLOW_PLATFORM` | 跟随 `WORKFLOW_PLATFORM` |

> Dify API key 说明：
> - `DIFY_API_KEY_TEST` / `DIFY_API_KEY_PRODUCTION`：`customer_profile` 任务按 `work_env` 切换使用（旧的 `DIFY_API_KEY` 作为兜底）。
> - `DIFY_INVITATION_STRATEGY_API_KEY`：`invitation_strategy` 任务专用，不分 test/production（`work_env` 仍透传给 Dify，由工作流内部处理）。

### 2.1 工作流平台切换（Dify ↔ QingTian ↔ 本地服务）

Worker 在调用工作流时通过 `WORKFLOW_PLATFORM` 决定走哪条适配器：

- `WORKFLOW_PLATFORM=dify`（**默认**）— 走原有 Dify 工作流，使用全部 `DIFY_*` 变量。QingTian 变量不读取。
- `WORKFLOW_PLATFORM=qingtian` — 走 DFMC 自研 QingTian 平台，必填项：

| 变量 | 说明 |
|------|------|
| `QINGTIAN_BASE_URL` | 包含 `/api/proxy/api/v1` 前缀，client 会附加 `/run_app_workflow` 与 `/query_run_app_process` |
| `QINGTIAN_API_KEY_TEST` | `customer_profile` 任务、`work_env=test` 时使用 |
| `QINGTIAN_API_KEY_PRODUCTION` | `customer_profile` 任务、`work_env=production` 时使用 |
| `QINGTIAN_INVITATION_STRATEGY_API_KEY` | `invitation_strategy` 任务专用 |
| `QINGTIAN_USER_ID` | 透传到 QingTian 请求的 `UserID` 字段 |
| `QINGTIAN_POLL_INTERVAL_MS` / `QINGTIAN_POLL_TIMEOUT_MS` | 轮询参数，默认与 Dify 一致（2000ms / 2400000ms） |

QingTian 的 API Key 选择规则与 Dify 完全一致：`task_type=invitation_strategy` 优先于 `work_env`。InputData 字段名也与 Dify 现有 inputs 一致（`phone_number / batch_id / work_env`）。

- `WORKFLOW_PLATFORM=local` — 走**本地 Python 画像服务**（原先跑在 Dify / QingTian 上的编排逻辑已用 Python 重写为本服务）。必填项：

| 变量 | 说明 |
|------|------|
| `LOCAL_BASE_URL` | 本地服务地址，**必须写实际部署主机**（服务通常不与本项目同机，不要用 `127.0.0.1`） |
| `LOCAL_API_KEY` | 提交时作为 `X-API-Key` 请求头（即服务侧的 `INBOUND_API_KEY`） |
| `LOCAL_CALLBACK_API_KEY` | 服务方回写画像到我方时的共享密钥；留空则回写端点整体返回 503（相当于关闭） |
| `INVITATION_STRATEGY_PLATFORM` | **必填**，取 `dify` 或 `qingtian`——`invitation_strategy` 暂未迁移到本地服务 |

其余可调项：`LOCAL_TASK_PATH`（默认 `/api/v1/tasks`）、`LOCAL_CALLBACK_PATH`（默认 `/api/v1/remote/callback/update-profile`）、`LOCAL_SUBMIT_TIMEOUT_MS`（默认 10000）、`LOCAL_SUBMIT_MAX_RETRIES`（默认 3）、`LOCAL_RETRY_AFTER_DEFAULT_SECONDS`（默认 30）。

#### 本地服务的调用语义：**提交即返回，结果靠回写**

与 Dify / QingTian 的"提交后由 Worker 阻塞轮询到终态"不同，本地服务是**两段式**：

1. Worker 把手机号 `POST` 到 `{LOCAL_BASE_URL}{LOCAL_TASK_PATH}`，服务落库后**立即**返回 200 与 `run_id`（毫秒级）。接口不提供查询与进度，**无法轮询**。
2. 约 21 分钟后，服务把结果 `POST` 到我方的 `LOCAL_CALLBACK_PATH`，请求体为 `{"phone","batch_id","analysis_result"}`，带 `X-API-Key: <LOCAL_CALLBACK_API_KEY>`。**这一步才把任务置为 `succeeded`**。

因此 `WORKFLOW_PLATFORM=local` 时任务在提交成功后会**停留在 `processing`**，直到回写到达，这是预期状态，不是卡死。`GET /v1/customer-profile/tasks/{phone}/result` 期间返回 202。

几点运维须知：

- **回写失败/丢失**会导致任务长期停留在 `processing`（本地服务没有重推机制时）。恢复办法是重新提交同一手机号（upsert 会重新入队）。
- 回写端点会校验 `batch_id` 必须是该行"当前这次提交"对应的值；对不上返回 409，避免上一轮的迟到回写覆盖新任务。
- 回写体里的 `analysis_result` 会用与 Dify 路径**同一个** schema（`contracts/customer-profile-output.schema.json`）校验，`card_id` / `nickname` 缺失会返回 422 并在日志中记录。
- 手机号校验比本项目提交接口更严：本地服务要求 11 位 `^1[3-9]\d{9}$`。本项目提交接口允许 `+` 与任意长度，这类号码在 Worker 侧会**在提交前**就失败并写入 `error_message`（错误码 `LOCAL_REQUEST_FAILED`）。
- 失败任务的 `error_message` 前缀为 `LOCAL_*`（`LOCAL_REQUEST_FAILED` / `LOCAL_TIMEOUT`），可据此区分平台归属。

启动时 `config_loaded` 日志会输出 `workflow_platform` 字段，便于核对当前生效平台；同时 bootstrap 会打印 `workflow_client_selected platform=...`。失败任务在 `tasks.error_message` 中保留平台前缀（`DIFY_*` 或 `QINGTIAN_*`），可据此排查归属平台。

回滚到 Dify：将 `.env` 中 `WORKFLOW_PLATFORM` 改回 `dify`（或删除该行）并重启 API + Worker。QingTian 变量空着不会影响 Dify 路径。同理，从 `local` 回滚只需改回 `dify` / `qingtian`，`LOCAL_*` 变量空着不影响其它平台。

Worker 在无 `queued` 任务时，默认 **不再** 以 INFO 每 2 秒打印 `worker_tick`（避免刷屏）。需要观察轮询心跳时，可将 `LOG_LEVEL` 设为 `DEBUG`。

## 3. 本地运行（开发 / 联调）

### 3.1 安装依赖

```bash
cd customer_profile_backend
pip install -e ".[dev]"
```

### 3.2 启动 API

```bash
# 方式一：入口脚本（读取 API_HOST / API_PORT）
set API_HOST=0.0.0.0
set API_PORT=8000
customer-profile-api
```

Linux / macOS：

```bash
export API_HOST=0.0.0.0
export API_PORT=8000
customer-profile-api
```

### 3.3 启动 Worker（处理队列、调用 Dify）

另开终端，相同 `.env`：

```bash
customer-profile-worker
```

### 3.4 使用 uvicorn 直接启动（与容器一致）

```bash
uvicorn src.app.asgi:app --host 0.0.0.0 --port 8000
```

健康检查：`GET http://127.0.0.1:8000/health`

## 4. Docker Compose（推荐 MVP 部署）

**Ubuntu 24.04 上从零安装 Docker、改端口、UFW 放通、前端 Base URL**：见 **[docker-deploy-ubuntu24.md](docker-deploy-ubuntu24.md)**。

在项目根目录：

```bash
# 准备 .env（含 MYSQL_ / DIFY_ 等）
docker compose build
docker compose up -d
```

- **api**：对外暴露 HTTP，默认映射本机 `8000` → 容器内 `8000`。
- **worker**：后台轮询 MySQL 队列并调用 Dify。
- **migrate**：一次性容器，启动时扫 `db/migrations/*.sql` 并按文件名顺序全部执行（所有 SQL 已实现幂等，可放心重复执行）。`api` 和 `worker` 通过 `depends_on: service_completed_successfully` 等它退出成功后再启动；任一迁移失败都会阻塞业务进程启动。MySQL 尚未就绪时 `migrate` 会自动重试（次数与间隔由 `MIGRATION_DB_WAIT_MAX_TRIES` / `MIGRATION_DB_WAIT_INTERVAL_MS` 控制，默认 30 次 × 2s）。

修改本机映射端口（例如改为 8080）：

```bash
# Windows CMD
set HOST_API_PORT=8080
docker compose up -d
```

```bash
# Linux / macOS
export HOST_API_PORT=8080
docker compose up -d
```

查看日志：

```bash
docker compose logs -f api
docker compose logs -f worker
```

停止：

```bash
docker compose down
```

## 5. 数据库迁移

**Docker Compose 部署下无需手动操作**：`docker compose up` 会先跑独立的 `migrate` 容器，扫描 `db/migrations/*.sql` 并按文件名顺序全部应用，成功退出后 `api` / `worker` 才会启动。所有迁移文件都使用 `CREATE TABLE IF NOT EXISTS` 或 `information_schema` 检查实现幂等，每次启动重复执行都是安全的。

如需单独触发（例如只想做迁移、不起业务进程）：

```bash
docker compose run --rm migrate
```

**本地裸跑**（已配好 `.env` 中的 `MYSQL_*`）：

```bash
customer-profile-migrate
```

可调参数：

| 变量 | 说明 | 默认 |
|------|------|------|
| `MIGRATION_DB_WAIT_MAX_TRIES` | MySQL 不可达时的最大重试次数 | `30` |
| `MIGRATION_DB_WAIT_INTERVAL_MS` | 重试间隔（毫秒） | `2000` |

> ⚠️ `003_add_task_type.sql` 会先 `DROP` 旧的 `uk_customer_profile_tasks_phone` 唯一键，再加上新的 `uk_customer_profile_tasks_phone_type (customer_phonenumber, task_type)`。MySQL DDL 不支持事务，期间存在短暂窗口可能允许同一手机号重复入库。**对在线升级建议在低峰期、API 短暂下线后执行**；首次全新部署不受此影响。
>
> 若代码已上线但 `003` 因某种原因未执行成功，新版 API 会在 `bootstrap` 日志里输出 `task_type_column_missing` 警告，且 `invitation_strategy` 提交会被错误地按 `customer_profile` 处理。
>
> ⚠️ `004_add_work_env_and_batch_id.sql` 补齐了 `work_env` / `batch_id` 两列。在该迁移执行前，repository 在读取时会回退到字面量 `'test'`、写入时直接丢弃这两个字段，**导致所有 `work_env=production` 的提交都被静默打到测试 Dify 工作流**。新部署或 docker compose 启动会自动应用；已经存在的环境需要确认 `customer_profile_tasks` 表里有这两列，缺失则手动执行一次 `customer-profile-migrate`。

## 6. 对外接口一览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查（含 DB ping） |
| POST | `/v1/customer-profile/tasks` | 提交任务（异步） |
| GET | `/v1/customer-profile/tasks` | 按手机号前缀列表 |
| GET | `/v1/customer-profile/tasks/{phone}/result` | 查询单任务结果 |
| POST | `/api/v1/remote/callback/update-profile` | **本地服务回写画像**（仅 `WORKFLOW_PLATFORM=local` 注册；需 `X-API-Key`，路径可用 `LOCAL_CALLBACK_PATH` 调整） |

详细请求体与轮询方式见 `docs/frontend-api-guide.md`。

## 7. 启动后 API 集成测试（curl）

1. 按上文启动 API（Worker 可选；仅测提交/列表/进行中结果时可不启 Worker）。
2. 设置环境变量并运行 pytest：

**Windows CMD：**

```cmd
set RUN_API_HTTP_E2E=1
set API_E2E_BASE_URL=http://127.0.0.1:8000
pytest tests\integration\test_api_http_curl_e2e.py -v -s
```

**Linux / macOS：**

```bash
export RUN_API_HTTP_E2E=1
export API_E2E_BASE_URL=http://127.0.0.1:8000
pytest tests/integration/test_api_http_curl_e2e.py -v -s
```

也可使用 shell 脚本（需已安装 `curl`）：

```bash
chmod +x scripts/api_e2e_curl.sh
API_E2E_BASE_URL=http://127.0.0.1:8000 ./scripts/api_e2e_curl.sh
```

## 8. 常见问题

- **API 启动报缺环境变量**：检查 `.env` 是否与启动命令在同一工作目录，或已 `export` / `set` 全部必填项。
- **Worker 不消费**：确认 MySQL 连通、任务状态为 `queued`、Worker 进程在跑。
- **容器内访问本机文件路径**：提交任务里的 `wechat_*_image` 若为本地路径，须是 **Worker 容器内可读路径**；演示环境建议使用 URL 或与 Worker 挂载相同卷。

### 8.1 提交后 Worker 不转发（日志无异常）——先比 `process_targets`

API 与 Worker 是**两个进程**，只通过 MySQL 队列耦合。若前端提交后本地服务没收到任何请求、且两边日志都没有异常，最常见的原因不是代码，而是**两个进程没有指向同一个数据库或同一份配置**：

1. 两个进程启动时都会打一行 `process_targets`，直接比两行的 `db=` 与 `local_base_url=`：
   - `db=` 是进程**实际连接**的 MySQL（`host:port/database`）。两行不一致时，任务入队进的是 API 的库，Worker 在另一个库里永远查不到活干。
   - `local_base_url=` 是 Worker 会把手机号 POST 过去的地址。
2. `docker compose` 只在**创建容器时**把 `.env` 注入进程。改了 `.env` 后必须重建容器才生效：

   ```bash
   docker compose up -d --force-recreate api worker
   ```

   只 `restart` 不重建，容器仍持有旧的配置快照——`config_loaded` 与 `process_targets` 会显示修改前的值，这正是"改了配置却没反应"的典型现场。
3. 每分钟一次的心跳 `worker_queue_depth pools={'customer_profile': N, ...}` 用来区分三种沉默：
   - `N=0`：Worker 连的库里确实没有待跑任务——**去查数据库指向**，不是查 Worker。
   - `N>0` 但一直没有 `task_claimed`：Worker 看得到任务却领不到，多为 `task_type` 列缺失或类型名对不上（见 §5 关于 `003` 的说明）。
   - 完全没有这一行：supervisor 线程没起来或进程已退出。
4. 领到任务后会依次出现 `task_claimed` → `worker_workflow_invoke_begin`（含 `platform=`、`customer_phonenumber=`、`batch_id=`）→ `local_run_workflow_start`（含 `netloc=`、`path=`）→ `local_submit_response`（含 `status=`、`elapsed_ms=`）。**日志停在哪一行，问题就在那一步**：
   - 停在 `worker_workflow_invoke_begin` 之后：连不上 `LOCAL_BASE_URL`，同一次调用会有 `local_submit_transport_error`（连接拒绝/超时/DNS）或 `local_submit_timeout`。
   - 停在 `local_submit_response` 且 `status=401/403/422`：密钥或手机号格式被服务端拒（`LOCAL_API_KEY` 要等于服务侧的 `INBOUND_API_KEY`；号码须为 11 位 `^1[3-9]\d{9}$`）。
   - 有 `worker_task_submitted_deferred`：转发**已成功**，任务停在 `processing` 等回写（约 21 分钟），这是预期状态。
5. 手机号不合规时 Worker 会在**发请求前**就失败，看不到任何 `local_*` 行，只有 `worker_task_failed error_code=LOCAL_REQUEST_FAILED`。
