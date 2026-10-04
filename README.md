# 事故复盘时间线（Retro Timeline）

供小组在本地协作的事故复盘时间线工具：记录事件的起始时间范围、持续分钟数与“先于”关系，
自动推算每个事件的可行时间窗并给出推算依据链。Python 标准库服务端 + SQLite 持久化 +
浏览器单页界面，无任何第三方依赖。

## 功能

- **事件建模**：每个事件包含起始时间范围（最早/最晚开始）、持续分钟数与备注。
- **“先于”约束（含间隔设置）**：A 先于 B 表示 A 结束后 B 才能开始。每条关系可设置
  **最短等待**（非负整数分钟，缺省 0）与**最长等待**（非负整数分钟，可空 = 不限），
  即 `A 结束 + 最短等待 ≤ B 开始 ≤ A 结束 + 最长等待`。未设置间隔的旧关系仍按旧语义
  `A 开始 + A 持续 ≤ B 开始` 推算，结果与升级前一致。最长等待不得短于最短等待，
  非法输入（负数、非整数、最长 < 最短）会被拒绝（HTTP 400）且不改任何记录。
- **时间窗推算**：前向/后向约束传播，同时施加每条关系的间隔上下界，给出每个事件的
  最早/最晚可行开始（即可行时间窗），并展示从输入到结论的完整依据链（含间隔取值）。
- **输入与结果分离保存**：原始输入存于 `events` / `relations` 表，每次保存的推算结果
  单独写入 `computations` 表（按复盘版本留存），互不覆盖。
- **版本回看与恢复**：每次保存/定稿成功时，除推算结果外另写一条**不可变输入快照**
  （标题、事件、“先于”关系及各自的间隔设置）到 `snapshots` 表，与当次推算一一对应。
  网页和 HTTP API 可列出各版本、回看当时输入与推算依据；把指定版本恢复时会
  **另存为当前复盘的新草稿版本**并重新推算，不覆盖任何旧版本。恢复须携带当前
  `base_version`，版本过期沿用 409 冲突语义。冲突或暂存输入允许恢复，但仍不得定稿。
- **旧库历史版本**：升级前只有推算结果、缺少完整输入快照的历史版本，会在版本列表中
  明确标记为「不可回看、不可恢复」，不会用当前输入冒充；老接口行为保持不变。
- **冲突处理**：约束冲突时保留全部输入，指出冲突事件及双方依据链（正推最早可行开始
  与反推最晚可行开始两条相反依据链），该版本**不得定稿**。冲突按成因归因：
  - `invalid_range`：输入范围自相矛盾（最早开始晚于最晚开始）；
  - `cycle`：“先于”关系构成**真正不可满足的有向环**（沿关系方向兜圈）；
  - `window`：时间窗冲突，含**多路径上下界矛盾**——关系图本身无有向环，但最短等待
    沿一条路径推出的最早界与最长等待沿另一路径推出的最晚界无法同时满足。此时按
    受影响事件给出由输入和各关系推出的最早、最晚界及可读依据链，不再误报为关系成环。
- **暂存事件**：缺少时间范围（或持续分钟数）的事件可暂存，其关联的“先于”关系
  暂不参与推算，并在页面上标明原因；存在暂存事件时同样不得定稿。
- **多人协作并发控制**：基于 `reviews.version` 的乐观锁。携带旧版本号的提交会被
  拒绝（HTTP 409）并提示刷新，不会覆盖他人较新的记录。
- **定稿**：推算通过且无暂存事件时可定稿；定稿后再次修改会自动回到草稿状态。

## 容器启动

```bash
# 方式一：Docker Compose（推荐）
docker compose up --build
# 或旧版：docker-compose up --build

# 方式二：纯 Docker
docker build -t retro-timeline .
docker run -d -p 8000:8000 -v retro-timeline-data:/data --name retro-timeline retro-timeline
```

容器启动时自动初始化数据库（创建目录与表结构），无需手动迁移。

## 网页访问

启动后浏览器访问：**http://localhost:8000**（默认端口，见下方 `PORT` 配置）。

## 可配置项

| 环境变量 | 默认值（容器内） | 说明 |
|---|---|---|
| `PORT` | `8000` | 服务监听端口。Compose 中同时用于宿主机映射：`PORT=9000 docker compose up` 后访问 `http://localhost:9000` |
| `HOST` | `0.0.0.0` | 监听地址 |
| `DB_PATH` | `/data/timeline.db` | SQLite 数据库文件路径。容器内 `/data` 为挂载卷，删除容器数据不丢失 |

Compose 示例（`docker-compose.yml`）中已包含命名卷 `timeline-data` 用于持久化；
如改用宿主机目录，可将卷替换为 `./data:/data`。

## 本地开发（不用 Docker）

```bash
python3 app/server.py          # 默认监听 8000，数据库写入 ./data/timeline.db
PORT=9000 DB_PATH=/tmp/t.db python3 app/server.py
python3 tests/test_compute.py  # 推算引擎单元测试（含间隔上下界）
python3 tests/test_versions.py # 版本快照/回看/恢复、旧库兼容单元测试
python3 tests/test_api.py      # HTTP API 端到端测试（间隔校验、409、恢复保留间隔）
```

## HTTP API 简介

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/reviews` | 复盘列表 |
| POST | `/api/reviews` | 新建复盘 `{title}` |
| GET | `/api/reviews/{id}` | 复盘快照：元信息 + 原始输入 + 最近一次推算结果 |
| PUT | `/api/reviews/{id}` | 整体保存并重新推算 `{base_version, title, events, relations}`；关系可带 `min_gap_minutes` / `max_gap_minutes`；版本过期返回 409，间隔非法返回 400 |
| POST | `/api/reviews/{id}/finalize` | 定稿 `{base_version}`；有冲突或暂存事件返回 400 |
| GET | `/api/reviews/{id}/versions` | 版本列表（含回看/恢复标记；旧库无快照版本 `reviewable=false`） |
| GET | `/api/reviews/{id}/versions/{v}` | 版本回看：当时标题/事件/关系输入 + 推算依据与状态；旧库无快照返回 422 |
| POST | `/api/reviews/{id}/versions/{v}/restore` | 恢复为新草稿版本 `{base_version}`；过期返回 409，旧库无快照返回 422 |
| DELETE | `/api/reviews/{id}` | 删除复盘 |

### 版本回看与恢复（HTTP 示例）

```bash
# 1) 列出可回看的版本
curl -s http://localhost:8000/api/reviews/1/versions
# {"current_version": 4, "versions": [
#   {"version": 4, "kind": "finalize", "state": "finalized",
#    "reviewable": true, "restorable": true, "status": "ok", ...},
#   {"version": 3, "kind": "save", "state": "draft", "reviewable": true, ...},
#   {"version": 2, "kind": null, "reviewable": false, "restorable": false,
#    "reason": "历史版本缺少完整输入快照，不可回看、不可恢复", ...} ]}

# 2) 回看某版本：返回不可变输入（title/events/relations）与当时推算结果
curl -s http://localhost:8000/api/reviews/1/versions/3
# {"version": 3, "state": "draft", "kind": "save", "title": "...",
#  "input": {"events": [...], "relations": [...]},
#  "result": {"status": "ok", "windows": {...}, "conflicts": [], "drafts": []}}

# 3) 把 v3 恢复为当前复盘的新草稿版本（必须带当前 base_version；旧版本不被覆盖）
curl -s -X POST http://localhost:8000/api/reviews/1/versions/3/restore \
  -H 'Content-Type: application/json' -d '{"base_version": 4}'
# 成功返回与 GET /api/reviews/1 相同的新快照（version=5, status="draft"）。
# base_version 过期 → 409 {"error": "...", "current_version": 5}
# 恢复冲突/暂存输入：允许成功，但该新草稿仍不可定稿（finalize 返回 400）。
# 恢复旧库无快照版本 → 422 {"error": "历史版本缺少完整输入快照，不可回看、不可恢复"}
```

页面下方「历史版本」面板提供同样的操作：每行可**回看**（弹窗展示当时输入与推算依据
链，可直接**恢复为新草稿版本**）；旧库无快照版本只显示不可回看、不可恢复的原因。
定稿产生的新版本会标注「已定稿」并沿用同一输入；由恢复产生的版本标注「恢复产生」并
记录来源版本。

### “先于”关系的间隔设置（HTTP 示例）

每条关系可携带 `min_gap_minutes`（最短等待，非负整数分钟，缺省 0）与
`max_gap_minutes`（最长等待，非负整数分钟，可空/缺省 = 不限）：

```bash
# 1) 保存：告警结束后 10~60 分钟内值班必须响应
curl -s -X PUT http://localhost:8000/api/reviews/1 \
  -H 'Content-Type: application/json' -d '{
    "base_version": 1, "title": "支付故障复盘",
    "events": [
      {"key": "a", "name": "告警触发",
       "earliest_start": "2026-10-01T10:00", "latest_start": "2026-10-01T10:30",
       "duration_minutes": 15},
      {"key": "b", "name": "值班响应",
       "earliest_start": "2026-10-01T10:00", "latest_start": "2026-10-01T12:00",
       "duration_minutes": 30}
    ],
    "relations": [
      {"predecessor_key": "a", "successor_key": "b",
       "min_gap_minutes": 10, "max_gap_minutes": 60}
    ]}'
# 推算同时施加间隔上下界：
#   值班响应可行窗口 10:25 ~ 11:45
#   依据链示例：「告警触发」先于「值班响应」且「告警触发」持续 15 分钟、
#   最短等待 10 分钟 ⇒ 「值班响应」最早开始不早于 2026-10-01T10:25

# 2) 非法间隔被拒绝（HTTP 400），记录保持不变
curl -s -X PUT http://localhost:8000/api/reviews/1 \
  -H 'Content-Type: application/json' -d '{
    "base_version": 2, "title": "支付故障复盘", "events": [...],
    "relations": [{"predecessor_key": "a", "successor_key": "b",
                   "min_gap_minutes": 60, "max_gap_minutes": 10}]}'
# 400 {"error": "关系「告警触发」先于「值班响应」的最长等待不得短于最短等待"}

# 3) 版本回看/恢复保留每条关系当时的间隔设置
curl -s http://localhost:8000/api/reviews/1/versions/2
#  "input": {"relations": [{"predecessor_key": "id_1", "successor_key": "id_2",
#                           "min_gap_minutes": 10, "max_gap_minutes": 60}], ...}
curl -s -X POST http://localhost:8000/api/reviews/1/versions/2/restore \
  -H 'Content-Type: application/json' -d '{"base_version": 3}'
# 新草稿版本的关系同样带 min_gap_minutes=10, max_gap_minutes=60；旧版本不被覆盖。
```

不设置间隔的关系（包括升级前保存的旧关系）按 `min_gap_minutes=0`、
`max_gap_minutes=null` 处理，推算结果与旧版完全一致。间隔约束与输入范围冲突时
（例如最长等待小于前项结束到后项最早开始的距离），推算状态为 `conflict`，
冲突明细中给出冲突事件与正/反两条依据链，输入保留但不得定稿。

### 多路径上下界矛盾的冲突归因（本轮变动）

关系图无有向环、但多条路径的间隔上下界相互矛盾时，推算**不再误报**
“先于关系构成循环”（`cycle`），而是按时间窗冲突（`window`）指出受影响事件，
展示由输入和各关系推出的最早、最晚界及可读依据链。真正有向环仍按 `cycle` 报告。

```bash
# 复盘：四个持续 0 分钟事件，A 固定 10:00，其余起始范围 10:00~12:00；
# A→B 最短等待 60 分钟、B→D 无额外等待、A→C 与 C→D 最长等待各 10 分钟。
curl -s -X PUT http://localhost:8000/api/reviews/1 \
  -H 'Content-Type: application/json' -d '{
    "base_version": 1, "title": "发布故障复盘",
    "events": [
      {"key": "a", "name": "A", "earliest_start": "2026-10-01T10:00",
       "latest_start": "2026-10-01T10:00", "duration_minutes": 0},
      {"key": "b", "name": "B", "earliest_start": "2026-10-01T10:00",
       "latest_start": "2026-10-01T12:00", "duration_minutes": 0},
      {"key": "c", "name": "C", "earliest_start": "2026-10-01T10:00",
       "latest_start": "2026-10-01T12:00", "duration_minutes": 0},
      {"key": "d", "name": "D", "earliest_start": "2026-10-01T10:00",
       "latest_start": "2026-10-01T12:00", "duration_minutes": 0}
    ],
    "relations": [
      {"predecessor_key": "a", "successor_key": "b", "min_gap_minutes": 60},
      {"predecessor_key": "b", "successor_key": "d"},
      {"predecessor_key": "a", "successor_key": "c", "max_gap_minutes": 10},
      {"predecessor_key": "c", "successor_key": "d", "max_gap_minutes": 10}
    ]}'
# 响应（摘录）：status="conflict"，conflicts 仅含 window 类型（无 cycle）：
#   {"type": "window", "event_ids": [<D 的 id>],
#    "message": "事件「D」推算后最早可行开始 2026-10-01T11:00 晚于最晚可行开始
#                2026-10-01T10:20，约束冲突",
#    "earliest_chain": [「A」输入的最早开始为 10:00 →
#      「A」先于「B」…最短等待 60 分钟 ⇒「B」最早开始不早于 11:00 →
#      「B」先于「D」… ⇒「D」最早开始不早于 11:00],
#    "latest_chain":  [「A」输入的最晚开始为 10:00 →
#      「A」先于「C」且最长等待 10 分钟 ⇒「C」最晚开始不晚于 10:10 →
#      「C」先于「D」且最长等待 10 分钟 ⇒「D」最晚开始不晚于 10:20]}
# windows 中 D 的窗口为 11:00 ~ 10:20（被击穿）；输入保留，finalize 返回 400。
# 网页与版本回看（GET /api/reviews/1/versions/2）展示同一结论。
```

推算仍保留 `status`、`conflicts`、`windows`、依据链字段与 `base_version` 过期返回
409 的约定；保存冲突输入、禁止冲突版本定稿、暂存事件跳过推算的行为不变；
未设间隔的旧关系照旧推算，已有历史版本的回看内容不被改写。

## 目录结构

```
app/
  server.py          HTTP 服务（标准库 http.server）、间隔设置校验
  db.py              SQLite 持久化、乐观锁、启动时建表与老库迁移
  compute.py         时间窗推算与依据链生成（含间隔上下界传播）
  static/            浏览器单页界面（原生 HTML/JS/CSS）
tests/test_compute.py  推算引擎单元测试
tests/test_versions.py 版本快照/回看/恢复与旧库兼容测试
tests/test_api.py      HTTP API 端到端测试
Dockerfile / docker-compose.yml / .dockerignore
```
