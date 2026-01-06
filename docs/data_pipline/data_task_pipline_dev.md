# 数据处理任务/管道开发指南（tasks / scheduler / task_piplines）

本文档面向“以后要新增数据处理定时任务/一次性任务”的开发者与自动化代理（AI）。目标是：在不熟悉代码的情况下，也能快速知道：

- 现有的数据管道是怎么被触发的（入口、调度方式、运行顺序）
- `src/tasks`、`src/scheduler`、`task_piplines` 各自承担什么职责、如何组合
- 数据库连接/表配置放在哪些 YAML，新增表时需要改哪些配置
- 现成可复用的工具有哪些（读写 DB、读 NAS、日志、配置加载等）
- 开发新任务/新管道的推荐规范（参数、配置、幂等、编码、日志、目录结构）

本指南重点覆盖以下现有脚本与其关联链路：

- `task_piplines/train_data_update/run_nas_data_pipeline.py`
- `task_piplines/train_data_update/run_daily_data_pipeline.py`
- `task_piplines/train_data_update/factors_share_iq_pipline.py`
- `master_scheduler.py`（以上三者的统一定时触发器）

---

## 1. 目录分层与职责边界（建议理解为三层）

### 1.1 `src/tasks`：最小可复用的“任务单元”（原子能力）

- 目标：封装“一件事”，例如：拉取/清洗/标准化/生成标签/生成禁投池/从 NAS 入库等。
- 理想形态：任务应具备清晰的输入参数与输出（或返回成功/失败），可被 CLI、scheduler、pipeline 组合调用。
- 代码约定：
  - 框架里已经提供了 `src/tasks/base.py:BaseTask`（`execute()` 包装 `pre_run/post_run`）。
  - 目前仓库中任务实现并不完全统一：既有继承 `BaseTask` 的（例如 `src/tasks/nas_forbid_data_task.py`），也有自定义 `execute()`/`run()` 的任务类（例如 `src/tasks/market_price_norm_data_initialization.py`、`src/tasks/standardization_parameter_generation.py`）。
  - 新增任务建议优先继承 `BaseTask`，避免接口碎片化（见后文“建议优化方向”）。

### 1.2 `src/scheduler`：定时调度与任务编排（组合 tasks）

- 目标：把多个 `task` 按顺序串起来，并通过定时器触发执行。
- 当前仓库中存在两套调度风格：
  1. 基于 `schedule`（轻量、简单、易部署）：
     - 例：`src/scheduler/nas_get_data_Scheduler.py:NASDataScheduler`
     - 例：`src/scheduler/Dfzq_gru_scheduler.py:DfzqGruScheduler`
  2. 基于 `apscheduler`（更专业的 trigger/持久化能力，但当前链路里不常用）：
     - 例：`src/scheduler/job_runner.py`（注意：`src/scheduler/job_definitions.py` 目前为空，实际落地需补充）

### 1.3 `task_piplines`：可运行的“管道脚本”（CLI 入口，组合 tasks 或 scheduler）

- 目标：提供可直接运行的命令行脚本，面向“运维/日常运行/手动触发/调试”。
- 它可以：
  - 直接调用一个或多个 `task`（例如一个 init/update 脚本）
  - 或者调用一个 `scheduler` 启动常驻循环（例如 `--schedule`）
  - 或者两者都用（先跑一次，再进入 schedule loop）
- 你对分层的判断基本正确：
  - `src/tasks` 是最基础能力层
  - `src/scheduler` 负责组合 tasks + 时间触发
  - `task_piplines` 既可以组合 tasks，也可以组合 scheduler（还可以被 `master_scheduler.py` 再组合一次）

---

## 2. 现有“训练数据更新”链路（从触发到落地）

### 2.1 总触发器：`master_scheduler.py`

`master_scheduler.py` 负责在同一进程内“每天定时”触发多个 pipeline 脚本（子进程执行）：

- 触发时间：默认每天 `00:30`（本地时间）
- 运行顺序与脚本列表：`master_scheduler.py:SCRIPTS_CONFIG`
  1. `task_piplines/train_data_update/run_nas_data_pipeline.py --latest`
  2. `task_piplines/train_data_update/run_daily_data_pipeline.py --step all`
  3. `task_piplines/train_data_update/factors_share_iq_pipline.py ...`
- 关键实现细节：
  - 使用 `sys.executable`，确保在当前虚拟环境 Python 下运行子脚本
  - 子进程注入 `PYTHONPATH=<repo_root>`，保证脚本能 `import src...` / `import configs...`
  - 强制 UTF-8：设置 `PYTHONIOENCODING=UTF-8`、`PYTHONUTF8=1`，减少 Windows/WSL 编码差异导致的日志乱码
- Windows 启动器：`start_master_scheduler.bat`（自动激活 `.venv` 并确保 `schedule` 已安装）

当你新增一个新的 `task_piplines/.../xxx.py` 并希望被统一定时运行时，最直接的方式是在 `master_scheduler.py:SCRIPTS_CONFIG` 里追加一项。

### 2.2 NAS 禁投池入库：`run_nas_data_pipeline.py` → `NASForbidDataTask`

入口脚本：`task_piplines/train_data_update/run_nas_data_pipeline.py`

- 支持运行模式（CLI）：
  - `--init`：历史全量初始化（可配 `--batch-size` 分批）
  - `--latest`：只更新最新日期（默认带 overlap）
  - `--date YYYYMMDD`：指定日期（可用 `--exact-date` 决定是否只跑当天）
  - `--range START END`：指定区间
  - `--schedule`：常驻调度模式（可用 `--schedule-time HH:MM`、`--run-now`）
- 任务实现：`src/tasks/nas_forbid_data_task.py:NASForbidDataTask`
  - 数据源：`src/data_service/data_loading/forbid_data.py:ForbidDataLoader`
  - NAS 访问：`src/utils/nas_connection.py:NASConnection`
  - DB 写入：`src/data_service/data_saving/data_to_testdb.py:TestDBManager.save_dataframe(mode='update', pk_fields=...)`
  - 表结构：`src/utils/table_schema.py:TableSchemaBuilder.create_forbid_table_schema`
- 配置文件：`configs/nas_disk/nas_config.yaml`
  - `loader.*`：文件名规则、列名、编码、overlap_days、batch_size
  - `database.*`：目标表名、主键字段（注意：当前任务实现里 `schema` 字段未被传入 `TestDBManager`，见“建议优化方向”）
- NAS 路径：
  - 当前链路使用硬编码 UNC：`\\\\space\\forbid`（见 `NAS_FORBID_PATH` / `DEFAULT_NAS_PATH`）
  - WSL/Linux 下不能直接用 UNC：应把共享盘挂载到 Linux 路径，然后设置环境变量 `NAS_BASE_PATH`（见 `src/utils/nas_connection.py` 的提示）

### 2.3 日度训练数据加工：`run_daily_data_pipeline.py` → `DfzqGruScheduler`

入口脚本：`task_piplines/train_data_update/run_daily_data_pipeline.py`

- 支持运行模式（CLI）：
  - 默认：立即执行一次，然后退出
  - `--schedule`：常驻 schedule 模式（注意脚本中有一段对 `schedule_tasks` 的 monkey-patch，用于覆盖运行时间）
  - `--step`：选择执行哪一步：`all/normalize/standardize/label/forbid`
- 调度与编排：`src/scheduler/Dfzq_gru_scheduler.py:DfzqGruScheduler`
  - 它内部用 `DataPipelineManager` 顺序调用各步骤
- 各步骤对应的任务/能力：
  - Step 1（normalize / factor_engineering）：
    - `src/tasks/market_price_norm_data_initialization.py:MarketPriceNormDataTask`
    - 依赖窗口配置：`src/data_service/preprocessing/methods/norm_config.py:Z_WINDOW_MAP_FACTOR_ENGINEERING`
    - 产出：写入类似 `ai_is.inter_train_factors_mkt_processed_v3` 的长表
  - Step 2（standardize parameters）：
    - `src/tasks/standardization_parameter_generation.py:StandardParamsGenerator`
    - 产出：统计/标准化参数表（用于后续标准化、也用于 factor generator 的 DB 补齐 z-score 等）
  - Step 4（label generation）：
    - `src/tasks/label_generation_task.py:LabelGenerationTask`
    - 产出：训练标签长表（例如 `ai_is.training_label_v1` 或带 shift 的版本）
  - Step 5（forbid pool generation）：
    - `src/tasks/forbid_pool_generation_task.py:ForbidPoolGenerationTask`
    - 产出：综合禁投池表（例如 `ai_is.forbid_pool_comprehensive`）
- 重要说明：
  - `task_piplines/.../run_daily_data_pipeline.py` 顶部有多份“硬编码 dict 配置”（表名、日期、性能参数等）。这对快速迭代方便，但对长期工程化不利；推荐逐步迁移到 YAML（见后文建议）。

### 2.4 因子导出到 NAS：`factors_share_iq_pipline.py` → `FactorGenerator`

入口脚本：`task_piplines/train_data_update/factors_share_iq_pipline.py`

- 目标：给定 `model_path` 和日期区间，生成模型预测（不回测）并按交易日输出 CSV 到：
  - `\\\\space\\iqshare\\AI_share\\AI_signals\\<factor_name>\\<factor_name>.<YYYYMMDD>.csv`
  - 文件无表头，两列：`stock_code, model_pred`
- 断点续跑：根据 `output_root/<factor_name>/` 里已存在的文件名，自动识别最后日期并从下一天开始。
- 推理引擎：`src/data_service/pipelines/factor_utils/factor_generator.py:FactorGenerator`
  - 模型配置/默认值：`configs/backtest/model_backtest_config.py:ModelBacktestConfig`
  - 自动解析 dataset/schema：`src/data_service/pipelines/factor_utils/config_utils.py:resolve_experiment_and_schema`
  - DB 补齐宽表 lag 特征（关键依赖）：`src/data_service/pipelines/factor_utils/db_fetcher.py:fetch_wide_lag`
    - 底层查询依赖：`src/data_service/data_loading/local_testdb_data.py:LocalTestDBDataProvider`
    - 表字段/类型映射依赖：`configs/db/local_db_configs.yaml`

这意味着：如果你新增了某个“特征表/标签表/统计表/禁投池表”，并希望被 factor generator 或 dataset builder 使用，除了在 DB 里建表，还要在 `configs/db/local_db_configs.yaml` 里登记（见下文“新增表流程”）。

---

## 3. 配置系统（YAML）与约定

### 3.1 `ConfigLoader`：统一加载与缓存

- 实现：`src/utils/config_loader.py:ConfigLoader`
- 默认根目录：`configs/`
- 特性：
  - 读取时使用 `encoding='utf-8'`
  - 支持环境变量占位符：形如 `${ENV_NAME}`
  - 支持缓存（修改配置后如遇到“配置未生效”，可调用 `ConfigLoader.clear_cache()` 或重启进程）

建议：新 pipeline/新 task 的参数尽量放到 `configs/<domain>/<name>.yaml`，由 `ConfigLoader` 加载，而不是在脚本里硬编码 dict。

### 3.2 数据库连接配置：`configs/db/*_db.yaml`

数据库连接由 `src/utils/db_connection.py:db_config` 统一管理：

- 读取的配置文件：
  - `configs/db/wind_db.yaml`
  - `configs/db/gogoal_db.yaml`
  - `configs/db/test_tdsql_db.yaml`（本地/测试环境常用）
  - `configs/db/prod_db.yaml`
- 连接字符串字段：`connection_string`
- 引擎获取：
  - `db_config.get_test_engine()`
  - `db_config.get_wind_engine()` 等

注意：目前这些 YAML 内含明文账号密码。更工程化的做法是把敏感信息改为环境变量引用（`${DB_PASSWORD}`），并在运行环境注入。

### 3.3 表级配置（非常关键）：`configs/db/local_db_configs.yaml`

该 YAML 是“本地测试库/ai_is schema 下各类表”的元数据注册表，多个模块会依赖它来正确读写数据：

- `src/data_service/data_loading/local_testdb_data.py:LocalTestDBDataProvider`（按表类型构造 SQL）
- dataset builder：`src/data_service/pipelines/Dataset_builder/io_tables.py`
- factor generator 的 DB 补齐：`src/data_service/pipelines/factor_utils/db_fetcher.py`

典型条目结构（示意）：

- key：一般使用带 schema 的全名，例如 `ai_is.forbid_pool_comprehensive`
- 常见字段：
  - `date_field` / `code_field`：日期/代码字段名（没有可填 `null`）
  - `table_type`：`wide/long/stat/flag`（决定如何拼 SQL/透视）
  - `database_type`：常见为 `test_tdsql`
  - `description`：说明
  - `output_transform_sequence`：输出代码转换规则（见 `configs/db/table_config.yaml`）
  - 其他：例如 `signal_field`、`field_name_field`、`value_field`、`extra_fields` 等

新增表必须补齐这份配置，否则上层模块可能报错：`ValueError: Unknown table: ...`

### 3.4 代码格式/后缀转换规则：`configs/db/table_config.yaml`

- 定义了 `code_format_rules`（db_format/output_format）
- 常见用途：
  - 将 `000001.SZ` 统一成 `000001`
  - 输出时移除/添加市场后缀
- 典型调用点：`LocalTestDBDataProvider._transform_stock_codes` 等

### 3.5 NAS 配置：`configs/nas_disk/nas_config.yaml`

NAS 禁投池链路读取该配置：

- `loader`：文件匹配规则、`date_pattern` 正则、CSV 编码、列名等
- `database`：目标表名、主键字段等

补充建议：把 `scheduler` 配置也补上（目前脚本/类会读取 `scheduler.enabled`、`scheduler.schedule.hour/minute`，但 YAML 中还没有显式写出）。

---

## 4. 数据库写入/建表：现成工具如何用

### 4.1 `TestDBManager`：统一写入（append/replace/update=upsert）

- 实现：`src/data_service/data_saving/data_to_testdb.py:TestDBManager`
- 常用能力：
  - `check_table_exists(table_name, schema=None)`
  - `create_table(table_name, columns, schema=None)`
  - `save_dataframe(df, table_name, mode='append'|'replace'|'update', pk_fields=[...], schema=None, ...)`
  - `delete_table(...)` / `delete_data(...)`

其中 `mode='update'` + `pk_fields=[...]` 会走 upsert 逻辑，适合日度增量任务（可重复运行，不会产生重复数据）。

### 4.2 `TableSchemaBuilder`：快速生成 schema

- 实现：`src/utils/table_schema.py:TableSchemaBuilder`
- 常用 schema 生成器：
  - `create_forbid_table_schema()`：禁投池表（`trade_date/stock_code/signal/insert_time`）
  - `create_long_factor_table_schema()`：长表因子（`trade_date/stock_code/factor_name/factor_value/...`）
  - `create_factor_table_schema()`：从 DataFrame 推断宽表结构
  - 可能后面还有新增的，你没新增一个类别，就要补充到这里。

当你的任务需要落一张新表时，推荐流程是：

1. 设计好表结构（列名、类型、主键）
2. 用 `TableSchemaBuilder` 生成列定义
3. 用 `TestDBManager.create_table` 创建
4. 用 `TestDBManager.save_dataframe(..., mode='update')` 做日常 upsert

---

## 5. 新增表（ai_is 新表）时必须做的事情

下面流程以“在 `ai_is` schema 建了一个新表，并希望被数据管道/特征补齐/数据集构建使用”为目标。

### 5.1 建表与主键（DB 层）

- 明确主键字段（强烈建议包含 `trade_date` 与 `stock_code`，或能唯一定位一行的组合键）
- 如果要走 upsert：主键/唯一索引必须能约束唯一性，否则会出现重复行或 upsert 不生效

### 5.2 更新表元数据注册：`configs/db/local_db_configs.yaml`

新增一条表配置（示意字段按实际选择）：

- `tables.ai_is.<your_table>`：
  - `date_field` / `code_field`
  - `table_type: long|wide|stat|flag`
  - `database_type: test_tdsql`
  - 如是 long 表，补齐 `field_name_field/value_field`
  - 如是 flag 表，补齐 `signal_field`
  - 如需要代码格式统一，补齐 `output_transform_sequence`

### 5.3 如果任务需要代码转换规则：更新 `configs/db/table_config.yaml`（可选）

- 只有当你需要新的转换规则（比如新的后缀）时才改
- 通常直接复用 `output_format.remove_all_suffix` 即可

### 5.4 如果会被 factor generator 读取：确认“能被 DB 补齐器路由到”

factor generator 的 DB 补齐链路依赖：

- `db_fetcher.fetch_wide_lag` 会用 `LocalTestDBDataProvider` 查表
- `LocalTestDBDataProvider` 必须能从 `local_db_configs.yaml` 识别该表
- 如果新表是“特征表”，还需确认它会出现在：
  - dataset 的 `meta/schema.json`（如果你走 dataset schema 驱动），或
  - `configs/backtest/model_backtest_config.py:ModelBacktestConfig.fetch.features_tables`（如果你走默认回退配置）

---

## 6. 新增 task 的推荐规范（面向可长期维护）

### 6.1 放置位置与命名

- 任务类放在 `src/tasks/` 下
- 文件名建议：`<domain>_<purpose>_task.py`（例如 `nas_forbid_data_task.py`）
- 类名建议：`<Domain><Purpose>Task`

### 6.2 接口建议（尽量统一）

推荐统一成：

- `class XxxTask(BaseTask)`
  - `run(...)`：只写业务逻辑
  - `execute(...)`：由 `BaseTask` 提供，统一异常处理与日志

如果历史原因继续保留 `execute()` 风格，也建议最少做到：

- `run(...)` 返回 `bool` 或返回标准结果对象（避免一会返回 DataFrame、一会返回 bool）
- 支持幂等（重复运行不产生重复数据）

### 6.3 参数化与配置

- “环境相关/可变参数”放 YAML：
  - 表名、schema、主键字段
  - overlap_days、batch_size、并行度等性能参数
  - 路径（NAS 根路径、输出路径等）
- “运行时参数”放 CLI：
  - `--start_date/--end_date`
  - `--latest/--init` 等模式开关
  - `--force-update` 等

### 6.4 幂等与增量（强烈建议）

推荐模式：

- init 模式：全量跑历史区间（可能分批）
- daily/最新模式：只跑最新 + overlap_days（避免因 T+1 数据修订、节假日等导致缺口）
- DB 写入使用 `mode='update'` + `pk_fields=[...]`（upsert）

### 6.5 编码、日期、股票代码（统一约定）

- 文件与日志 I/O：显式 UTF-8（参考 `master_scheduler.py` 已强制子进程 UTF-8）
- 日期：
  - CLI/文件名常用 `YYYYMMDD`
  - 部分任务内部使用 `YYYY-MM-DD`（建议新代码统一输入格式，内部转换）
- 股票代码：
  - 标准输出为 6 位字符串：`zfill(6)`
  - 如带后缀，应在输入/输出边界统一转换（参考 `configs/db/table_config.yaml`）

---

## 7. 新增 scheduler 的推荐规范

当你需要“常驻 + 定时触发”时，再写 scheduler；否则单次 CLI pipeline 就够了。

### 7.1 `schedule` 风格（简单、易部署）

参考：

- `src/scheduler/nas_get_data_Scheduler.py:NASDataScheduler`
- `src/scheduler/Dfzq_gru_scheduler.py:DfzqGruScheduler`

推荐结构：

- `__init__`：加载 config，初始化 task
- `job()`：一次性执行逻辑（捕获异常、记录结果）
- `schedule_tasks()`：注册 schedule 规则
- `run_continuously()`：while loop + `schedule.run_pending()`

### 7.2 `apscheduler` 风格（复杂需求再用）

参考：`src/scheduler/job_runner.py`

适用场景：

- 需要 cron 表达式/多触发器/错过触发补偿/持久化 jobstore 等

注意：当前 job definitions 未落地（`src/scheduler/job_definitions.py` 为空），如果要启用这条路线，需要补齐 job 配置与启动入口。

---

## 8. 新增 task_piplines 管道脚本（CLI 入口）

### 8.1 什么时候写 pipeline 脚本

满足任一条件就值得写：

- 需要被 `master_scheduler.py` 定时触发
- 需要给非开发同学一个“可直接运行”的命令行入口
- 需要把多个 tasks 串起来，并提供参数开关

### 8.2 推荐模板要点

- `argparse` 定义模式与参数（init/latest/date/range/schedule）
- 使用 `src/utils/logger.setup_logger` 或 `logging.basicConfig`，并把日志落到 `logs/` 或脚本目录（注意 UTF-8）
- 不要在脚本里写大量业务逻辑，尽量调用 `src/tasks` / `src/scheduler`
- 保证脚本能在以下两种方式下运行：
  1. 被 `master_scheduler.py` 调用（已注入 `PYTHONPATH`）
  2. 开发者手动运行（必要时在脚本里设置 repo root 到 `PYTHONPATH`，但建议统一通过 `PYTHONPATH` 启动）

### 8.3 接入 `master_scheduler.py`

- 在 `master_scheduler.py:SCRIPTS_CONFIG` 里新增：
  - `script`: 你的脚本路径
  - `args`: 默认参数（建议最安全的日更参数）
  - `description`: 描述

---

## 9. 常见问题与排查

### 9.1 报 `Unknown table: ai_is.xxx`

- 原因：`configs/db/local_db_configs.yaml` 没有登记
- 处理：补齐表配置；必要时同步 `configs/db/table_config.yaml` 的转换规则

### 9.2 WSL 下无法访问 `\\\\space\\...`

- 原因：Linux/WSL 不能直接访问 UNC
- 处理：
  1. 把 NAS 共享挂载到 Linux 路径（例如 `/mnt/nas`）
  2. 设置 `NAS_BASE_PATH=/mnt/nas/...`
  3. 再运行 pipeline（`NASConnection` 会读取该环境变量覆盖 base path）

### 9.3 配置改了但没生效

- 原因：`ConfigLoader` 有缓存
- 处理：重启进程，或在代码里 `ConfigLoader.clear_cache()` / `db_config.reload_configs()`

### 9.4 数据重复/覆盖异常

- 建议：
  - 使用 `TestDBManager.save_dataframe(mode='update', pk_fields=[...])`
  - 确保 DB 层有主键/唯一索引
  - 增量任务使用 overlap_days（例如 3/20/60 天）避免修订数据缺口

---

## 10. 建议优化方向（可做可不做，但会显著提升可维护性）

以下仅针对当前代码框架中较明显、会影响“未来新增任务的工程化成本”的点：

1. 统一 `src/tasks` 的接口风格：新任务尽量继承 `BaseTask`，老任务逐步收敛到 `run()+execute()` 模式。
2. 把 `task_piplines/train_data_update/run_daily_data_pipeline.py` 里的硬编码 dict 配置迁移到 `configs/`：
   - 例如 `configs/pipelines/daily_data_pipeline.yaml`，并通过 `ConfigLoader` 加载。
3. 统一“路径注入/PYTHONPATH 处理”：
   - 当前 `master_scheduler.py` 做得较好；各 pipeline 脚本里对 `sys.path` 的处理不一致，建议统一只靠 `PYTHONPATH`。
4. 敏感信息（DB/NAS 密码）改为环境变量引用，避免明文落仓库。
5. NAS 入库任务完善 schema 传递：
   - `configs/nas_disk/nas_config.yaml` 里有 `schema` 字段，但 `NASForbidDataTask` 目前没有传入 `TestDBManager`，建议补齐。
6. 日志工具 `setup_logger` 建议避免重复添加 handler（多次调用会重复输出），可以按 name 做去重或检查 `logger.handlers`。
7. 逐步把“定时”能力集中到一个地方（`master_scheduler.py` 或 `apscheduler`），避免同一任务链路里同时存在多套常驻调度循环。
