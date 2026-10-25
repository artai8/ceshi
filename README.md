# NB 多租户 Telegram 转发

NB 的生产入口由四类服务组成：

- `api`：FastAPI 控制面与 Telegram Mini App。
- `bot`：仅处理私聊登录、入口和解绑命令，不监听或转发频道消息。
- `dashboard`：租户化 Streamlit 后台。
- `worker`：持有租户用户会话并执行实时、历史和定时任务，可横向扩容。

MongoDB 是新运行时唯一的数据源。旧 `nb.config.json` 和旧 Session 不会自动迁移。

## 部署

完整的环境需求、生产门禁、部署、备份、扩容和故障排查请阅读
[DEPLOYMENT.md](DEPLOYMENT.md)。当前代码审查结论和已知问题见
[CODE_REVIEW.md](CODE_REVIEW.md)。

1. 复制 `.env.example` 为 `.env`，填写平台 API ID、域名、管理员 ID 和 Mongo 用户名。
2. 按 `secrets/README.md` 创建 Mongo root/应用密码、`mongo_uri`、`bot_token`、`api_hash` 和 `session_encryption_key` 六个 Secret 文件。
3. 生成 Session 加密密钥：

   ```powershell
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

4. 将生成值写入 `secrets/session_encryption_key`，并确保 `DOMAIN` 已解析到部署主机。
5. 在 BotFather 中把 Mini App URL 配置为 `https://你的域名/miniapp`。
6. 启动服务：

   ```powershell
   docker compose up -d --build --scale worker=8
   ```

Caddy 会为域名提供 HTTPS，并将 `/dashboard` 转发到 Streamlit，其余请求转发到 FastAPI。

## 用户流程

1. 私聊控制 Bot，使用 `/login` 扫码绑定当前 Telegram 用户账号；无法扫码时使用 `/phone`、`/code` 和 `/password`。
2. 使用 `/app` 打开 Mini App，或使用 `/web` 获取五分钟有效的一次性后台链接。
3. 创建任务，填写来源、目标、模式和插件后先校验，再运行。
4. 公开频道可由用户客户端自动加入；私密邀请必须先由用户手动加入。目标频道必须允许该用户账号发消息。
5. `/logout` 会停止任务、撤销 Telegram 授权并硬删除任务、Session 密文和水印资源。

## 本地验证

```powershell
$env:NB_MULTITENANT='1'
python -m compileall -q nb
python -m pytest -q
python -m nb.cli --help
```

真实 Telegram 登录、频道权限和 FloodWait 行为必须使用测试账号在预发布环境验证。
