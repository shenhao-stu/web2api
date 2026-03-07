# Web2API

把“网页上的 AI 服务”包装成 **OpenAI 兼容接口**。

如果你已经有：

- 代理
- 站点登录态（例如 Claude 的 `sessionKey`）
- 想让 `OpenAI SDK`、`Cursor`、或者任何兼容 `/v1/chat/completions` 的客户端直接调用

那这个项目就是干这个的。

当前仓库默认内置的是 `claude` 插件，也就是说你可以像调 OpenAI 一样去调 Claude 网页端。

## 这个项目到底做了什么

简单说，它在本地帮你做了这几件事：

1. 启动一个真实浏览器
2. 用你的代理和登录态打开网页
3. 帮你维持网页会话
4. 对外暴露一个 OpenAI 风格的 HTTP API

你调用的是：

```text
POST /claude/v1/chat/completions
```

项目内部实际做的是：

```text
代理组 -> 浏览器 -> Claude tab -> 网页会话
```

## 适合谁

适合下面这类场景：

- 你已经有现成的 OpenAI 客户端，但想换成网页端 Claude
- 你想让 Cursor 之类的工具接一个“看起来像 OpenAI”的后端
- 你不想手写浏览器自动化细节，只想配好账号就能用

如果你只是想“体验 Claude 网页”，这个项目不适合你；它更像一个给开发者用的桥接服务。

## 4 个概念

- `代理组`
  一组代理配置，对应一个浏览器进程。
- `type`
  某种站点能力。当前仓库默认是 `claude`。
- `账号`
  某个 `type` 的登录态。Claude 一般就是 `sessionKey`。
- `会话`
  一次聊天上下文。项目会尽量复用，重启后如果复用不了，就会自动新建并把历史对话重新发过去。

## 快速开始

### Docker 一条命令启动

如果你是在 `Linux` 服务器上部署，仓库根目录已经提供了 `Dockerfile` 和 `compose.yaml`。

如果你用源码构建镜像，直接运行：

```bash
docker compose up -d --build
```

仓库已经发布镜像，也可以直接一条命令拉起：

```bash
docker run -d \
  --name web2api \
  --restart unless-stopped \
  --platform linux/amd64 \
  --shm-size=1g \
  -p 9000:9000 \
  -v "$(pwd)/docker-data:/data" \
  ghcr.io/caiwuu/web2api:latest
```

如果你是在 `Apple Silicon` 的 Mac 上运行 Docker，也建议显式使用 `linux/amd64`，因为当前内置的 `fingerprint-chromium` 是 `x86_64 Linux` 版本。

`macOS / Apple Silicon` 用户再注意 4 点：

- 首次 `docker compose up -d --build` 会比较慢  
  因为这里会下载浏览器、安装依赖，并且通过 `qemu` 运行 `linux/amd64` 镜像。
- `compose.yaml` 里已经内置了 `platform: linux/amd64`  
  正常情况下不需要你手动再改。
- 如果你之前拉起过旧容器，建议直接重新构建  
  用 `docker compose up -d --build`，不要只执行 `docker compose up -d`。
- 启动后先看日志，再打开配置页  
  用 `docker logs -f web2api`，看到 `服务已就绪` 后再访问页面。

还有一个限制需要明确：

- 当前内置的 `fingerprint-chromium` 是 `x86_64 Linux` 版本  
  在 `Apple Silicon + Docker + linux/amd64(qemu)` 下，Chromium 可能出现 `connect_over_cdp ECONNREFUSED`、`Target page/context/browser has been closed`、`GPU process isn't usable` 这类崩溃。
- 即使开启 `browser.disable_gpu=true` 和 `browser.disable_gpu_sandbox=true`，也不保证能彻底解决  
  这两个参数更适合做容器兼容性调优，不是跨架构模拟环境下的根治方案。
- 如果你要稳定使用，优先顺序建议是：  
  `x86_64 Linux 宿主机 / x86_64 Linux VPS > macOS 本机源码运行 > Apple Silicon Docker`

启动后：

- API 地址：`http://127.0.0.1:9000`
- 配置页：`http://127.0.0.1:9000/config`
- 持久化目录：`./docker-data`

容器首次启动时会自动把默认配置写到：

- `./docker-data/config.yaml`

默认镜像内已经包含：

- `fingerprint-chromium`
- `Xvfb`
- 运行 Chromium 所需的 Linux 依赖

如果想快速确认容器是否真的正常起来，可以直接执行：

```bash
docker compose ps
docker logs --tail=200 web2api
```

正常情况下你会看到类似输出：

```text
INFO:     Started server process [1]
INFO:     Waiting for application startup.
服务已就绪，已注册 type: claude
INFO:     Uvicorn running on http://0.0.0.0:9000
```

如果你不是用 Docker，再看下面的源码运行方式。

### 1. 准备环境

你需要先准备好：

- Python `3.12+`
- [`uv`](https://github.com/astral-sh/uv)
- 指纹浏览器 [fingerprint-chromium](https://github.com/adryfish/fingerprint-chromium)
- 可用代理
- 可用的 Claude `sessionKey`

### 2. 安装依赖

```bash
uv sync
```

### Linux 用户：推荐用虚拟屏幕启动

如果你是在 `Linux` 服务器或 `Docker` 容器里常驻运行，建议配合 `Xvfb`，这样浏览器不会占用桌面，同时仍然是“有界面浏览器”。

先安装：

```bash
sudo apt update
sudo apt install -y xvfb
```

启动时这样跑：

```bash
xvfb-run -a -s "-screen 0 1920x1080x24" uv python run main.py
```

这样做比直接改成 `headless` 更稳，原因是：

- 站点兼容性通常更好
- 扩展和登录态行为更接近真实浏览器
- 更不容易因为无头模式差异触发风控

如果你是放在 `Docker` 里运行，也建议沿用这个思路：

- 容器内跑 `Chromium + Xvfb`
- 持久化 `config.yaml`、`db.sqlite3`、浏览器数据目录
- 给 Chromium 足够的共享内存，例如更大的 `/dev/shm`

### 3. 检查 `config.yaml`

项目根目录有一个 [config.yaml](/Users/caiwu/code/CDPDemo/config.yaml)，它主要控制：

- 服务端口
- 浏览器可执行文件路径
- 调度与回收参数
- mock 调试端口

你至少要确认这一项是对的：

```yaml
browser:
  chromium_bin: '/Applications/Chromium.app/Contents/MacOS/Chromium'
```

如果你遇到浏览器在 Linux / Docker 环境里启动后立刻关闭，也可以先尝试这组兼容参数：

```yaml
browser:
  no_sandbox: true
  disable_gpu: true
  disable_gpu_sandbox: true
```

注意：

- 这更适合容器、Xvfb、远程桌面环境
- 对本机桌面环境通常不需要
- 对 `Apple Silicon Docker + x86_64 fingerprint-chromium`，这组参数也不保证稳定

当前仓库示例端口是：

```yaml
server:
  port: 9000
```

### 4. 启动服务

```bash
uv python run main.py
```

如果启动成功，你会看到类似日志：

```text
服务已就绪，已注册 type: claude
Uvicorn running on http://127.0.0.1:9000
```

### 5. 打开配置页，填入网络和账号

浏览器访问：

```text
http://127.0.0.1:9000/config
```

在里面填：

- `fingerprint_id`
- 账号 `name`
- 账号 `type=claude`
- 账号 `auth.sessionKey`

如果你需要代理：

- 勾选“使用代理”
- 填 `proxy_host`
- 按需填 `proxy_user`
- 按需填 `proxy_pass`

如果你本身就在可用地区、自用且不需要切 IP：

- 取消“使用代理”
- 这时浏览器会走当前机器的直连出口
- 但最终是否可用，仍取决于你机器本身的出口地区和风控情况

保存后立即生效。

配置时建议注意一件事：

- 不要在同一个 `代理组` 下面堆很多同类型账号  
  例如同一个 IP 下面挂大量 `claude` 账号。这样虽然看起来方便调度，但更容易让站点把这些账号关联到同一出口 IP，增加风控和封号风险。

### 6. 发第一条请求

```bash
curl -s "http://127.0.0.1:9000/claude/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "s4",
    "stream": false,
    "messages": [
      {"role":"user","content":"你好，简单介绍一下你自己。"}
    ]
  }'
```

## 如果你写的是自己的客户端，请注意

项目会把会话 ID 以**不可见字符**的形式附在 assistant 回复末尾。

这意味着：

- 如果你直接用 OpenAI SDK / Cursor，通常不用管
- 如果你自己保存聊天记录，不要把 assistant 文本里的零宽字符清洗掉

否则下一轮请求时，服务端可能没法继续复用会话。

## FAQ

### 为什么不直接封装网络数据包，而要打开一个真实浏览器？

因为这个项目优先追求的是**稳定复用网页侧真实能力**，不是做一个“看起来更轻”的抓包转发器。

直接封装网络包当然更省资源，但长期使用时通常有这些问题：

- 登录态不只是一个固定 Cookie  
  很多站点除了 `sessionKey`，还依赖浏览器里的本地存储、页面初始化状态、动态 token 等运行时上下文。

- 前端协议不是静态不变的  
  有些请求字段是前端 JS 在运行时组装的，站点一改前端逻辑，纯抓包方案就容易失效。

- 更容易碰到风控  
  真实浏览器天然更接近站点预期的访问行为；纯脚本直连接口更容易因为指纹、请求时序、上下文缺失而被拦。

- 会话复用更自然  
  这个项目需要长期维持网页会话、支持断点续聊、支持账号切换后的调度。浏览器方案更容易和网页端保持一致。

- 调试成本更低  
  出问题时可以直接看真实页面、真实登录态、真实请求环境，而不是只盯着抓包日志猜协议哪里变了。

一句话说，这个项目是用更高的资源成本，换更强的稳定性、兼容性和可维护性。

当然，浏览器方案也有代价：

- 更吃内存
- 冷启动更慢
- 调度逻辑更复杂

所以这不是最轻的方案，而是更适合“长期跑、尽量少因为站点变动而失效”的方案。

### 浏览器弹窗很烦，怎么处理？

如果你是在本机直接运行，这个项目会启动真实浏览器，所以看到窗口弹出是正常的。

针对 `Linux` 用户，推荐直接看上面的“快速开始 -> Linux 用户：推荐用虚拟屏幕启动”。

如果你是在 `macOS` 本机上运行，更现实的做法通常不是强行隐藏本机窗口，而是把项目放到 `Docker` 或远程 `Linux` 环境里运行。

如果你接入的是**检测不严格**的平台，也可以考虑增加或开启 `headless` 模式，来减少窗口干扰和图形环境依赖。

对应配置在 [config.yaml](/Users/caiwu/code/CDPDemo/config.yaml)：

```yaml
browser:
  headless: true
```

但要注意：

- `headless` 更适合风控弱、页面行为简单的平台
- 对依赖真实浏览器环境的平台，兼容性通常不如“有界面浏览器 + 虚拟屏幕”
- 对 `claude` 这类检测更严格的平台，不推荐优先走 `headless`

所以默认建议仍然是：

- `claude`：优先真实浏览器，`Linux` 下配合 `Xvfb`
- 其它检测不严格的平台：可以再评估是否使用 `headless`

## API 示例

### 列出模型

```bash
curl "http://127.0.0.1:9000/claude/v1/models"
```

### 非流式

```bash
curl -s "http://127.0.0.1:9000/claude/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "s4",
    "stream": false,
    "messages": [
      {"role":"user","content":"给我 3 条学习建议。"}
    ]
  }'
```

### 流式

```bash
curl -N "http://127.0.0.1:9000/claude/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "s4",
    "stream": true,
    "messages": [
      {"role":"user","content":"用三点总结今天的计划。"}
    ]
  }'
```

## 调试用 Mock

如果你暂时不想连真实 Claude，可以先启动 mock：

```bash
uv run python main_mock.py
```

然后把 [config.yaml](/Users/caiwu/code/CDPDemo/config.yaml) 里的这两项改成 mock 地址：

```yaml
claude:
  start_url: 'http://127.0.0.1:8002/mock'
  api_base: 'http://127.0.0.1:8002/mock'
```

这样主服务就会把请求打到本地 mock，而不是打到真实 Claude。

## 这个项目现在支持什么

当前仓库默认支持：

- `claude`

项目本身是插件化的，后续可以继续扩展别的 `type`，但如果你刚开始使用，先把 Claude 跑通就行。

## 项目结构

你不需要一开始就看懂全部代码，先知道这些入口就够了：

- [main.py](/Users/caiwu/code/CDPDemo/main.py)
  主服务入口
- [main_mock.py](/Users/caiwu/code/CDPDemo/main_mock.py)
  mock 服务入口
- [core/app.py](/Users/caiwu/code/CDPDemo/core/app.py)
  应用组装
- [core/api/](/Users/caiwu/code/CDPDemo/core/api)
  OpenAI 兼容接口
- [core/plugin/](/Users/caiwu/code/CDPDemo/core/plugin)
  各种站点插件
- [core/runtime/](/Users/caiwu/code/CDPDemo/core/runtime)
  浏览器、tab、会话调度

如果你想看更底层的设计，再去读：

- [docs/architecture.md](/Users/caiwu/code/CDPDemo/docs/architecture.md)
- [docs/page-pool-scheme.md](/Users/caiwu/code/CDPDemo/docs/page-pool-scheme.md)

## 开发检查

```bash
uv run ruff check .
```

## 安全提醒

请不要把这些内容提交到公开仓库：

- `db.sqlite3`
- 代理账号密码
- `sessionKey`
- 抓包数据
- 任何真实用户对话

另外也不建议这样使用：

- 在同一个代理组下集中堆很多同类型账号

更稳妥的做法是把同类型账号分散到不同 IP / 不同代理组，降低账号关联和风控风险。

## 最后一句话

如果你是第一次接触这个项目，最推荐的路径是：

1. 先把 `config.yaml` 里的端口和浏览器路径改对
2. 启动服务
3. 去 `/config` 配一个 Claude 账号
4. 用上面的 `curl` 发第一条消息
5. 成功之后再去看架构文档

先跑通，再读源码，会轻松很多。
