# Web2API 部署指南

本文档介绍如何将 Web2API 部署到本地 Ubuntu、远程 VPS 或 Render 平台。

---

## 目录

- [系统要求](#系统要求)
- [方式一：Docker 部署（推荐）](#方式一docker-部署推荐)
- [方式二：Docker Compose 部署](#方式二docker-compose-部署)
- [方式三：Ubuntu 裸机部署](#方式三ubuntu-裸机部署)
- [方式四：Render 部署](#方式四render-部署)
- [环境变量参考](#环境变量参考)
- [部署后配置](#部署后配置)
- [常见问题](#常见问题)

---

## 系统要求

| 项目 | 最低要求 | 推荐配置 |
|------|---------|---------|
| CPU | 1 核 | 2 核+ |
| 内存 | 1 GB | 2 GB+ |
| 磁盘 | 2 GB | 5 GB+ |
| 系统 | Ubuntu 22.04+ / Debian 12+ | Ubuntu 24.04 |
| Python | 3.12+ | 3.12 |
| 架构 | amd64 / arm64 | amd64 |

---

## 方式一：Docker 部署（推荐）

最简单的部署方式，适用于本地 Ubuntu 和远程 VPS。

### 1. 安装 Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# 重新登录使 docker 组生效
```

### 2. 构建镜像

```bash
git clone https://github.com/shenhao-stu/web2api.git
cd web2api
git checkout feat/huggingface-postgres-space

docker build -t web2api .
```

### 3. 运行容器

```bash
docker run -d \
  --name web2api \
  -p 9000:9000 \
  -v web2api-data:/data \
  -e WEB2API_AUTH_API_KEY="your-api-key-here" \
  -e WEB2API_AUTH_CONFIG_SECRET="your-admin-password" \
  -e WEB2API_BROWSER_NO_SANDBOX=true \
  -e WEB2API_BROWSER_DISABLE_GPU=true \
  -e WEB2API_BROWSER_DISABLE_GPU_SANDBOX=true \
  web2api
```

### 4. 验证

```bash
# 检查服务状态
curl http://localhost:9000/claude/v1/models \
  -H "Authorization: Bearer your-api-key-here"

# 测试对话
curl http://localhost:9000/claude/v1/chat/completions \
  -H "Authorization: Bearer your-api-key-here" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4.6","stream":false,"messages":[{"role":"user","content":"Hello"}]}'
```

---

## 方式二：Docker Compose 部署

适合需要 PostgreSQL 持久化配置的场景。

创建 `docker-compose.yml`：

```yaml
services:
  web2api:
    build: .
    ports:
      - "9000:9000"
    volumes:
      - web2api-data:/data
    environment:
      - WEB2API_AUTH_API_KEY=your-api-key-here
      - WEB2API_AUTH_CONFIG_SECRET=your-admin-password
      - WEB2API_BROWSER_NO_SANDBOX=true
      - WEB2API_BROWSER_DISABLE_GPU=true
      - WEB2API_BROWSER_DISABLE_GPU_SANDBOX=true
      - WEB2API_DATABASE_URL=postgresql://web2api:web2api@db:5432/web2api
    depends_on:
      db:
        condition: service_healthy
    restart: unless-stopped

  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: web2api
      POSTGRES_PASSWORD: web2api
      POSTGRES_DB: web2api
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U web2api"]
      interval: 5s
      timeout: 3s
      retries: 5
    restart: unless-stopped

volumes:
  web2api-data:
  pgdata:
```

```bash
docker compose up -d
```

---

## 方式三：Ubuntu 裸机部署

适合不想用 Docker 的场景，或需要更精细控制的 VPS。

### 1. 安装系统依赖

```bash
sudo apt-get update && sudo apt-get install -y \
  ca-certificates curl xz-utils xvfb xauth \
  python3 python3-pip python3-venv python-is-python3 \
  software-properties-common fonts-liberation \
  libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 \
  libcairo2 libcups2t64 libdbus-1-3 libdrm2 \
  libfontconfig1 libgbm1 libglib2.0-0t64 libgtk-3-0t64 \
  libnspr4 libnss3 libpango-1.0-0 libu2f-udev \
  libvulkan1 libx11-6 libx11-xcb1 libxcb1 \
  libxcomposite1 libxdamage1 libxext6 libxfixes3 \
  libxkbcommon0 libxrandr2 libxrender1 libxshmfence1
```

> 注意：Ubuntu 22.04 上部分包名不带 `t64` 后缀，如 `libasound2`、`libcups2` 等。

### 2. 安装 Fingerprint Chromium

```bash
# AMD64
sudo mkdir -p /opt/fingerprint-chromium
curl -L "https://github.com/adryfish/fingerprint-chromium/releases/download/142.0.7444.175/ungoogled-chromium-142.0.7444.175-1-x86_64_linux.tar.xz" \
  -o /tmp/fp-chromium.tar.xz
sudo tar -xf /tmp/fp-chromium.tar.xz -C /opt/fingerprint-chromium --strip-components=1
rm /tmp/fp-chromium.tar.xz

# 验证
/opt/fingerprint-chromium/chrome --version
```

### 3. 安装 Python 依赖

```bash
cd /opt
git clone https://github.com/shenhao-stu/web2api.git
cd web2api
git checkout feat/huggingface-postgres-space

python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install curl-cffi fastapi playwright pyyaml python-dotenv pydantic pytz "uvicorn[standard]"

# 如需 PostgreSQL 支持
pip install "psycopg[binary]"
```

### 4. 创建配置文件

```bash
mkdir -p /data
cp docker/config.container.yaml /data/config.yaml
```

根据需要编辑 `/data/config.yaml`，或通过环境变量覆盖。

### 5. 创建 systemd 服务

```bash
sudo tee /etc/systemd/system/web2api.service > /dev/null << 'EOF'
[Unit]
Description=Web2API Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/web2api
Environment=DISPLAY=:99
Environment=WEB2API_DATA_DIR=/data
Environment=WEB2API_CONFIG_PATH=/data/config.yaml
Environment=WEB2API_AUTH_API_KEY=your-api-key-here
Environment=WEB2API_AUTH_CONFIG_SECRET=your-admin-password
Environment=WEB2API_BROWSER_NO_SANDBOX=true
Environment=WEB2API_BROWSER_DISABLE_GPU=true
Environment=WEB2API_BROWSER_DISABLE_GPU_SANDBOX=true
Environment=HOME=/data
Environment=PYTHONUNBUFFERED=1
ExecStartPre=/bin/bash -c 'rm -rf /data/fp-data && mkdir -p /data/fp-data /tmp/.X11-unix'
ExecStartPre=/bin/bash -c 'rm -f /tmp/.X99-lock; Xvfb :99 -screen 0 1600x900x24 -nolisten tcp -ac &'
ExecStartPre=/bin/sleep 1
ExecStart=/opt/web2api/.venv/bin/python -u /opt/web2api/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now web2api
```

### 6. 查看日志

```bash
sudo journalctl -u web2api -f
```

---

## 方式四：Render 部署

Render 支持 Docker 部署，流程与 VPS 类似。

### 1. 创建 Render Web Service

1. 登录 [Render Dashboard](https://dashboard.render.com)
2. New → Web Service → 连接 GitHub 仓库 `shenhao-stu/web2api`
3. 选择分支 `feat/huggingface-postgres-space`
4. 配置：
   - **Environment**: Docker
   - **Instance Type**: Starter ($7/月) 或更高
   - **Disk**: 添加 1 GB 持久化磁盘，挂载到 `/data`

### 2. 设置环境变量

在 Render Dashboard → Environment 中添加：

| Key | Value |
|-----|-------|
| `WEB2API_AUTH_API_KEY` | 你的 API 密钥 |
| `WEB2API_AUTH_CONFIG_SECRET` | 管理后台密码 |
| `WEB2API_BROWSER_NO_SANDBOX` | `true` |
| `WEB2API_BROWSER_DISABLE_GPU` | `true` |
| `WEB2API_BROWSER_DISABLE_GPU_SANDBOX` | `true` |
| `PORT` | `9000` |

### 3. （可选）添加 PostgreSQL

1. Render Dashboard → New → PostgreSQL
2. 创建后复制 Internal Database URL
3. 添加环境变量 `WEB2API_DATABASE_URL` = 复制的 URL

### 4. 部署

Render 会自动构建 Docker 镜像并部署。部署完成后通过 `https://your-service.onrender.com` 访问。

---

## 环境变量参考

### 必需

| 变量 | 说明 | 示例 |
|------|------|------|
| `WEB2API_AUTH_API_KEY` | API 认证密钥 | `sk-your-key` |
| `WEB2API_AUTH_CONFIG_SECRET` | 管理后台密码 | `admin123` |

### 浏览器

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `WEB2API_BROWSER_NO_SANDBOX` | 禁用沙箱（容器必须） | `false` |
| `WEB2API_BROWSER_DISABLE_GPU` | 禁用 GPU | `false` |
| `WEB2API_BROWSER_DISABLE_GPU_SANDBOX` | 禁用 GPU 沙箱 | `false` |
| `WEB2API_BROWSER_HEADLESS` | 无头模式 | `false`（使用 Xvfb） |
| `WEB2API_BROWSER_CDP_PORT_START` | CDP 起始端口 | `9222` |
| `WEB2API_BROWSER_CDP_PORT_COUNT` | CDP 端口数量 | `20` |

### 服务器

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `HOST` / `WEB2API_SERVER_HOST` | 监听地址 | `0.0.0.0` |
| `PORT` / `WEB2API_SERVER_PORT` | 监听端口 | `9000` |
| `WEB2API_DATABASE_URL` | PostgreSQL 连接串 | 空（使用 SQLite） |

### 调度器

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `WEB2API_SCHEDULER_TAB_MAX_CONCURRENT` | 单 tab 最大并发 | `1` |
| `WEB2API_SCHEDULER_RESIDENT_BROWSER_COUNT` | 预热浏览器数 | `1` |

### 覆盖规则

所有 `config.yaml` 中的配置项都可以通过环境变量覆盖：

```
config.yaml 中的 section.key → WEB2API_SECTION_KEY
```

例如：`claude.api_base` → `WEB2API_CLAUDE_API_BASE`

---

## 部署后配置

1. 访问 `http://your-host:9000/login`，输入 `WEB2API_AUTH_CONFIG_SECRET` 设置的密码
2. 进入 `/config` 管理页面
3. 添加代理组（Proxy Group）：
   - 如不需要代理，取消勾选 `use_proxy`，`fingerprint_id` 填任意唯一标识
4. 添加 Claude 账号：
   - `name`：任意名称
   - `type`：`claude`
   - `auth`：`{"sessionKey": "你的 Claude sessionKey"}`
5. 点击 Save config
6. （可选）开启 Pro models 开关以使用 Haiku / Opus 模型

### 获取 sessionKey

1. 登录 [claude.ai](https://claude.ai)
2. 打开浏览器开发者工具 → Application → Cookies
3. 复制 `sessionKey` 的值

---

## 常见问题

### Page crashed / OOM

浏览器内存不足导致页面崩溃。解决方案：
- 升级服务器内存到 2 GB+
- 减少 `WEB2API_SCHEDULER_TAB_MAX_CONCURRENT` 为 `1`
- 减少 `WEB2API_BROWSER_CDP_PORT_COUNT` 为 `3`
- 设置 `WEB2API_SCHEDULER_RESIDENT_BROWSER_COUNT=0` 禁用预热

### D-Bus / XKEYBOARD 警告

容器环境中的正常噪音，不影响功能和性能，可忽略。

### Page.goto: Timeout

浏览器导航超时，通常是网络问题或 Claude 服务暂时不可用。服务会自动重试（最多 3 次）。如果持续出现：
- 检查服务器到 claude.ai 的网络连通性
- 检查代理配置是否正确
- 检查 sessionKey 是否过期

### 端口冲突

默认使用 9000 端口和 9222-9241 的 CDP 端口。如有冲突：
```bash
-e PORT=8080 \
-e WEB2API_BROWSER_CDP_PORT_START=19222 \
-e WEB2API_BROWSER_CDP_PORT_COUNT=6
```
