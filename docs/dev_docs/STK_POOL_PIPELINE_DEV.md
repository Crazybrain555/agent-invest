# 股票池更新 Pipeline 设计与开发文档（`run_stk_pool_pipline`）

> 目标：在 `task_piplines/train_data_update/` 新增一个 `run_stk_pool_pipline.py`，用于定期更新“股票池”数据。  
> 股票池至少两类：**常见指数成份股**（先做） + **自定义股票池**（后续做）。  
> 本文档先把整体设计讲清楚，便于你审核后再进入代码开发。

---

## 0. 背景与范围

你希望把“股票池”作为一个与训练数据更新链路一致的工程化 pipeline：

- 入口脚本：`task_piplines/train_data_update/run_stk_pool_pipline.py`
- 任务实现：在 `src/tasks/` 下拆成多个 task（指数池 / 自定义池）
- 产出表：先新增一张“指数股票池表”（第一阶段），字段与 `ai_is.forbid_pool_comprehensive` 类似，但增加 `pool_code`；后续自定义股票池另建新表（避免单表过大）

**本次第一阶段只实现：指数池 task（来自 Wind：`wind_quant.dbo.AIndexMembers`）。**

---

## 1. 第一阶段产出：指数股票池表（建议方案）

### 1.1 表名建议

你希望按“股票池类型”拆分成多张表，避免未来数据量过大。本阶段只建设“指数股票池表”：

- 表名（建议）：`ai_is.stk_pool_of_index`

说明：
- 该表只用于“常见指数成份股”与未来可能扩展的“更多指数 pool_code”。
- 后续自定义股票池（模型/第三方/规则）将建设独立表（你后面再单独定义表结构与写入规则）。

### 1.2 字段与主键

字段（与现有工程一致，成份股字段使用 `stock_code`）：

| 字段 | 类型 | 含义 | 备注 |
|---|---|---|---|
| `trade_date` | DATE | 交易日 | YYYYMMDD 对齐 |
| `pool_code` | VARCHAR(40) | 池标识 | 指数 Wind 代码，例如 `000300.SH`、`000938.SH` |
| `stock_code` | VARCHAR(15) | 股票代码 | 推荐存 **6位数字**（去掉 `.SZ/.SH/.BJ/...`） |
| `signal` | SMALLINT | 是否属于该池 | 默认 1 |
| `insert_time` | TIMESTAMP | 写入时间 | `datetime.utcnow()` |

主键（推荐）：
- `PRIMARY KEY (trade_date, pool_code, stock_code)`

索引：
- `(pool_code, trade_date)`：常见查询是“某个 pool 在某日的成份股”


---

## 2. 数据源：Wind `AIndexMembers`

### 2.1 Wind 表字段对照（本 pipeline 用到的部分）

表：`wind_quant.dbo.AIndexMembers`

| Wind 字段 | 含义 | 映射到产出 |
|---|---|---|
| `S_INFO_WINDCODE` | 指数 Wind 代码 | `pool_code` |
| `S_CON_WINDCODE` | 成份股 Wind 代码 | `stock_code`（建议去后缀） |
| `S_CON_INDATE` | 纳入日期（YYYYMMDD） | 用于区间判断 |
| `S_CON_OUTDATE` | 剔除日期（YYYYMMDD，可空） | 用于区间判断 |
| `CUR_SIGN` | 最新标志 | 可用于“仅取最新成份”模式（可选） |

### 2.2 “某交易日成份股”判定规则（建议按 Wind 说明）

对给定 `trade_date=YYYYMMDD`，某成份记录在该日有效的条件建议为：

- `S_CON_INDATE <= trade_date`
- 且 `S_CON_OUTDATE IS NULL OR S_CON_OUTDATE >= trade_date`

原因：
- Wind FAQ 明确提到：剔除日期是生效日期的前一交易日；纳入日期是生效日当天（交易日）。因此 `OUTDATE` 更像“最后一个有效交易日”，应当 **包含当日**。

> 这里建议在开发时对比 `CUR_SIGN=1` 的最新成份做 spot-check，确认 `OUTDATE` 的边界是否需要 `>`/`>=`。

### 2.3 “主指数/副指数”缺失的处理策略

Wind FAQ 提示：`AIndexMembers` 仅提供主指数成份，副指数（如 `399932.SZ`）可能查不到，需要用 `RalatedSecuritiesCode`（`S_RELATION_TYPCODE=115002004`）映射到主指数。

第一阶段建议：
- 仅覆盖一组“常见主指数”作为 `pool_code`（例如沪深300等），规避副指数问题。

第二阶段可扩展：
- 增加 “pool_code 规范化层”：当输入 `pool_code` 查不到时，尝试用关系表映射到主指数并记录映射关系（可落配置或落表）。

---

## 3. 代码结构与职责（建议落地方式）

### 3.1 新增/修改的核心文件（建议）

**入口脚本（CLI）**
- 新增：`task_piplines/train_data_update/run_stk_pool_pipline.py`

**Tasks（最小原子能力）**
- 新增：`src/tasks/index_stk_pool_task.py`（第一阶段实现）
- 预留：`src/tasks/custom_stk_pool_task.py`（第二阶段实现，先占位或不实现）

**表结构（复用 TableSchemaBuilder 模式）**
- 增加：`src/utils/table_schema.py` 中新增一个 schema builder，例如：
  - `TableSchemaBuilder.create_stk_pool_table_schema()`

**表配置注册（可选但强烈建议）**
- 增加：`configs/db/local_db_configs.yaml` 新条目 `ai_is.stk_pool_of_index`
  - 这样 `LocalTestDBDataProvider.fetch_data()` 可以工程化读表（并通过 `column_filters` 筛 `pool_code`）

**pipeline 配置（建议新增 YAML）**
- 新增：`configs/pipelines/stk_pool.yaml`（或 `configs/stk_pool/index_pools.yaml`）
  - 维护“常见指数列表、更新参数、目标表名、重叠天数”等，避免入口脚本硬编码。

### 3.2 和现有链路的关系

- 该 pipeline 可作为 `master_scheduler.py` 中的一个子脚本加入每日 00:30 链路：
  - 建议位置：`run_nas_data_pipeline.py` 与 `run_daily_data_pipeline.py` 之后，`factors_share_iq_pipline.py` 之前（取决于你后续是否会用股票池约束因子导出/训练集构建）。

---

## 4. 指数池 Task：数据流与实现细节（第一阶段）

### 4.1 输入与输出

**输入**
- 整体的输入输出可以参考task_piplines\train_data_update\run_daily_data_pipeline.py 和src\tasks\forbid_pool_generation_task.py的输入输出
比较重要的如下：

- `pool_codes`：需要更新的指数列表（如 `['000300.SH','000905.SH',...]`）
- 日期范围：
  - init：全量历史（例如从 `20050104` 起）
  - latest：从目标表最新日期往后更新（带 overlap）
  - range/date：指定区间/指定日

**输出**
- 写入 `ai_is.stk_pool_of_index`：
  - `trade_date, pool_code, stock_code, signal=1, insert_time`

### 4.2 推荐的取数方式（仅方案 A）：直接 SQL 取数（Wind 引擎）+ pandas 处理

理由：
- `AIndexMembers` 的“有效区间”逻辑（IN/OUTDATE）不符合 `MarketDataProvider` 的字段映射范式（它偏向 `TRADE_DT + S_INFO_WINDCODE` 的日频表）。
- 当前工程里大量 task 已经直接 `pd.read_sql(sql, db_config.get_wind_engine())`，风格一致（见 `src/tasks/forbid_pool_generation_task.py`）。

核心 SQL（对单日）：

```sql
SELECT
  S_INFO_WINDCODE AS pool_code,
  S_CON_WINDCODE  AS stock_windcode
FROM wind_quant.dbo.AIndexMembers
WHERE S_INFO_WINDCODE IN (...)
  AND S_CON_INDATE <= :trade_date
  AND (S_CON_OUTDATE IS NULL OR S_CON_OUTDATE >= :trade_date)
```

Python 侧处理要点：
- `stock_windcode` → `stock_code`：按 `configs/db/table_config.yaml` 的 `output_format.remove_all_suffix` 去后缀并确保 6 位（必要时 `zfill(6)`）
- 添加：
  - `trade_date`（DATE）
  - `signal=1`
  - `insert_time=datetime.utcnow()`
- 用 `TestDBManager.save_dataframe(..., mode='update', pk_fields=['trade_date','pool_code','stock_code'])` upsert

### 4.3 增量更新策略（latest 模式建议）

目标：每天跑时只补新交易日，且保证成份调整日不会漏更新。

建议策略：
1. 从目标表里查该 `pool_code` 的最新 `trade_date`（如无数据则走 init）
2. `start_date = max_date - overlap_days`（例如 10~20 天）
3. 用 Wind 交易日历 `AShareCalendar` 得到 `[start_date, end_date]` 的交易日列表
4. 对每个交易日计算成员并 upsert

说明：
- overlap 的意义：成份调整可能会导致最近若干天成员变化，重算可避免边界遗漏。
- 与 `run_nas_data_pipeline.py` 的 overlap 思路一致。

### 4.4 性能建议（避免 “逐日 × 逐指数” 的 SQL 爆炸）

如果 pool_codes 多、日期跨度大，逐日逐指数查询会很慢。建议在实现时优先采用：

- **逐日但一次查全指数**：对每个 `trade_date`，`S_INFO_WINDCODE IN (pool_codes)` 一次性取回所有指数该日成份
- 或者 **区间展开**（更快但代码复杂）：
  - 一次性取回指定指数的全部 IN/OUT 区间记录
  - 在 Python 用 trade_date 列表做向量化判断并展开到长表（注意内存）

第一阶段你可先做“逐日一次查全指数”，通常已足够。

---

## 5. Pipeline 脚本：`run_stk_pool_pipline.py`（建议 CLI 设计）

参考 `task_piplines/train_data_update/run_nas_data_pipeline.py` 的模式，建议支持：

- `--init`：全量初始化（可选 `--start_date/--end_date`，可选 `--batch-size`）
- `--latest`：增量更新（可选 `--overlap-days`）
- `--date YYYYMMDD`：跑单日
- `--range START END`：跑区间
- `--pool-codes 000300.SH 000905.SH ...`：覆盖默认指数列表
- `--config <path>`：指定 YAML 配置（默认读取你放在 `configs/` 的位置）
- （可选）`--schedule`：常驻定时（如果你希望单脚本常驻；否则交给 `master_scheduler.py` 更统一）

建议默认行为：
- 不传任何模式时，默认等价 `--latest`
- 默认 pool_codes 从配置文件读取（避免写死在脚本里）
- `end_date=None` 时不使用“自然日当天”，而是使用“最近一个交易日/最近一个可用日”（与现有 pipeline 口径保持一致）

---

## 6. 配置与注册（建议你审核时确认的点）

### 6.1 `configs/db/local_db_configs.yaml` 增加表登记（推荐）

建议新增：
- key：`ai_is.stk_pool_of_index`
- `table_type: flag`
- `date_field: trade_date`
- `code_field: stock_code`
- `signal_field: signal`
- `output_transform_sequence: ['output_format.remove_all_suffix']`

这样你可以用现成工具读取：
- `src/data_service/data_loading/local_testdb_data.py:LocalTestDBDataProvider.fetch_data()`
- 并通过 `column_filters={'pool_code': ['000300.SH']}` 筛选某个池

### 6.2 `src/utils/table_schema.py` 增加 schema builder（推荐）

仿照 `create_forbid_table_schema()`，新增 `create_stk_pool_table_schema()`：
- 多一个 `pool_code` 字段
- 主键 3 列

目的：
- 新 task 里可沿用 `TestDBManager.create_table()` 的“表不存在则创建”逻辑

---

## 7. 验证与回归（建议最小化验证清单）

1. 对账检查（建议作为开发完成后的第一条验证）：
   - 目标：对 `000852.SH` 在指定日期的成份股与离线基准一致
   - 基准文件：
     - `docs/data_pipline/000852.SH-成分及权重-20221224.xlsx`
     - `docs/data_pipline/000852.SH-成分及权重-20251222.xlsx`
   - 对账口径：**只比成份股集合（stock_code 列表），暂不比权重**（本阶段数据源是 `AIndexMembers`，不含权重）
2. 小区间 dry-run：
   - 例如跑 `000300.SH` 的最近 10 个交易日，确认每日成份数量合理（≈300）
3. 边界检查：
   - 选一条发生调整的日期附近，确认 INDATE/OUTDATE 的 `<=`/`>=` 边界处理正确
4. DB 落表检查：
   - 主键唯一性（无重复）
   - `stock_code` 是否统一为 6 位数字且无后缀
5. 读取检查：
   - 用 `LocalTestDBDataProvider.fetch_data(table, column_filters={'pool_code':[...]} )` 能正常取回

### 7.1 阶段一验收记录（已完成）

> 说明：阶段一只验收“指数池表”写入是否正确（成份股集合），不验收权重。

- 初始化建表 + 全量更新：
  - `./.venv_wsl/bin/python task_piplines/train_data_update/run_stk_pool_pipline.py --init`
  - 目标表：`ai_is.stk_pool_of_index`
- Excel 基准对账（仅集合，对应 `pool_code=000852.SH`）：
  - 2025-12-22：  
    `./.venv_wsl/bin/python tools/verify_stk_pool_index.py --pool-code 000852.SH --excel docs/data_pipline/000852.SH-成分及权重-20251222.xlsx`  
    结果：✅ `Sets match`
  - 2022-12-24（非交易日，取最近交易日 2022-12-23 对账）：  
    `./.venv_wsl/bin/python tools/verify_stk_pool_index.py --pool-code 000852.SH --trade-date 20221223 --excel docs/data_pipline/000852.SH-成分及权重-20221224.xlsx`  
    结果：✅ `Sets match`
- 计数 sanity-check（同一交易日，task 提取 vs DB 落表一致）：  
  - `000300.SH=300`, `000905.SH=500`, `000852.SH=1000`（以 2025-12-22 为例）

---

## 8. 第二阶段（自定义股票池）预留设计

后续自定义池来源可能包括：
- 模型训练输出（例如选股结果）
- 第三方清单
- 规则筛选（如“可交易池”“白名单池”等）

按你当前的规划：自定义股票池将建设独立新表（避免与指数池表混在一起导致单表过大）。

可以复用的点：
- pipeline 入口脚本仍然可以复用同一个 `run_stk_pool_pipline.py`，内部以不同 task 写入不同目标表
- `stock_code` 依然统一为 6 位数字无后缀（保证可与因子/标签/禁投池 join）

---

## 9. 已确认口径 & 待检查项

已确认：
1. 第一阶段表名：`ai_is.stk_pool_of_index`
2. 成份股字段：`stock_code`
3. `stock_code` 格式：6 位数字、无后缀（`.SZ/.SH/.BJ/...` 全部移除）
4. 取数方式：仅采用“直接 SQL（Wind 引擎）+ pandas 处理”
5. `end_date=None`：按“最近一个交易日/最近一个可用日”处理（不直接取自然日当天）

待开发时做一次快速 check：
- INDATE/OUTDATE 边界：使用 `S_CON_INDATE <= trade_date` 且 `S_CON_OUTDATE IS NULL OR S_CON_OUTDATE >= trade_date`，并用 `000852.SH` 的两份 Excel 基准做对账验证
