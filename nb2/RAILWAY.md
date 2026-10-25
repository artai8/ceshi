# Railway 测试部署指南

## 1. 部署结构

Railway 不直接运行本项目的 `docker-compose.yml`。在同一个 Railway Project
内创建六个 Service：

| Service 名称 | 来源 | 公网域名 | 作用 |
|---|---|---:|---|
| `mongodb` | Railway MongoDB | 否 | 数据库和持久卷 |
| `api` | 本仓库 `Dockerfile` | 否 | FastAPI 控制面 |
| `bot` | 本仓库 `Dockerfile` | 否 | Telegram 控制 Bot，固定单副本 |
| `dashboard` | 本仓库 `Dockerfile` | 否 | Streamlit 后台 |
| `worker` | 本仓库 `Dockerfile` | 否 | 转发 worker，测试阶段一个副本 |
| `gateway` | `Dockerfile.railway-gateway` | 是 | 同域名入口和 HTTPS |

只有 `gateway` 对公网开放。它将 `/dashboard` 代理到 Streamlit，其余路径代理到
FastAPI。这样 `/web` 写入的安全 Cookie 与 Dashboard 保持同源。

## 2. 前置值

准备：

- Telegram `API_ID`、`API_HASH`。
- BotFather 生成的 `BOT_TOKEN`。
- 管理员 Telegram 数字 ID（可选）。
- Session 加密密钥。可在本机生成：

  ```bash
  python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
  ```

此密钥上线后不得随意更换；否则已保存的 Telegram Session 无法解密。

## 3. 上传源码

推荐把 `nb2` 的内容作为 GitHub 仓库根目录，再在 Railway 创建 Empty Project。
如果 GitHub 仓库根目录外还有一层 `nb2`，每个代码 Service 的 Root Directory
都设置为 `/nb2`。

不要上传 `.env`、真实 Secret、旧 `*.session` 或 `nb.config.json`。

## 4. 创建 MongoDB

在 Project Canvas 选择 `+ New` -> `Database` -> `MongoDB`，将服务重命名为
`mongodb`。保持数据库私有，不创建 TCP Proxy 或公网域名。Railway 会提供
`MONGO_URL`，应用通过引用变量读取它。

测试数据库也必须保留 Railway Volume；删除 Environment、MongoDB Service 或
Volume 会丢失测试数据。

## 5. 创建 Shared Variables

在 Project Settings -> Shared Variables 创建：

```dotenv
NB_API_ID=填Telegram_API_ID
NB_API_HASH=填Telegram_API_HASH
NB_BOT_TOKEN=填Bot_Token
NB_SESSION_ENCRYPTION_KEY=填生成的Fernet密钥
NB_MONGO_DB_NAME=nb-multitenant
NB_ADMIN_TELEGRAM_IDS=
NB_WEB_SESSION_HOURS=24
NB_WORKER_CLIENT_CAPACITY=10
NB_WORKER_LEASE_SECONDS=45
NB_WORKER_POLL_SECONDS=5
```

将 `NB_API_HASH`、`NB_BOT_TOKEN`、`NB_SESSION_ENCRYPTION_KEY` 设置为 Sealed。
测试阶段容量先使用 10，不要一开始扩容多个 worker。

## 6. 创建四个 Python Service

从同一个 GitHub 仓库分别创建 `api`、`bot`、`dashboard`、`worker`。四者均使用
根目录 `Dockerfile`，并在 Settings -> Deploy 设置以下 Start Command：

在填写下面的 `PUBLIC_BASE_URL` 前，先创建名为 `gateway` 的第五个代码 Service，
按第 7 节设置它的 Dockerfile 并生成公网域名。首次因变量尚未填全而出现的失败部署
可以忽略，全部配置完成后重新 Deploy。

| Service | Start Command |
|---|---|
| `api` | `python -m nb.cli api --loud` |
| `bot` | `python -m nb.cli bot --loud` |
| `dashboard` | `python -m nb.cli dashboard --loud` |
| `worker` | `python -m nb.cli worker --loud` |

### api Variables

```dotenv
PORT=8000
MONGO_CON_STR=${{mongodb.MONGO_URL}}
MONGO_DB_NAME=${{shared.NB_MONGO_DB_NAME}}
BOT_TOKEN=${{shared.NB_BOT_TOKEN}}
PUBLIC_BASE_URL=https://${{gateway.RAILWAY_PUBLIC_DOMAIN}}
ADMIN_TELEGRAM_IDS=${{shared.NB_ADMIN_TELEGRAM_IDS}}
WEB_SESSION_HOURS=${{shared.NB_WEB_SESSION_HOURS}}
```

设置 Healthcheck Path 为 `/health`，Timeout 建议 300 秒。

### bot Variables

```dotenv
MONGO_CON_STR=${{mongodb.MONGO_URL}}
MONGO_DB_NAME=${{shared.NB_MONGO_DB_NAME}}
BOT_TOKEN=${{shared.NB_BOT_TOKEN}}
API_ID=${{shared.NB_API_ID}}
API_HASH=${{shared.NB_API_HASH}}
SESSION_ENCRYPTION_KEY=${{shared.NB_SESSION_ENCRYPTION_KEY}}
PUBLIC_BASE_URL=https://${{gateway.RAILWAY_PUBLIC_DOMAIN}}
```

### dashboard Variables

```dotenv
PORT=8501
CONTROL_API_URL=http://${{api.RAILWAY_PRIVATE_DOMAIN}}:8000
```

Healthcheck Path 可设置为 `/dashboard/_stcore/health`。

### worker Variables

```dotenv
MONGO_CON_STR=${{mongodb.MONGO_URL}}
MONGO_DB_NAME=${{shared.NB_MONGO_DB_NAME}}
API_ID=${{shared.NB_API_ID}}
API_HASH=${{shared.NB_API_HASH}}
SESSION_ENCRYPTION_KEY=${{shared.NB_SESSION_ENCRYPTION_KEY}}
WORKER_CLIENT_CAPACITY=${{shared.NB_WORKER_CLIENT_CAPACITY}}
WORKER_LEASE_SECONDS=${{shared.NB_WORKER_LEASE_SECONDS}}
WORKER_POLL_SECONDS=${{shared.NB_WORKER_POLL_SECONDS}}
```

四个 Service 均不要生成公网域名。`bot` 固定一个副本；测试阶段 `worker` 也只用
一个副本。Restart Policy 使用 `Always`。

## 7. 创建 gateway Service

打开前一步创建的 `gateway` Service；如果尚未创建，则再次连接同一个仓库并把服务名
设为 `gateway`：

1. Build -> Dockerfile Path 设置为 `/Dockerfile.railway-gateway`。
2. 设置 Variables：

   ```dotenv
   PORT=8080
   API_INTERNAL_URL=http://${{api.RAILWAY_PRIVATE_DOMAIN}}:8000
   DASHBOARD_INTERNAL_URL=http://${{dashboard.RAILWAY_PRIVATE_DOMAIN}}:8501
   ```

3. Networking -> Public Networking -> Generate Domain，目标端口选择 `8080`。
4. Healthcheck Path 设置为 `/health`，Timeout 设置为 300 秒。

Railway 在公网边缘终止 HTTPS，`Caddyfile.railway` 因此关闭了 Caddy 自动签证书。
不要部署 Compose 中原有的 Caddy Service，也不要把 `Caddyfile` 当作 Railway 配置。

## 8. 部署顺序和设置

建议按顺序部署：

1. `mongodb`
2. `api`、`dashboard`
3. `gateway`
4. `bot`、`worker`

Railway 没有 Compose 的 `depends_on`。首次并行部署时服务可能因 MongoDB 尚未就绪
而重启一次；Restart Policy 应保持 `Always`。

关闭所有 Service 的 Serverless/App Sleeping，尤其是 `mongodb`、`bot` 和
`worker`。Bot 长轮询和实时转发必须持续运行。

## 9. Telegram 配置与验收

复制 gateway 的 Railway 域名，例如：

```text
https://example.up.railway.app
```

在 BotFather 中把菜单或 Mini App URL 设置为：

```text
https://example.up.railway.app/miniapp
```

验证：

```bash
curl -fsS https://example.up.railway.app/health
```

期望：

```json
{"status":"ok"}
```

然后依次验证：

1. Bot `/start`、`/login`。
2. `/app` 打开 Mini App。
3. `/web` 通过一次性链接进入 `/dashboard`。
4. 创建、验证并运行一条测试转发任务。
5. 暂停任务并执行 `/logout`，确认账号与任务被清理。

普通浏览器直接打开 `/miniapp` 缺少 Telegram `initData`，不能代表 Mini App 登录
失败；必须从 Telegram 内打开。

## 10. 常见故障

- `502`：检查 gateway 的两个内部 URL、api/dashboard 日志和固定 `PORT`。
- API 启动失败：确认 `MONGO_CON_STR` 引用了 `mongodb.MONGO_URL`，而不是公网 URI。
- `/web` 后未登录：确认只给 gateway 公网域名，并从同一个 gateway 域名访问
  `/auth/magic/...` 和 `/dashboard`。
- Bot 重复拉取或冲突：确认 `bot` 只有一个副本。
- Worker 不运行：检查 `API_ID`、`API_HASH`、Session Key、MongoDB 租约和账号状态。
- Caddy 证书错误：Railway 部署必须使用 `Caddyfile.railway`，由 Railway 处理 HTTPS。
- 构建找不到 Dockerfile：如果仓库外层包含 `nb2`，设置 Root Directory 为 `/nb2`。

Railway 测试不能替代真实 Telegram 权限、FloodWait、长期媒体处理及 400 租户负载
验收。测试完成后应删除无用环境或设置消费上限，避免持续计费。
