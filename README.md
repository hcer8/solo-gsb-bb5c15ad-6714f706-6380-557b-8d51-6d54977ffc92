# 胶片档案扫描整理 API

面向胶片档案扫描整理的本地 API：按批提交胶卷编号、预期帧号起止范围与扫描帧号列表，
服务自动完成**跨批次帧号合并、重复帧识别（只计一次并单独列出）、预期范围内连续缺帧区间计算**。
批次导入**幂等**——重复导入相同批次不会改变任何结果。

技术栈：Python + FastAPI + SQLite（单文件库，零外部依赖）。

## 一条命令启动

```bash
docker compose up -d --build
```

启动后访问：

| 用途 | 地址 |
| --- | --- |
| API 服务（端口 **8000**） | http://localhost:8000 |
| 健康检查 | http://localhost:8000/health |
| Swagger 交互文档 | http://localhost:8000/docs |
| OpenAPI 描述 | http://localhost:8000/openapi.json |

停止 / 查看日志：

```bash
docker compose down        # 停止（数据保留在 volume 中）
docker compose logs -f     # 查看日志
```

## 配置（环境变量）

均为可选项，默认值开箱即用：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | `8000` | 服务监听端口（改端口时 compose 的 `ports` 也要同步调整） |
| `HOST` | `0.0.0.0` | 监听地址 |
| `DB_PATH` | `/data/film.db` | SQLite 数据库文件路径，启动时自动建库建表 |
| `DB_TIMEOUT` | `30000` | SQLite 写锁等待毫秒数 |

数据持久化在 compose 命名卷 `film-data`（容器内 `/data`）。
改用宿主机目录持久化可把 compose 中的 `film-data:/data` 换成 `./data:/data`。

## API 一览

### 1. 提交扫描批次（幂等）

```bash
curl -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{
    "roll_no": "R-001",
    "expected_start": 1,
    "expected_end": 12,
    "frames": [1, 2, 2, 3, 5, 8, 9, 10, 12],
    "batch_id": "b-2026-001"
  }'
```

字段说明：

- `roll_no`：胶卷编号（必填）
- `expected_start` / `expected_end`：本批预期帧号起止（闭区间，整数 ≥ 0，end ≥ start）
- `frames`：本批扫描帧号列表（至少 1 个；批内重复会被识别为重复帧）
- `batch_id`：可选；提供后可用它查询批次。留空则服务端自动生成

响应（首次导入 `201 Created`）：

```json
{
  "batch_id": "b-2026-001",
  "roll_no": "R-001",
  "repeated": false,
  "added_frames": [1, 2, 3, 5, 8, 9, 10, 12],
  "duplicates": [2],
  "merged_frames": [1, 2, 3, 5, 8, 9, 10, 12],
  "expected_ranges": [[1, 12]],
  "missing_ranges": [[4, 4], [6, 7], [11, 11]],
  "out_of_range_frames": [],
  "created_at": "2026-09-23T06:00:00.000Z"
}
```

**幂等行为**：同一胶卷下，用相同的预期范围 + 相同帧号集合再次提交（无论是否带相同
`batch_id`），返回 `200 OK` 且 `"repeated": true`，不写入任何数据，胶卷汇总结果不变。

跨批次合并示例（继续上面的胶卷提交第二批 `[11, 12, 12, 13]`，预期范围 `10-14`）：

- `added_frames = [11, 13]`（12 已由上一批扫到）
- `duplicates = [12]`（12 跨批次重复；批内重复的 12 同样只计一次）
- `merged_frames` 为该胶卷全部帧号（去重排序）
- 预期范围自动合并为 `[[1,14]]`，`missing_ranges` 随之基于合并范围重新计算
- 帧 15 这种落在该胶卷**所有**预期范围之外的扫描帧，不计入缺帧，列入 `out_of_range_frames`

### 2. 查询批次

```bash
curl http://localhost:8000/batches/b-2026-001
```

### 3. 查询胶卷完整状态

```bash
curl http://localhost:8000/rolls/R-001
```

返回合并后的预期范围、全部帧号、连续缺帧区间、范围外帧，以及每个批次的明细
（各批次的原始帧、新增帧、重复帧）。

### 4. 列出全部胶卷汇总

```bash
curl http://localhost:8000/rolls
```

### 5. 健康检查

```bash
curl http://localhost:8000/health
```

## 业务规则

1. **跨批次合并**：同一 `roll_no` 的所有批次共享帧号集合与预期范围集合。
2. **重复帧**：同一批次内重复出现、或此前批次已扫到的帧号，只计一次，在
   `duplicates` 中单独列出（去重排序）。
3. **连续缺帧区间**：该胶卷所有批次的预期范围先做区间合并（重叠/相邻即合并），
   再扣除已扫帧，输出若干连续闭区间，如 `[4,4]`、`[6,7]`。
4. **幂等导入**：以 `(胶卷编号, 预期范围, 帧号集合)` 的 SHA-256 指纹判重，
   重复导入不改变结果。显式 `batch_id` 若被**不同内容**占用返回 `409`。
5. 帧号为非负整数；预期范围为闭区间。

## 本地非 Docker 运行（开发用）

```bash
pip install -r requirements.txt
DB_PATH=./data/film.db uvicorn app.main:app --reload --port 8000
```
