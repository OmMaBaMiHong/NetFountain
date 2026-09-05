# NetFountain —— 两级代理 IP 池系统

一个两级代理 IP 池系统，由四个后端目录组成：`common`（公共库）、`level1_pool`（一级池）、`level2_pool`（二级池）、`proxy`（代理层），外加自包含子项目 `frontend`（Web 面板 + 数据聚合后端 BFF）。三个业务项目是独立服务，均依赖公共库 `ip_pool_common`，项目之间不相互 import，仅通过 HTTP API 通信；`frontend` 仅经 HTTP 访问三个服务，不修改其后端代码。

## 系统架构

```
  供应商公网 IP 池 (HTTP, 1s/10个)
         │
         ▼
 ┌──────────────────────────────┐
 │      一级池 level1_pool       │  拉取(91HTTP/供应商) → 代理可达性测试 → 环形池(500+TTL)
 │       (0.0.0.0:8000)         │  拉取与测试解耦 · proxy_url 去重 · HTTP API /api/v1
 └──────────────┬───────────────┘
                │ HTTP 增量同步 (/api/v1/ips, /ips/after/{id})
      ┌─────────┴─────────┐
      ▼                   ▼
 ┌────────────┐    ┌────────────┐
 │ 二级池 A    │    │ 二级池 B    │   每站点独立进程
 │ level2_pool│    │ level2_pool│   同步 → 站点连通测试(<2000ms) → 租赁池
 │ (:8001)    │    │ (:8002)    │
 └─────┬──────┘    └─────┬──────┘
       │ HTTP            │ HTTP
       └────────┬────────┘
                ▼
 ┌──────────────────────────────┐
 │      代理层 proxy (:9000)     │  按站点路由 → 透传请求/响应
 │      /api/v1/{site}/...      │  每分钟重载路由表
 └──────────────┬───────────────┘
                │ HTTP
                ▼
 ┌──────────────────────────────┐
 │   前端面板 frontend (:3000)   │  BFF 定时采集三服务，聚合 /api/*
 │  总览/IP列表/站点/统计分析    │  Express5+node:sqlite+Vue3+ECharts（dev :5173）
 └──────────────┬───────────────┘
                │ HTTP（浏览器只访问 3000，禁止直连 8000/8001/9000）
                ▼
              用户
```

## 目录职责

| 目录 | 职责 | 端口 |
|---|---|---|
| `common` | 公共库 `ip_pool_common`：数据模型、代理测试原语、配置加载、日志、API 通用件。被三项目依赖，不依赖任何业务项目。 | — |
| `level1_pool` | 一级池：从多个供应商（91HTTP / freeproxy / default_http，一个配置文件 `global`+`providers` 多供应商配置）拉取 IP、代理可达性测试（每供应商独立拉取器与测试管线，拉取与测试解耦多 worker）、按 `proxy_url` 去重入共享环形池、TTL/容量淘汰、对外查询 API。 | 8000 |
| `level2_pool` | 二级池：从一级池增量同步、站点连通测试（延迟 < 2000ms 才入池）、租赁分配（提取策略：最新/随机/延迟升序/剩余时间降序，可筛选延迟与剩余时间，支持批量提取）/释放/删除、周期复验。一个配置文件 `global`+`pools` 自动多开（单进程多线程），子池配置覆盖全局。 | 8001+ |
| `proxy` | 代理层：按站点标识路由到对应二级池并纯透传请求/响应，每分钟热更新路由表；支持账号定向池（凭据决定下游从哪个二级池租 IP）。 | 9000 |
| `frontend` | Web 面板 + 数据聚合后端 BFF（Vue3 + Express5 + node:sqlite）：只经 HTTP 定时聚合三服务数据并提供可视化。 | 3000/5173 |

## 账号定向池（接口调用方身份识别）

代理层支持给下游服务分配账号：凭据决定它从哪个二级池租 IP，没注册的一律走默认池。

- **注册**：`POST /api/v1/accounts`，body `{"username":"sub2api","password":"***","assigned_site":"zhihu"}`；
  列表 `GET /api/v1/accounts`；删除 `DELETE /api/v1/accounts/{username}`（管理接口仅限内网）；
- **带凭据调用**：租还类接口（acquire / acquire-batch / release / delete / release-all）带
  `Authorization: Basic <user:pass>`（即 `curl -u user:pass`），校验通过后强制走该账号绑定的池，
  请求其它池回 403；凭据错误回 401；
- **无凭据调用**：只允许访问默认池（`proxy_routes.yaml` 的 `auth.default_site`，空 = 路由表第一个站点）；
- **只读接口**（status / count / ips / health）不鉴权，前端面板不受影响；
- 账号库：`proxy/data/accounts.db`（SQLite，密码加盐哈希存储，不入 git）。

```bash
# 注册：把 sub2api 绑定到 zhihu 池
curl -X POST http://127.0.0.1:9000/api/v1/accounts \
     -H 'Content-Type: application/json' \
     -d '{"username":"sub2api","password":"s3cret","assigned_site":"zhihu"}'

# 带凭据租 IP（强制走 zhihu 池）
curl -u sub2api:s3cret -X POST http://127.0.0.1:9000/api/v1/zhihu/ips/acquire
```

## 隧道代理入口（一个端口 + 凭据，对齐行业惯例）

代理层可开启标准隧道代理入口（`proxy/config/proxy_routes.yaml` 的 `tunnel` 段，默认关闭）：下游把 `http://user:pass@代理层主机:9001` 填进自己的代理配置，即可使用整个池——HTTP 自动转发、HTTPS 走 CONNECT 隧道（支持流式），每个请求自动从池里取出口 IP，坏 IP 自动换（`max_attempts`），用完归还。

- **凭据定池**：凭据就是 `accounts` 账号表的账号（与 9000 的账号定向池同一套）；无凭据走默认池（`auth.default_site`）；凭据缺失/错误回 **407**；
- **独立端口**：默认 9001，与 9000 管理 API 互不干扰，不做协议分拣、没有内部端口，uvicorn 启动方式不变；
- **池空等待**：按 `acquire_max_wait` 秒轮询等待，超时回 502。

```yaml
# proxy/config/proxy_routes.yaml
tunnel:
  enabled: true
  port: 9001          # 独立代理入口端口
  max_attempts: 4     # 单请求最多换几个出口 IP
  connect_timeout: 10.0
  acquire_max_wait: 30.0
```

```bash
# 经隧道入口请求（走 u1 绑定的池，凭据错误回 407）
curl -x http://u1:pw@127.0.0.1:9001 http://www.baidu.com -i
# HTTPS 走 CONNECT 隧道
curl -x http://u1:pw@127.0.0.1:9001 https://www.zhihu.com -I
```


## 服务安装与启动

各项目先安装依赖（公共库以 editable 方式引用），再以 uvicorn 启动。

```bash
# 一级池
pip install -r level1_pool/requirements.txt
uvicorn app.main:app --app-dir level1_pool --host 0.0.0.0 --port 8000

# 二级池（单进程多开，每站点一个子池线程）
pip install -r level2_pool/requirements.txt
cd level2_pool && python -m app.launcher
# 旧方式（单实例，读取同一配置文件）
# uvicorn app.main:app --app-dir level2_pool --host 0.0.0.0 --port 8001

# 代理层
pip install -r proxy/requirements.txt
uvicorn app.main:app --app-dir proxy --host 0.0.0.0 --port 9000
```

前端面板（探针界面，自包含子项目，需 Node ≥ 22.5）定时轮询三个服务做聚合展示（总览 / IP 列表 / 站点视图 / 统计分析），浏览器只访问 3000 端口：

```bash
cd frontend
npm install
npm run dev                  # 开发：BFF(3000) 与 Vite(5173) 一键同起
npm run build && npm start   # 生产：构建后同进程托管 dist/ → http://localhost:3000
```

## 文档索引

### 使用说明

| 文档 | 内容 | 适用对象 |
|---|---|---|
| `USAGE.md` | 使用说明：代理层网关对外 API，从池中获取 / 释放 / 删除 IP 的完整流程 | 使用方（用户 / 调用方） |
| `API_USAGE.md` | API 使用说明：三个服务全部 HTTP API（字段、参数、示例、错误码） | 开发者 / 运维 |
| `DEPLOYMENT.md` | 部署使用说明：环境准备、依赖安装、配置、启动、验证、运维排障 | 部署 / 运维 |

### 项目文档

- 公共库：`common/README.md`、`common/测试计划书.md`
- 一级池：`level1_pool/项目策划书.md`、`level1_pool/测试计划书.md`
- 二级池：`level2_pool/项目策划书.md`、`level2_pool/测试计划书.md`
- 代理层：`proxy/项目策划书.md`、`proxy/测试计划书.md`
- 前端面板 + BFF：`frontend/README.md`（技术文档：架构、BFF 设计、API、前端设计、约束）

## 跨服务契约要点

- **统一响应结构**：所有服务返回 `{code, msg, data}`。`code=0` 表示成功；非 0 为错误码（如 40402 空池、40400 站点未配置）。
- **一级池 → 二级池**：`GET /api/v1/ips` 返回全部 IP；`GET /api/v1/ips/after/{id}` 返回 `id` 之后（增量）的 IP，响应顶层带 `max_id`（当前池内最大 id）。增量返回为空时，二级池仅当水位线已越过 `max_id`（一级池重启/换代）才触发全量重拉；水位线等于 `max_id` 视为暂无新 IP，不做全量提取。
- **一级池去重**：一级池按 `proxy_url`（ip+port+protocol）去重，重复入池删除旧记录并以新 id 重建（刷新 ttl/region）；仅当 region 未变且新 ttl 严格小于旧 ttl（上次拉取返回值）时直接跳过不更新；`/status` 的 `total_duplicates` 只累计跳过次数，重建产生的新 id 会驱动二级池增量同步感知刷新。
- **代理层 → 二级池**：`/api/v1/{site}/...` 剥离 `{site}` 段后透传到对应二级池，`{code,msg,data}` 原样透传。
- **协议枚举**：`http`、`https`、`socks4`、`socks5`。
- **公共库引用**：`requirements.txt` 中 `-e ../common`（editable install），安装后 `import ip_pool_common`。
