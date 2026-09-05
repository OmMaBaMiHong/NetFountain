# NetFountain API 使用说明

本文档仅列出本系统三个服务的全部 HTTP API 及其详细用法（字段、参数、示例、错误码），不包含架构与部署说明。

## 1. 总览

### 1.1 服务与端口

| 服务 | 默认端口 | API 前缀 |
|---|---|---|
| 一级池 `level1_pool` | 8000 | `/api/v1` |
| 二级池 `level2_pool` | 8001（默认配置 8111） | `/api/v1` |
| 代理层 `proxy` | 9000 | `/api/v1` |

### 1.2 统一响应结构

所有服务返回统一信封：

```json
{ "code": 0, "msg": "ok", "data": <任意> }
```

- `code = 0` 表示成功；非 0 为错误码。
- 失败时 `data` 为 `null`。

### 1.3 错误码表

| code | 名称 | 含义 |
|---|---|---|
| 0 | `OK` | 成功 |
| 40000 | `PARAM_ERROR` | 参数错误 |
| 40400 | `NOT_FOUND` | 站点未配置 / 对象不存在 |
| 40402 | `EMPTY_POOL` | 二级池 acquire 时空池或全部已租赁 |
| 50000 | `INTERNAL` | 内部错误 |
| 50200 | `UPSTREAM_ERROR` | 代理层上游转发失败 |

### 1.4 协议枚举

`protocol` 字段取值：`http`、`https`、`socks4`、`socks5`。

### 1.5 代理层透传规则

- 代理层为纯透传网关，不定义业务字段。
- `/{site}` 路径段表示站点标识，转发前被剥离：`/api/v1/{site}/ips/acquire` → 转发至该站点二级池的 `/api/v1/ips/acquire`。
- 请求的方法、查询参数、JSON 请求体原样转发；上游的 HTTP 状态码与响应体原样透传。
- 路由表（站点 → 上游基础 URL）每分钟热更新，见 `proxy/config/proxy_routes.yaml`。

| 情况 | HTTP 状态 | 响应体 |
|---|---|---|
| 站点未配置 | 404 | `{"code":40400,"msg":"site not configured","data":null}` |
| 上游不可达/超时 | 502 | `{"code":50200,"msg":"upstream error","data":null}` |
| 上游正常 | 上游状态码 | 上游响应体原样 |

> **日志中的业务码**：三服务启动后按 `service.log_level` 输出结构化日志，且每条 API 请求追加一行含返回业务码的访问日志，格式 `http=<HTTP状态码> biz=<业务码> method=<方法> path=<路径>`。`biz` 即响应 body 的 `code` 字段，便于同时观测 HTTP 状态与业务结果（如 `HTTP 200` + `biz=40400` 表示记录不存在）。

---

## 2. 一级池 API（level1_pool, :8000）

一级池共 4 个接口，全部为 GET、无参数、无请求体。

### 2.1 GET /api/v1/status

服务状态与统计快照。

**请求**：无参数、无请求体。

**响应 `data` 字段**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `uptime` | float | 进程启动至今秒数 |
| `total_pulled` | int | 从供应商拉取总数 |
| `total_entered` | int | 通过可达性测试入池总数 |
| `total_duplicates` | int | 因 `proxy_url` 重复被跳过更新（仅 TTL 变小且 region 未变）的累计次数 |
| `pool_size` | int | 当前池容量 |
| `counts` | dict | 各协议数量 `{http, https, socks4, socks5}` |
| `api_call_count` | int | 累计 `/api/v1` 调用次数 |
| `next_id` | int | 全局自增 ID 水位 |
| `errors` | object | 错误计数 `{pull_failures, test_failures, ttl_sweep_failures}` |
| `drops` | int | 因队满被丢弃的待测批次累计数 |

**示例**：

```json
{
  "code": 0, "msg": "ok",
  "data": {
    "uptime": 1234.567,
    "total_pulled": 1000,
    "total_entered": 800,
    "total_duplicates": 12,
    "pool_size": 500,
    "counts": {"http": 300, "https": 100, "socks4": 50, "socks5": 50},
    "api_call_count": 42,
    "next_id": 812,
    "errors": {"pull_failures": 3, "test_failures": 0, "ttl_sweep_failures": 0},
    "drops": 0
  }
}
```

### 2.2 GET /api/v1/count

仅返回池容量与各协议数量。

**请求**：无参数、无请求体。

**响应 `data` 字段**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `pool_size` | int | 当前池容量 |
| `counts` | dict | 各协议数量 `{http, https, socks4, socks5}` |

**示例**：

```json
{
  "code": 0, "msg": "ok",
  "data": {
    "pool_size": 500,
    "counts": {"http": 300, "https": 100, "socks4": 50, "socks5": 50}
  }
}
```

### 2.3 GET /api/v1/ips

返回池内全部 IP 记录（按插入顺序）。

**请求**：无参数、无请求体。

**响应 `data`**：记录数组，每条字段如下（空池返回 `[]`）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | int | 全局自增 ID |
| `ip` | str | IP 地址 |
| `port` | int | 端口 |
| `protocol` | str | 协议枚举 |
| `proxy_url` | str | 代理地址 `<protocol>://<ip>:<port>` |
| `region` | str\|null | 地区 |
| `ttl` | float\|null | 生存时间 |
| `created_at` | float | 创建时间戳 |

**示例**：

```json
{
  "code": 0, "msg": "ok",
  "data": [
    {
      "id": 1, "ip": "1.2.3.4", "port": 8080,
      "protocol": "http", "proxy_url": "http://1.2.3.4:8080",
      "region": "CN", "ttl": 3600.0, "created_at": 1690000000.0
    }
  ]
}
```

### 2.4 GET /api/v1/ips/after/{id}

增量同步接口：返回 `id` 之后（`id > {id}`）的记录。响应顶层带 `max_id`（当前池内最大 id，池空为 `null`）。

**请求**：
- 路径参数 `id`（int，必填）：增量水位 ID。

**响应 `data`**：与 `/api/v1/ips` 相同的记录数组；另含顶层字段 `max_id`。

**增量为空时的二级池判定**：
- `id == max_id`：一级池暂无新 IP，**不做全量提取**（主池尚未有新进 IP 时不触发全量重拉）；
- `id > max_id`：一级池 id 空间已重置（重启/换代），触发全量重拉并重置水位线；
- `max_id` 缺失（旧版一级池）：回退全量重拉。

**示例**：

```
GET /api/v1/ips/after/100
```

```json
{
  "code": 0, "msg": "ok",
  "max_id": 102,
  "data": [
    { "id": 101, "ip": "5.6.7.8", "port": 3128, "protocol": "https",
      "proxy_url": "https://5.6.7.8:3128", "region": "US", "ttl": 7200.0,
      "created_at": 1690000600.0 }
  ]
}
```

---

## 3. 二级池 API（level2_pool, :8001+）

二级池共 8 个接口。每条记录字段结构如下（`/api/v1/ips` 及 `acquire` 返回）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | int | 本池本地记录 ID |
| `ip` | str | IP 地址 |
| `port` | int | 端口 |
| `protocol` | str | 协议枚举 |
| `proxy_url` | str | 代理地址 |
| `latency_ms` | float | 站点连通测试延迟（毫秒） |
| `leased` | bool | 是否已租赁 |
| `ttl` | float\|null | 生存时间 |
| `created_at` | float | 创建时间戳 |

### 3.1 GET /api/v1/status

服务运行统计与池统计。

**请求**：无参数、无请求体。

**响应 `data` 字段**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `uptime` | float | 进程启动至今秒数 |
| `total_pulled` | int | 从一级池拉取总数 |
| `total_entered` | int | 通过站点连通测试入池总数 |
| `api_call_count` | int | 累计 `/api/v1` 调用次数 |
| `last_synced_id` | int\|null | 增量同步水位 |
| `errors` | object | 错误计数 `{sync_failures, test_failures, revalidate_failures, ttl_sweep_failures, empty_acquires}` |
| `drops` | int | 因队满被丢弃的待测批次累计数 |
| `pool_stats` | object | 池统计，见下 |

`pool_stats` 字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `total` | int | 池内总数 |
| `by_proto` | dict | 各协议总数 `{http, https, socks4, socks5}` |
| `leased_total` | int | 已租赁总数 |
| `leased_by_proto` | dict | 各协议已租赁数 |
| `free_total` | int | 空闲总数 |
| `free_by_proto` | dict | 各协议空闲数 |

**示例**：

```json
{
  "code": 0, "msg": "ok",
  "data": {
    "uptime": 100.0, "total_pulled": 800, "total_entered": 600,
    "api_call_count": 10, "last_synced_id": 799,
    "errors": {"sync_failures": 1, "test_failures": 0, "revalidate_failures": 0,
               "ttl_sweep_failures": 0, "empty_acquires": 0},
    "drops": 0,
    "pool_stats": {
      "total": 600,
      "by_proto": {"http": 300, "https": 150, "socks4": 80, "socks5": 70},
      "leased_total": 200,
      "leased_by_proto": {"http": 100, "https": 50, "socks4": 30, "socks5": 20},
      "free_total": 400,
      "free_by_proto": {"http": 200, "https": 100, "socks4": 50, "socks5": 50}
    }
  }
}
```

### 3.2 GET /api/v1/count

仅返回池统计（同 `/api/v1/status` 中的 `pool_stats` 结构）。

**请求**：无参数、无请求体。

**示例**：

```json
{
  "code": 0, "msg": "ok",
  "data": {
    "total": 600,
    "by_proto": {"http": 300, "https": 150, "socks4": 80, "socks5": 70},
    "leased_total": 200,
    "leased_by_proto": {"http": 100, "https": 50, "socks4": 30, "socks5": 20},
    "free_total": 400,
    "free_by_proto": {"http": 200, "https": 100, "socks4": 50, "socks5": 50}
  }
}
```

### 3.3 GET /api/v1/ips

返回池内全部记录（按插入顺序，不改变租赁状态）。

**请求**：无参数、无请求体。

**响应 `data`**：记录数组（结构见本节开头），空池返回 `[]`。

**示例**：

```json
{
  "code": 0, "msg": "ok",
  "data": [
    { "id": 1, "ip": "1.2.3.4", "port": 8080, "protocol": "http",
      "proxy_url": "http://1.2.3.4:8080", "latency_ms": 120.5,
      "leased": false, "ttl": 3600.0, "created_at": 1690000000.0 }
  ]
}
```

### 3.4 POST /api/v1/ips/acquire

租赁一条代理：按提取策略选取一条空闲记录，标记 `leased=true` 并设置 `leased_at`。租赁操作在锁内原子执行。

**提取策略与筛选参数**（均为可选 query 参数；**全部不传 = 旧行为**，完全向后兼容）：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `strategy` | str | `latest` | 提取策略：`latest` 最新优先（默认）/ `random` 随机 / `latency_asc` 延迟从低到高 / `remaining_desc` 剩余时间从高到低 |
| `max_latency_ms` | float | 不筛选 | 延迟上限筛选：仅提取 `latency_ms <= max_latency_ms` 的记录 |
| `min_remaining_sec` | float | 不筛选 | 剩余时间下限筛选：仅提取剩余时间 `>= min_remaining_sec` 的记录 |

> **剩余时间** = `created_at + ttl - 当前时间`（不是 `ttl` 字段本身）；`ttl=null`（永不过期）视为剩余时间无穷大：`remaining_desc` 排序最优先、`min_remaining_sec` 筛选恒通过。
>
> **单条提取不排序**：`latency_asc` / `remaining_desc` 直接一次扫描取延迟最低 / 剩余时间最长者（并列取先入池者），不做全量排序。
>
> 非法参数（`strategy` 取值未知、筛选值非数字或为负）返回 `code: 40000`；筛选后无空闲候选与空池/全租赁同样返回 `code: 40402`。

**响应**：
- 成功：HTTP 200，`data` 为被租赁的记录，`leased: true`。
- 失败（空池/全部已租赁/筛选后无候选）：HTTP 200，`code: 40402`，`msg: "empty pool: no free ip available"`，`data: null`。

**示例**：

```
POST /api/v1/ips/acquire
POST /api/v1/ips/acquire?strategy=latency_asc&max_latency_ms=200
POST /api/v1/ips/acquire?strategy=remaining_desc&min_remaining_sec=60
```

```json
{
  "code": 0, "msg": "ok",
  "data": {
    "id": 10, "ip": "9.9.9.9", "port": 1080, "protocol": "socks5",
    "proxy_url": "socks5://9.9.9.9:1080", "latency_ms": 88.0,
    "leased": true, "ttl": 3600.0, "created_at": 1690001000.0
  }
}
```

**示例（失败）**：

```json
{ "code": 40402, "msg": "empty pool: no free ip available", "data": null }
```

### 3.5 POST /api/v1/ips/{id}/release

释放某条记录的租赁（`leased=false`，`leased_at=null`）。

**请求**：
- 路径参数 `id`（int，必填）：本池本地记录 ID。

**响应**：
- 成功：HTTP 200，`data: true`。
- 失败（记录不存在）：HTTP 200，`code: 40400`，`msg: "record not found: {id}"`，`data: null`。

**示例（成功）**：

```json
{ "code": 0, "msg": "ok", "data": true }
```

### 3.6 DELETE /api/v1/ips/{id}

从池中彻底删除一条记录（连同其租赁状态）。

**请求**：
- 路径参数 `id`（int，必填）：本池本地记录 ID。

**响应**：
- 成功：HTTP 200，`data: true`。
- 失败（记录不存在）：HTTP 200，`code: 40400`，`msg: "record not found: {id}"`，`data: null`。

**示例（成功）**：

```json
{ "code": 0, "msg": "ok", "data": true }
```

### 3.7 POST /api/v1/ips/release-all

释放所有当前已租赁的记录。

**请求**：无参数、无请求体。

**响应 `data`**：int，本次释放的租赁数量。

**示例**：

```json
{ "code": 0, "msg": "ok", "data": 3 }
```

### 3.8 POST /api/v1/ips/acquire-batch

批量租赁代理：单锁内原子执行，按提取策略与筛选参数（同 3.4，见上表）一次租赁至多 `count` 条空闲记录。

**请求**（query 参数）：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `count` | int | 必填 | 期望提取数量，须 `>= 1`；缺失/非整数/`< 1` 返回 `code: 40000` |
| `strategy` | str | `latest` | 同 3.4 |
| `max_latency_ms` | float | 不筛选 | 同 3.4 |
| `min_remaining_sec` | float | 不筛选 | 同 3.4 |

**响应**：
- 成功：HTTP 200，`code: 0`，`data` 为被租赁记录的**数组**，按选取顺序排列（`latest` 最新在前 / `latency_asc` 延迟升序 / `remaining_desc` 剩余时间降序 / `random` 随机）。空闲不足 `count` 时**尽量多给**（部分满足，仍为 `code: 0`）。
- 失败（一条都租不到）：HTTP 200，`code: 40402`，`data: null`。

**示例**：

```
POST /api/v1/ips/acquire-batch?count=5&strategy=latency_asc&max_latency_ms=300
```

```json
{
  "code": 0, "msg": "ok",
  "data": [
    { "id": 3, "ip": "3.3.3.3", "port": 3128, "protocol": "http",
      "proxy_url": "http://3.3.3.3:3128", "latency_ms": 90.0,
      "leased": true, "ttl": 3600.0, "created_at": 1690000900.0 },
    { "id": 10, "ip": "9.9.9.9", "port": 1080, "protocol": "socks5",
      "proxy_url": "socks5://9.9.9.9:1080", "latency_ms": 120.0,
      "leased": true, "ttl": 3600.0, "created_at": 1690001000.0 }
  ]
}
```

---

## 4. 代理层 API（proxy, :9000）

代理层共 12 个接口：9 个透传/健康检查 + 3 个账号管理（见 4.10）。除 `/api/v1/health` 外透传接口（`{site}` 为路由表中的站点标识）响应与上游二级池一致，见上文各对应接口。

**调用方鉴权**（仅约束 4.5 ~ 4.9 租还类接口；status/count/ips/health 等只读接口开放）：

- 带 `Authorization: Basic <user:pass>`：校验通过后**强制使用该账号绑定的池**，请求的 `{site}` 与绑定池不符回 **403**（`code=40300`）；用户名不存在或密码错误回 **401**（`code=40101`）；
- 不带凭据：只允许访问默认池（`proxy_routes.yaml` 的 `auth.default_site`，空 = 路由表第一个站点），访问其它池回 **403**（`code=40300`）并提示注册；
- 站点不在路由表：照旧回 **404**（`code=40400`），鉴权不改变该契约。

### 4.1 GET /api/v1/health

网关存活检查，返回当前路由表、代理层自身运行统计，并实时聚合一级池与各站点二级池的 `/status` 信息（`pools`）。

**请求**：无参数、无请求体。

**响应 `data` 字段**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | str | 固定 `"ok"` |
| `started_at` | str | 代理层启动时间（ISO 8601 UTC，如 `2026-08-29T12:00:00Z`） |
| `uptime` | float | 自启动以来的运行秒数 |
| `stats` | object | 代理层 API 被调用统计（见下） |
| `sites` | array | 站点列表，每项 `{name, base_url, target_url}` |
| `pools` | object | 实时聚合的池状态（见下） |

`stats` 字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `total_calls` | int | 代理层 API 被调用总次数（含 health 自身） |
| `calls_by_ip` | object | 按来源客户端 IP 的调用次数 `{ip: count}` |
| `calls_by_site` | object | 按站点透传转发次数 `{site: count}` |
| `errors` | object | 代理层自身错误次数 `{code: count}`，仅含代理层传输错误（`40400` 站点未配置 / `50200` 上游不可达）；**不统计**任何二级池业务错误码（如 `40402` 空池，由二级池自身 `/status` 的 `errors` 统计） |

`pools` 字段（health 时实时请求一级池与各站点二级池 `/status`）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `pools.level1` | object | `{base_url, status}`；`status` 为一级池 `/status` 的 `data`，不可达时为 `{error}` |
| `pools.sites` | array | 各站点 `{name, base_url, status}`；`status` 为该站点二级池 `/status` 的 `data`，不可达时为 `{error}` |

**示例**：

```json
{
  "code": 0, "msg": "ok",
  "data": {
    "status": "ok",
    "started_at": "2026-08-29T12:00:00Z",
    "uptime": 123.456,
    "stats": {
      "total_calls": 5,
      "calls_by_ip": { "127.0.0.1": 5 },
      "calls_by_site": { "site_a": 2, "site_b": 2 },
      "errors": { "40400": 1 }
    },
    "sites": [
      { "name": "site_a", "base_url": "http://127.0.0.1:8001", "target_url": null },
      { "name": "site_b", "base_url": "http://127.0.0.1:8002", "target_url": null }
    ],
    "pools": {
      "level1": {
        "base_url": "http://127.0.0.1:8000",
        "status": { "pool_size": 500, "total_pulled": 1000 }
      },
      "sites": [
        { "name": "site_a", "base_url": "http://127.0.0.1:8001",
          "status": { "total": 600, "free_total": 400 } },
        { "name": "site_b", "base_url": "http://127.0.0.1:8002",
          "status": { "error": "unreachable" } }
      ]
    }
  }
}
```

### 4.2 GET /api/v1/{site}/status

透传 → 上游二级池 `GET /api/v1/status`。查询参数原样转发。

### 4.3 GET /api/v1/{site}/count

透传 → 上游二级池 `GET /api/v1/count`。查询参数原样转发。

### 4.4 GET /api/v1/{site}/ips

透传 → 上游二级池 `GET /api/v1/ips`。查询参数原样转发。

### 4.5 POST /api/v1/{site}/ips/acquire

透传 → 上游二级池 `POST /api/v1/ips/acquire`。查询参数（`strategy` / `max_latency_ms` / `min_remaining_sec`）与请求体原始 JSON 原样转发（可为空）；成功返回被租赁记录，空池时返回 `code: 40402`。

### 4.6 POST /api/v1/{site}/ips/acquire-batch

透传 → 上游二级池 `POST /api/v1/ips/acquire-batch`。查询参数（`count` 及 3.4 的策略/筛选参数）原样转发；成功返回被租赁记录数组（部分满足仍为 `code: 0`），一条都租不到返回 `code: 40402`。

### 4.7 POST /api/v1/{site}/ips/{id}/release

透传 → 上游二级池 `POST /api/v1/ips/{id}/release`。

**路径参数**：
- `site`（str，必填）：站点标识。
- `id`（int，必填）：本池本地记录 ID（非 int 时返回 422 校验错误）。

请求体可选原始 JSON，原样转发。成功返回 `data: true`，记录不存在返回 `code: 40400`。

### 4.8 DELETE /api/v1/{site}/ips/{id}

透传 → 上游二级池 `DELETE /api/v1/ips/{id}`。

**路径参数**：
- `site`（str，必填）：站点标识。
- `id`（int，必填）：本池本地记录 ID。

成功返回 `data: true`，记录不存在返回 `code: 40400`。

### 4.9 POST /api/v1/{site}/ips/release-all

透传 → 上游二级池 `POST /api/v1/ips/release-all`。请求体可选原始 JSON，原样转发；成功返回释放数量 `data: <int>`。

### 4.10 账号管理与调用方鉴权（proxy, :9000）

给下游服务分配账号，凭据决定它从哪个二级池拿 IP（账号定向池）。账号存于 `proxy/data/accounts.db`（SQLite，密码加盐哈希，不入 git）；管理接口仅限内网使用，不设管理鉴权。

#### POST /api/v1/accounts — 注册账号

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `username` | str | 是 | 用户名（唯一） |
| `password` | str | 是 | 密码（服务端加盐哈希存储） |
| `assigned_site` | str | 是 | 绑定的二级池站点名（必须在路由表中，否则 40400） |

**响应 `data`**：`{username, assigned_site, created_at}`。重复注册回 400（`code=40000`）。

```bash
curl -X POST http://127.0.0.1:9000/api/v1/accounts \
     -H 'Content-Type: application/json' \
     -d '{"username":"sub2api","password":"s3cret","assigned_site":"zhihu"}'
```

#### GET /api/v1/accounts — 账号列表

**响应 `data`**：`{accounts: [{username, assigned_site, created_at}], total}`（不含任何密码材料）。

#### DELETE /api/v1/accounts/{username} — 删除账号

**响应 `data`**：`{username, deleted: true}`；不存在回 404（`code=40400`）。删除后该账号凭据立即失效（401）。

#### 调用示例：带凭据租 IP（强制走 zhihu 池）

```bash
curl -u sub2api:s3cret -X POST http://127.0.0.1:9000/api/v1/zhihu/ips/acquire
```

### 4.11 隧道代理入口（tunnel, :9001）

标准正向代理入口（`proxy_routes.yaml` 的 `tunnel` 段，`enabled: true` 开启，默认关闭）：下游把 `http://user:pass@代理层主机:9001` 填进代理配置即可整池使用。端口独立于 9000，只讲代理协议。

**行为**：

- HTTP 绝对 URI 请求经池内出口 IP 转发；HTTPS 走 CONNECT 隧道（建联后双向透传，支持流式）；
- 凭据：`Proxy-Authorization: Basic user:pass`，查 `accounts` 表定池（与 4.10 同一套账号）；无凭据走默认池（`auth.default_site`）；凭据缺失/格式非法/密码错误回 **407**（响应带 `Proxy-Authenticate: Basic`）；
- 每个请求（CONNECT 为每条连接）从绑定池 acquire 一个出口 IP，失败自动换下一个（最多 `max_attempts` 次），用完 release 归还；
- 池空按 `acquire_interval` 轮询等待，超过 `acquire_max_wait` 秒回 **502**（`{"error":"pool empty"}`）；重试耗尽回 502（`{"error":"all upstream proxies failed"}`）。

**配置**：

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | false | 是否开启隧道入口 |
| `host` | 随 service.host | 监听地址 |
| `port` | 9001 | 代理入口端口 |
| `max_attempts` | 4 | 单请求最多换几个出口 IP |
| `connect_timeout` | 10.0 | 连上游出口 IP 超时秒数 |
| `upstream_timeout` | 15.0 | 上游 CONNECT 响应/二级池接口超时秒数 |
| `acquire_max_wait` | 30.0 | 池空最长等待秒数 |
| `acquire_interval` | 2.0 | 池空轮询间隔秒数 |

```bash
curl -x http://u1:pw@127.0.0.1:9001 http://www.baidu.com -i
curl -x http://u1:pw@127.0.0.1:9001 https://www.zhihu.com -I
```

---

## 5. 接口速查表

### 一级池（level1_pool, :8000）

| 方法 | 路径 | 参数 | 请求体 | data |
|---|---|---|---|---|
| GET | `/api/v1/status` | — | — | 服务+池统计 |
| GET | `/api/v1/count` | — | — | 池统计 |
| GET | `/api/v1/ips` | — | — | 记录数组 |
| GET | `/api/v1/ips/after/{id}` | `id`(int) | — | 记录数组（顶层含 `max_id`） |

### 二级池（level2_pool, :8001+）

| 方法 | 路径 | 参数 | 请求体 | data | 错误 |
|---|---|---|---|---|---|
| GET | `/api/v1/status` | — | — | 服务+池统计 | — |
| GET | `/api/v1/count` | — | — | 池统计 | — |
| GET | `/api/v1/ips` | — | — | 记录数组 | — |
| POST | `/api/v1/ips/acquire` | `strategy`/`max_latency_ms`/`min_remaining_sec`(可选) | — | 记录(leased=true) | 40000, 40402 |
| POST | `/api/v1/ips/acquire-batch` | `count`(必填)+同 acquire 可选参数 | — | 记录数组(leased=true) | 40000, 40402 |
| POST | `/api/v1/ips/{id}/release` | `id`(int) | — | `true` | 40400 |
| DELETE | `/api/v1/ips/{id}` | `id`(int) | — | `true` | 40400 |
| POST | `/api/v1/ips/release-all` | — | — | 释放数量(int) | — |

### 代理层（proxy, :9000）

| 方法 | 路径 | 参数 | 请求体 | data |
|---|---|---|---|---|
| GET | `/api/v1/health` | — | — | 路由表+代理层统计 |
| GET | `/api/v1/{site}/status` | `site` | — | 上游透传 |
| GET | `/api/v1/{site}/count` | `site` | — | 上游透传 |
| GET | `/api/v1/{site}/ips` | `site` | — | 上游透传 |
| POST | `/api/v1/{site}/ips/acquire` | `site` | 原始 JSON | 上游透传 |
| POST | `/api/v1/{site}/ips/acquire-batch` | `site` | 原始 JSON | 上游透传 |
| POST | `/api/v1/{site}/ips/{id}/release` | `site`,`id`(int) | 原始 JSON | 上游透传 |
| DELETE | `/api/v1/{site}/ips/{id}` | `site`,`id`(int) | — | 上游透传 |
| POST | `/api/v1/{site}/ips/release-all` | `site` | 原始 JSON | 上游透传 |
