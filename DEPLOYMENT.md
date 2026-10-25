# NB 多租户平台环境需求与部署指南

## 1. 适用范围与重要结论

本文适用于当前四服务架构：

- api：FastAPI 控制面和 Mini App。
- bot：私聊控制 Bot，只负责登录、入口、通知和解绑。
- dashboard：Streamlit 租户后台。
- worker：持有租户 Telegram 用户会话并执行转发。
- MongoDB：保存配置、会话密文、任务、作业、租约、投递、审计和 GridFS 水印。
- Caddy：公网 HTTPS 和反向代理。

指定的代码级阻断问题已经修复，详见 [CODE_REVIEW.md](CODE_REVIEW.md)。当前环境尚未执行真实 MongoDB、Telegram、Compose 和目标规模负载测试，因此应先部署到预发布并完成外部验收，再批准生产。

## 2. 部署假设

- 使用 Linux x86_64/arm64 主机运行 Linux 容器。
- 有可公网解析的域名，80/443 可以入站。
- 平台统一提供一组 Telegram API ID/API Hash。
- 每个租户绑定与 Bot 用户 ID 相同的 Telegram 用户账号。
- MongoDB 是唯一持久数据源，不部署 Redis。
- 业务定时区固定 Asia/Shanghai；主机保持 NTP 同步。
- 300+ 在线账号的容量数字是起始规划值，尚未通过真实 400 客户端负载验收。

## 3. 软件环境

### 3.1 Docker 部署

必需：

- 支持 Docker Compose v2 的 Docker Engine。
- 64 位 Linux。
- 公网域名和 HTTPS。
- 出站 DNS、HTTPS 和 Telegram 数据中心连接。
- 准确的系统时间。

当前镜像环境：

- Python 3.10 slim。
- MongoDB 7。
- Caddy 2。
- FFmpeg。
- Tesseract OCR 和简体中文语言包。

当前 poetry.lock 的关键版本：

| 依赖 | 版本 |
|---|---:|
| Telethon | 1.42.0 |
| FastAPI | 0.115.14 |
| Uvicorn | 0.34.3 |
| PyMongo | 4.17.0 |
| Streamlit | 1.60.0 |
| Pydantic | 2.13.4 |
| Cryptography | 44.0.3 |
| Pillow | 10.4.0 |
| aiohttp | 3.14.3 |

Dockerfile 使用 poetry.lock、多阶段构建和非 root 运行时。发布时仍应记录镜像 digest，并在 CI 中执行漏洞扫描。

### 3.2 本地开发

需要 Python 3.10 或 3.11、Poetry 2.x、MongoDB 7 或兼容托管服务、FFmpeg、Tesseract OCR 和 chi_sim 数据。

~~~bash
poetry install
poetry run python -m pytest -q
~~~

Windows 开发机需把 ffmpeg、tesseract 加入 PATH。生产建议使用 Linux 容器，减少媒体工具和字体差异。

## 4. 硬件与容量起点

下表是起步建议，不是经过本项目负载测试证明的容量保证。

| 场景 | CPU | 内存 | 磁盘 | Worker |
|---|---:|---:|---:|---:|
| 本地开发 | 4 核 | 8 GB | 30 GB | 1 |
| 少量预发布账号 | 4-8 核 | 8-16 GB | 50-100 GB SSD | 1-2 |
| 300+ 在线账号起点 | 16 核 | 32 GB | 200 GB NVMe | 8 |

容量说明：

- 默认每个 worker 最多持有 50 个账号。
- 8 个 worker 名义容量为 400 个账号，给 300 个在线账号留余量。
- 每个 worker 最多创建 2 个 CPU 处理进程；8 个 worker 最多形成 16 个媒体处理子进程。
- OCR、水印、视频和大媒体会显著提高 CPU、内存、磁盘和网络需求。
- 当前没有平台级 Telegram 媒体大小上限。临时磁盘至少覆盖“最大并发媒体数 × 最大媒体体积 × 2 到 3”，并设置告警。
- 生产建议至少 100 Mbps 稳定出站带宽，最终按媒体比例实测。

## 5. 网络、域名和端口

公网：

| 端口 | 用途 |
|---|---|
| TCP 80 | Caddy ACME 和 HTTP 跳转 |
| TCP 443 | Mini App、API、Streamlit HTTPS |

Compose 内部：

| 端口 | 服务 |
|---|---|
| 8000 | FastAPI |
| 8501 | Streamlit |
| 27017 | MongoDB |

要求：

- DOMAIN 的 A/AAAA 指向部署主机。
- 配置 AAAA 时，IPv6 必须真实可达 80/443。
- 云安全组、防火墙和 NAT 放通 80/443。
- 不把 27017 暴露公网。
- 允许容器出站访问 Telegram、DNS 和证书服务。
- 项目当前不提供代理配置；受限网络需先解决 Telegram 可达性。

## 6. Telegram 前置准备

### 6.1 API ID/API Hash

在 Telegram 官方开发者页面申请平台 API ID 和 API Hash。所有租户共享这组平台凭据，租户不需要填写。

API Hash 是高敏感秘密，不得进入镜像、源码、前端或日志。Telegram 对自动化账号有限制，上线前必须使用测试账号验证 FloodWait 和权限。

### 6.2 控制 Bot

使用 BotFather 创建 Bot 并取得 Bot Token：

1. 设置名称、描述和命令列表。
2. 将 Mini App/Web App 域名设为正式 HTTPS 域名。
3. 可把菜单按钮指向 <code>https://你的域名/miniapp</code>。
4. 用户从私聊 Bot 的 <code>/login</code> 开始绑定。

生产只运行一个 bot 副本。多个副本长轮询同一 Token 会竞争更新。

## 7. 环境变量和 Secret 文件

从 <code>.env.example</code> 创建 <code>.env</code>。非敏感设置保存在 .env：

| 变量 | 必填 | 示例 | 说明 |
|---|---|---|---|
| MONGO_ROOT_USERNAME | 内置 Mongo | nb_admin | 内置 Mongo 管理用户名 |
| MONGO_APP_USERNAME | 内置 Mongo | nb_app | 应用最小权限用户名 |
| MONGO_DB_NAME | 否 | nb-multitenant | 数据库名 |
| API_ID | 是 | 12345678 | 平台 Telegram API ID |
| DOMAIN | 是 | tg.example.com | Caddy 域名，不带协议 |
| PUBLIC_BASE_URL | 是 | https://tg.example.com | 外部根地址，必须 HTTPS |
| ADMIN_TELEGRAM_IDS | 否 | 123,456 | 管理员 Telegram ID |
| WORKER_CLIENT_CAPACITY | 否 | 50 | 单 worker 最大客户端数 |
| WORKER_LEASE_SECONDS | 否 | 45 | 租约秒数，代码最小 15 |
| WORKER_POLL_SECONDS | 否 | 5 | 扫描/热加载间隔，代码最小 1 |
| WEB_SESSION_HOURS | 否 | 24 | 浏览器会话小时数 |
| WORKER_ID | 通常不设置 | 自动生成 | 多副本必须唯一 |

敏感值写入 <code>secrets/</code>，每个文件只有一行值：

| 文件 | 使用者 |
|---|---|
| mongo_root_password | 内置 Mongo |
| mongo_app_password | 初始化应用用户 |
| mongo_uri | api、bot、worker |
| bot_token | api、bot |
| api_hash | bot、worker |
| session_encryption_key | bot、worker |

应用也支持对应的环境变量或 <code>NAME_FILE</code> 形式。Compose 默认挂载 Secret 文件，不再把全部秘密注入 dashboard。

严禁提交 .env 或 Secret 文件；不要让多个 worker 使用相同固定 WORKER_ID；不要随意更换 session_encryption_key。

### 7.1 生成加密密钥

~~~bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
~~~

PowerShell：

~~~powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
~~~

完整输出写入 <code>secrets/session_encryption_key</code>，并独立离线备份。MongoDB 备份不包含此密钥。

### 7.2 示例 .env

~~~dotenv
MONGO_ROOT_USERNAME=nb_admin
MONGO_DB_NAME=nb-multitenant
API_ID=12345678
DOMAIN=tg.example.com
PUBLIC_BASE_URL=https://tg.example.com
ADMIN_TELEGRAM_IDS=123456789
WORKER_CLIENT_CAPACITY=50
WORKER_LEASE_SECONDS=45
WORKER_POLL_SECONDS=5
WEB_SESSION_HOURS=24
~~~

<code>secrets/mongo_uri</code> 的内置 Mongo 示例：

~~~text
mongodb://nb_app:URL_ENCODED_PASSWORD@mongo:27017/nb-multitenant?authSource=nb-multitenant
~~~

URI 密码是 URL 编码形式，<code>mongo_app_password</code> 保存原始密码。应用 URI 不应使用 Mongo root 账号。

## 8. 生产上线门禁

至少完成：

- 确认 CODE_REVIEW.md 的代码级回归继续通过。
- 使用真实 MongoDB 运行集成测试。
- 成功构建并启动 Docker 镜像。
- 用测试 Telegram 账号完成扫码、验证码和 2FA。
- 验证公开频道自动加入、私密频道检查、目标发送权限。
- 验证实时、历史、定时、评论相册、机器人媒体和所有插件。
- 验证 worker 重启、租约接管、Mongo 短时故障和 FloodWait。
- 按预期媒体大小做临时磁盘压力测试。
- 做接近目标规模的客户端和消息速率测试。
- 配置 Mongo 认证、备份、监控和恢复演练。

门禁未完成时，只作为预发布部署。

## 9. 当前 Compose 的预发布部署

### 9.1 准备目录和配置

~~~bash
cd /opt
git clone YOUR_REPOSITORY_URL nb
cd nb
cp .env.example .env
chmod 600 .env
mkdir -p secrets
chmod 700 secrets
~~~

编辑 .env，并按 <code>secrets/README.md</code> 创建六个 Secret 文件：

~~~bash
chmod 600 secrets/mongo_root_password secrets/mongo_app_password secrets/mongo_uri secrets/bot_token secrets/api_hash secrets/session_encryption_key
~~~

确认 mongo_uri 使用 MONGO_APP_USERNAME 和 mongo_app_password，而不是 root 账号，并确认域名解析。若不用 Git 分发，不要复制开发机的旧 Session 或 nb.config.json。

### 9.2 预检查

~~~bash
docker version
docker compose version
docker compose config
ss -lntp
~~~

<code>docker compose config</code> 会展开配置，不要把含秘密的输出贴到公开日志。确认 80/443 未被其他服务占用。

### 9.3 构建和启动

~~~bash
docker compose build --pull
docker compose up -d --scale worker=8
~~~

Compose 不再内置固定 replicas，统一通过 <code>--scale worker=N</code> 控制。用 <code>docker compose ps</code> 确认实际副本数。

### 9.4 健康检查

~~~bash
docker compose ps
docker compose logs --tail=200 api
docker compose logs --tail=200 bot
docker compose logs --tail=200 worker
docker compose logs --tail=200 caddy
curl -fsS https://tg.example.com/health
~~~

期望：

~~~json
{"status":"ok"}
~~~

入口：

~~~text
https://tg.example.com/miniapp
https://tg.example.com/dashboard
~~~

普通浏览器直接打开 Mini App 会提示从 Telegram 进入，这是正常行为。dashboard 必须通过 Bot 的 <code>/web</code> 单次链接建立 Cookie。

## 10. 生产 MongoDB

### 10.1 当前 Compose

内置 Mongo 已启用 root 认证、独立 readWrite 应用用户和健康检查；api、bot、worker 从 <code>secrets/mongo_uri</code> 读取应用账号 URI，不再获得 root 凭据，也不再被 Compose 硬编码覆盖。若改用托管 Mongo，只需替换 mongo_uri，并在生产 override 中禁用内置 mongo 服务及相应依赖。

Mongo 官方镜像只会在空数据目录首次初始化 root 用户。若已经存在旧的无认证 <code>mongo-data</code> 卷，不能直接套用新 Compose：先备份数据，在旧实例内创建管理员用户后再启用认证，或按项目“不迁移旧部署”的边界使用新的数据卷重新配置。不要删除尚未验证备份的数据卷。

### 10.2 生产要求

- 使用托管 MongoDB 或至少三节点副本集。
- 启用认证和 TLS。
- 数据库账号只授权目标数据库。
- 开启自动备份和时间点恢复。
- 监控连接、延迟、磁盘、复制和 TTL。
- 不公开 27017；托管服务只允许部署网络。
- 恢复演练同时验证 GridFS 水印。

URI 形态示例：

~~~dotenv
MONGO_CON_STR=mongodb://nb_user:URL_ENCODED_PASSWORD@mongo1:27017,mongo2:27017,mongo3:27017/nb-multitenant?replicaSet=rs0&authSource=nb-multitenant&tls=true
~~~

密码必须 URL 编码，不要把真实 URI 写入仓库或命令历史。

### 10.3 必需索引

服务启动会调用 ensure_indexes。首次启动后检查：

- telegram_accounts.tenant_id 唯一。
- jobs.job_key 唯一。
- jobs(task_id, scheduled_at) 条件唯一。
- web_sessions.expires_at TTL。
- delivery_records.delivery_key 唯一。
- delivery_records.expires_at TTL。

TTL 删除不是严格实时，过期数据可能延迟清理。

## 11. 服务副本与扩容

| 服务 | 推荐副本 | 说明 |
|---|---:|---|
| caddy | 单机 1 | 公网入口 |
| mongo | 开发 1，生产高可用 | 不依赖单容器生产 |
| api | 1 起步 | 可在外部负载均衡后扩容 |
| bot | 固定 1 | 避免更新竞争 |
| dashboard | 1 起步 | WebSocket 需保持会话 |
| worker | 按容量 | 默认 50 账号/副本 |

调整 worker：

~~~bash
docker compose up -d --scale worker=10 worker
~~~

缩容前检查租约和历史作业。正常停止会释放租约；强制终止需等待租约过期。不要为所有副本设置相同 WORKER_ID，默认 hostname+pid 适合 Compose。

运行限制：

- 每账号独立顺序队列。
- 租户间并行。
- FloodWait 只暂停对应租户。
- 队列满会施加背压；必须监控 “Tenant queue applying backpressure” 并处理持续拥塞。
- 增加 worker 不能绕过 Telegram 单账号限制。

## 12. 首次功能验收

使用专用测试账号：

1. 私聊 Bot，发送 <code>/start</code>。
2. <code>/login</code> 测试二维码。
3. 测试扫描账号与 Bot 用户不一致会被拒绝。
4. 测试 <code>/phone</code>、<code>/code</code> 和 <code>/password</code>。
5. <code>/app</code> 创建草稿任务。
6. 分别填写数值 ID、@username、公开 t.me 链接。
7. 测试公开频道自动加入。
8. 测试未手工加入的私密邀请被拒绝。
9. 测试无发送权限目标进入 error。
10. 测试同来源扇出到不同目标和过滤条件。
11. 测试实时、历史和 Asia/Shanghai 定时任务。
12. 测试编辑/删除同步、评论、相册评论、机器人媒体。
13. 逐个测试过滤、格式、替换、水印、OCR、页眉页脚、剧透、Inline Button。
14. <code>/web</code> 登录 dashboard，验证与 Mini App 同时编辑冲突。
15. <code>/logout</code> 后确认 Telegram 授权和 Mongo 数据删除。

评论相册、插件失败和 tenant profile 清理已有回归测试；真实 Telegram 环境仍必须重复执行这些验收。

## 13. 测试命令

PowerShell：

~~~powershell
$env:NB_MULTITENANT='1'
python -m compileall -q nb tests
python -m pytest -q
python -m poetry check
python -m pip check
node --check nb/multitenant/static/app.js
~~~

Linux：

~~~bash
NB_MULTITENANT=1 poetry run python -m compileall -q nb tests
NB_MULTITENANT=1 poetry run pytest -q
poetry check
poetry run pip check
node --check nb/multitenant/static/app.js
~~~

真实 MongoDB 测试必须使用隔离测试实例：

~~~bash
MONGO_TEST_URI="mongodb://test-mongo:27017" poetry run pytest tests/test_mongo_integration.py -q
~~~

400 租户、8 worker 的真实 Mongo 并发 lease/job fencing 压测：

~~~bash
MONGO_TEST_URI="mongodb://test-mongo:27017" python scripts/mongo_load_test.py --tenants 400 --workers 8
~~~

真实 Telegram 只读预检查：

~~~bash
python scripts/telegram_preflight.py
~~~

运行中容器冒烟：

~~~bash
python scripts/container_smoke_test.py --base-url https://tg.example.com
~~~

所需变量和安全发送开关见 CODE_REVIEW.md。真实 Telegram 脚本默认不发送消息。

建议加入 CI：

~~~bash
poetry run ruff check nb tests
poetry run mypy nb/multitenant
poetry run bandit -r nb/multitenant
poetry run pip-audit
docker compose build --no-cache
docker compose config
~~~

这些工具当前未进入项目依赖，需要先锁定版本。

## 14. 备份与恢复

### 14.1 必须备份

- MongoDB 目标数据库，包括 GridFS。
- SESSION_ENCRYPTION_KEY，存入独立秘密管理系统。
- 其他平台秘密。
- 源码 commit 或镜像 digest。
- Caddy 数据卷可选；丢失后通常可重签证书。

只有 Mongo 备份、没有 SESSION_ENCRYPTION_KEY，不能恢复可用租户 Session。

### 14.2 内置 Mongo 备份

~~~bash
mkdir -p /opt/nb-backups
docker compose exec mongo sh -lc 'mongodump --username "$MONGO_INITDB_ROOT_USERNAME" --password "$(cat "$MONGO_INITDB_ROOT_PASSWORD_FILE")" --authenticationDatabase admin --db nb-multitenant --archive=/tmp/nb.archive.gz --gzip'
docker compose cp mongo:/tmp/nb.archive.gz /opt/nb-backups/nb-YYYYMMDD-HHMMSS.archive.gz
~~~

验证文件并定期异机复制，不要只放在同一磁盘。

### 14.3 恢复演练

恢复会覆盖数据。先停写、再次备份，并在隔离环境演练：

~~~bash
docker compose cp /opt/nb-backups/nb-YYYYMMDD-HHMMSS.archive.gz mongo:/tmp/restore.archive.gz
docker compose exec mongo sh -lc 'mongorestore --username "$MONGO_INITDB_ROOT_USERNAME" --password "$(cat "$MONGO_INITDB_ROOT_PASSWORD_FILE")" --authenticationDatabase admin --archive=/tmp/restore.archive.gz --gzip --drop'
~~~

恢复后使用原 SESSION_ENCRYPTION_KEY，并验证 API、Session 解密、任务/offset、作业/租约、GridFS 水印和重复租约。

## 15. 升级与回滚

升级前：

1. 阅读数据模型变化。
2. 记录当前镜像 digest/commit。
3. 备份并验证 MongoDB。
4. 保留当前 SESSION_ENCRYPTION_KEY。
5. 在预发布运行测试和 Telegram 冒烟。

更新：

~~~bash
docker compose build --pull
docker compose up -d --no-deps api dashboard
docker compose up -d --no-deps bot
docker compose up -d --no-deps --scale worker=8 worker
docker compose up -d caddy
~~~

Bot 始终单副本。当前 Compose 没有严格滚动策略，大规模生产建议使用支持健康检查和滚动部署的编排器。

回滚：

- 回到记录的 commit 或镜像 digest。
- 数据不向后兼容时先停写，再恢复对应备份。
- 不要通过更换 SESSION_ENCRYPTION_KEY 解决 Session 错误。
- 回滚后检查 running jobs 和 worker_leases。

## 16. 监控与告警

最低监控：

- <code>/health</code> 可用性和延迟。
- 容器重启次数、API 5xx、Caddy 502/504。
- Lease lost、Tenant runtime crashed、Task runtime failed。
- Tenant queue applying backpressure：持续出现时告警。
- FloodWait 次数和秒数，按 tenant 聚合。
- reauth_required 和长时间 deleting。
- task error 数量与持续时间。
- queued/running/error/cancelled job 数量与年龄。
- 过期租约、epoch 异常增长。
- Mongo 连接、延迟、磁盘、复制。
- 临时目录磁盘和 inode。
- OCR/水印进程 CPU、内存和超时。
- Caddy 证书错误。

日志不得包含 Bot Token、API Hash、Session、手机号、验证码、2FA 密码、initData、Web session token 或用户消息全文。

当前缺少结构化 request ID、指标端点和完整脱敏策略，生产可观测性需要补齐。

## 17. 安全检查清单

- [ ] .env 为 600、secrets 目录为 700、Secret 文件为 600，且均未提交。
- [x] dashboard 配置不接收平台秘密。
- [x] 应用容器使用非 root 且不再 chmod 777。
- [ ] Mongo 启用认证、TLS、最小权限、网络白名单。
- [ ] 27017 未公开。
- [ ] SESSION_ENCRYPTION_KEY 已离线备份。
- [ ] Bot 固定单副本。
- [ ] PUBLIC_BASE_URL 是正式 HTTPS。
- [ ] 主机时间同步。
- [x] Mini App 会话签发具备 Mongo 限流。
- [x] 水印像素上限已实现。
- [x] 插件失败不再静默继续。
- [x] hard delete 不保留用户名、显示名或 Session。
- [ ] 锁文件构建已在真实 Docker 中完成并通过镜像扫描。
- [ ] 备份恢复演练通过。

## 18. 常见故障

### Caddy 无法签发证书

检查 DOMAIN、A/AAAA、80/443、防火墙、端口占用和 Caddy ACME 日志。

### Mini App 签名/auth_date 无效

检查 Bot Token 是否匹配、主机 NTP、是否从 Telegram 打开、initData 是否超过五分钟。

### dashboard 未认证

必须从 <code>/web</code> 单次链接进入；检查 PUBLIC_BASE_URL、HTTPS Secure Cookie 和 Caddy 对 <code>/dashboard</code> WebSocket 的代理。

### 账号变成 reauth_required

可能是授权撤销、session_encryption_key 不匹配或密文损坏。不要直接替换密钥。删除请求遇到无法解密的 Session 会保持 deleting 并退避重试；恢复原密钥后再次请求删除。

### 任务验证失败

检查来源可读、公开频道可加入、私密频道已手工加入、目标有发言/发帖权限、水印已上传、正则有效。

### 消息延迟或漏转

检查 FloodWait、Tenant queue applying backpressure、Task runtime failed、Lease lost 和 Mongo 延迟。队列满时现在会施加背压；长期背压说明单账号处理能力不足或 Telegram 正在限流。

### worker 有容量但租户不运行

检查账号/任务状态、worker_leases、WORKER_CLIENT_CAPACITY，并确认副本没有相同 WORKER_ID。

## 19. 最终生产验收

只有同时满足以下条件才标记为生产就绪：

1. CODE_REVIEW.md 的代码级修复保持通过。
2. 自动测试通过，真实 MongoDB 测试不再 skipped。
3. Docker 镜像按锁文件可复现并通过漏洞扫描。
4. Mongo 认证、备份、高可用和恢复演练完成。
5. 真实测试账号覆盖二维码、验证码、2FA、权限和完整解绑。
6. 评论、机器人媒体和插件按任务隔离通过。
7. worker 故障、租约接管、FloodWait 不阻塞其他租户。
8. 接近目标规模的负载测试达到容量和延迟目标。
9. 告警、日志脱敏和操作手册生效。
10. SESSION_ENCRYPTION_KEY 和回滚版本安全备份。

在此之前，限定为预发布和专用测试账号。
