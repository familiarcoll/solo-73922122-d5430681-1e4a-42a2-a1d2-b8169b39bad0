# 逐日温度网格 → 站点时序提取服务

把逐日温度的规则经纬网格（NetCDF）按调查站点提取为站点级时序 CSV，供暴露分析使用。

技术栈：**FastAPI + xarray + netCDF4**，以 Docker Compose 一键启动，Swagger 文档在 `/docs`。

## 功能与约束

| 维度 | 约定 |
| --- | --- |
| 上传大小 | NetCDF ≤ **32 MiB**（流式落盘，超限立即拒绝并清理暂存） |
| 站点数量 | ≤ **100** 个，每个含 ID、纬度、经度，ID 不可重复 |
| 数据结构 | 仅接受变量维度严格为 `(time, lat, lon)` 的三维**规则经纬网**（坐标严格单调、等间距） |
| 日历 | 仅接受标准公历（CF `standard` / `gregorian` / `proleptic_gregorian`） |
| 最近格点 | 按**球面 haversine 距离**选最近格点；兼容**降序纬度**与 **0–360 / -180–180 两种经度** |
| 缺测 | 网格缺测（NaN）在 CSV 中以**空字段**输出 |
| 并行度 | 最多 **2 个任务并行**，满额直接 **409 拒绝**（不排队） |
| 计算时限 | 自任务开始计算起 **60 秒**，超时判 `timeout` |
| 取消 | 任意时刻可取消；受理后绝不发布迟到结果 |
| 清理 | 取消 / 失败 / 超时均等实际计算停止、文件关闭后才清理暂存，且不影响其他任务 |
| 下载 | 仅 **completed** 任务可下载完整 CSV，其余状态返回 409 |
| 重启恢复 | 重启后未完成任务记为 `failed`；已完成结果仍可下载 |

## 目录结构

```
.
├── app/
│   ├── main.py            # FastAPI 路由：上传/查询/取消/下载
│   ├── task_manager.py    # 并发槽位、状态机、取消/超时、持久化与重启恢复
│   ├── extraction.py      # 球面最近格点、分块提取、原子发布 CSV
│   ├── validation.py      # 网格/公历/站点输入校验
│   ├── netcdf_guard.py    # HDF5/netCDF4 访问串行锁（线程安全）
│   ├── config.py          # 环境变量配置
│   └── states.py          # 任务状态常量
├── samples/
│   ├── generate_samples.py  # 生成虚构网格、站点与慢提取样例
│   ├── demo_api.sh          # 端到端操作演示脚本
│   └── data/                # 生成的样例文件（运行脚本后出现）
├── tests/                 # pytest 测试（29 个，含并发/取消/超时/重启恢复）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 快速开始（Docker Compose）

```bash
# 1. 生成虚构样例数据（需要本地 Python 环境，或直接使用已附带的 samples/data）
python samples/generate_samples.py

# 2. 构建并启动
docker compose up --build

# 3. 打开交互式文档
#    http://localhost:8000/docs
```

健康检查：`GET http://localhost:8000/health`

### 不使用 Docker（本地运行）

```bash
pip install -r requirements.txt
python samples/generate_samples.py
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/tasks` | 上传 NetCDF + 站点，创建提取任务（multipart 表单） |
| GET | `/api/tasks` | 列出全部任务 |
| GET | `/api/tasks/{id}` | 查询单个任务状态与进度 |
| POST | `/api/tasks/{id}/cancel` | 请求取消 |
| GET | `/api/tasks/{id}/download` | 下载结果 CSV（仅 completed） |
| GET | `/docs` | Swagger UI，可直接在页面操作全部接口 |

### 创建任务的表单字段

- `file`：NetCDF 文件
- `variable`：温度变量名，如 `tas`、`t2m`、`temperature`
- `stations_file`：站点 CSV（表头 `id,lat,lon`，亦支持 `station_id/latitude/longitude/name` 等别名），**或**
- `stations_json`：站点 JSON 字符串，如
  `[{"id":"ST01","lat":39.9,"lon":116.4}]`（也接受 `{"stations":[...]}` 包装）

两种站点形式二选一。

### 任务状态机

```
pending → running ─┬─→ completed
                   ├─→ failed
                   ├─→ timeout
 running/cancelling ─→ cancelled
```

### 输出 CSV 格式

```csv
station_id,date,grid_lat,grid_lon,temperature
ST01,2026-01-01,40.0,120.0,33.622032165527344
HOLE,2026-01-01,0.0,180.0,
```

- 每个站点 × 每个日期一行；`date` 为 `YYYY-MM-DD`
- `grid_lat/grid_lon` 为命中的**网格原始坐标**（保留 0–360 或降序纬度等原始写法）
- `temperature` 缺测时为空

## 命令行演示

```bash
bash samples/demo_api.sh
```

脚本依次演示：健康检查 → 上传建任务 → 轮询状态 → 下载 CSV → 慢任务取消（含取消后下载返回 409）。

## 虚构样例说明

`python samples/generate_samples.py` 在 `samples/data/` 生成：

| 文件 | 用途 |
| --- | --- |
| `demo_grid.nc` | 升序纬度 + 0–360 经度，含缺测空洞，变量 `tas` |
| `demo_grid_desc.nc` | **降序纬度** + -180–180 经度，变量 `temperature` |
| `demo_slow_cancel.nc` | 可控慢提取：每 5 天休眠 2 秒（约 12 秒），用于演示**取消** |
| `demo_slow_timeout.nc` | 可控慢提取：每 2 天休眠 5 秒（约 75 秒），用于演示 **60 秒超时** |
| `stations.csv` / `stations.json` | 10 个虚构站点，覆盖两种经度约定 |

慢提取由 NetCDF 全局属性驱动（仅本服务识别，真实文件会被忽略）：

```
demo_slow = 1
demo_delay_seconds = 2.0
demo_chunk_steps = 5
```

## 关键设计说明

- **原子发布**：结果先写 `{id}.csv.tmp.{pid}`，完整写入并关闭后 `os.replace` 原子改名；任何中断都不会留下可下载的半成品。
- **取消语义**：`cancel` 仅设置事件并把状态置为 `cancelling`；工作线程在每个时间块边界（含可打断的休眠，50ms 粒度）响应，实际停止并关闭数据集后才落 `cancelled` 终态并清理，因此不存在迟到结果。
- **超时语义**：截止时间为 `开始计算时刻 + 60s` 的单调时钟，与取消共用同一检查点，超时同样不发布结果。
- **线程安全**：manylinux 版 netCDF4/HDF5 wheel 非线程安全，并发进入会段错误；`netcdf_guard.netcdf_lock` 将所有 HDF5 打开/读取/关闭串行化，而 CSV 拼装与慢提取休眠在锁外，两个任务仍可并行调度。
- **重启恢复**：任务元数据持久化于 `data/registry.json`（原子写）。启动时未处于终态的任务一律标记 `failed`（原因“服务重启，任务未完成”）；completed 任务仅在 CSV 文件存在时保持可下载，否则降级为 failed。

## 运行测试

```bash
pip install -r requirements-dev.txt
pytest
```

测试覆盖：网格/公历/站点校验、球面最近格点（降序纬度、经度约定）、缺测留空、取消、超时、并发上限与槽位释放、下载门禁、重启恢复，共 29 项。

## 配置（环境变量）

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `APP_DATA_DIR` | `data` | 上传、结果与注册表根目录 |
| `APP_MAX_UPLOAD_BYTES` | `33554432` | 上传上限（32 MiB） |
| `APP_MAX_STATIONS` | `100` | 站点数上限 |
| `APP_MAX_PARALLEL_TASKS` | `2` | 并行任务上限 |
| `APP_TASK_TIMEOUT_SECONDS` | `60` | 单任务计算时限 |
| `APP_CHUNK_SIZE` | `10` | 每次读取的时间步分块大小 |
