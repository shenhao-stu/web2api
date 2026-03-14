# 配置说明

项目根目录的 [config.yaml](../config.yaml) 主要控制：

- 服务端口
- API Key 鉴权
- 配置页登录密钥
- 浏览器可执行文件路径
- 调度与回收参数
- mock 调试端口

## 配置文件优先级

如果你本地经常需要改配置、又不想每次提交都处理 `config.yaml` 的变更，可以在项目根目录新建 `config.local.yaml`。

加载优先级：

1. `WEB2API_CONFIG_PATH` 环境变量
2. `config.local.yaml`
3. `config.yaml`

此外，单个配置项也支持环境变量覆盖：

- 通用规则：`WEB2API_<SECTION>_<KEY>`
- 额外兼容：
  - `server.host` 也支持 `HOST`
  - `server.port` 也支持 `PORT`

例如：

```bash
export PORT=9000
export WEB2API_AUTH_API_KEY='your-secret-key'
export WEB2API_AUTH_CONFIG_SECRET='your-admin-secret'
```

## 关键配置项

### 服务端口

```yaml
server:
  port: 9000
```

### 浏览器路径

你至少要确认这一项是对的：

```yaml
browser:
  chromium_bin: '/Applications/Chromium.app/Contents/MacOS/Chromium'
```

Linux 示例：`/usr/bin/chromium` 或 `/opt/fingerprint-chromium/chrome`

### Linux / Docker 兼容参数

如果浏览器在 Linux / Docker 环境里启动后立刻关闭，可以尝试：

```yaml
browser:
  no_sandbox: true
  disable_gpu: true
  disable_gpu_sandbox: true
```

注意：这更适合容器、Xvfb、远程桌面环境；对本机桌面环境通常不需要。

### API Key 鉴权

如果不希望任何人拿到地址就能直接调用，建议配置：

```yaml
auth:
  api_key: 'your-secret-key'
```

多客户端可配置多个：

```yaml
auth:
  api_key:
    - 'client-key-1'
    - 'client-key-2'
```

启用后：

- `/{type}/v1/*` 都需要带其中一个有效 key
- 推荐请求头：`Authorization: Bearer your-secret-key`
- 修改 `auth.api_key` 后需要重启服务

### 配置页保护

如果要保护配置页面：

```yaml
auth:
  config_secret: '配置页面登录密码'
```

行为说明：

- 如果 `config_secret` 留空，`/config` 与 `/api/config` 不可访问
- **文件模式**：如果填的是明文，项目启动后会自动转换成哈希并回写到当前 `config.yaml`
- **Hosted 模式**（例如 Hugging Face Space 环境变量覆盖）：如果通过 `WEB2API_AUTH_CONFIG_SECRET` 提供明文，启动后会在内存里哈希，不回写文件
- 以后访问配置页面时，需要先打开 `/login`，输入这个明文 secret 登录
- 如果要改 secret，直接把 `config_secret` 改成新的明文，再重启服务即可
- 配置页登录默认按来源 IP 做简单限流：连续失败 5 次后锁定 600 秒，可通过 `auth.config_login_max_failures` 和 `auth.config_login_lock_seconds` 调整

### PostgreSQL 持久化

默认情况下，项目使用 SQLite。

如果设置了以下任意环境变量，配置持久化会自动切换到 PostgreSQL：

- `WEB2API_DATABASE_URL`
- `DATABASE_URL`

例如：

```bash
export WEB2API_DATABASE_URL='postgresql://user:password@host:5432/dbname?sslmode=require'
```

当前会持久化：

- 代理组
- 账号
- `auth` JSON
- `enabled`
- `unfreeze_at`

不会持久化：

- 浏览器 profile
- Chromium 本地缓存/日志
- 进程内 `SessionCache`
