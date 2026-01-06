# Space Signals Pipeline - 使用文档

> **重构日期**: 2024-10  
> **版本**: 2.0 (二级分类版本)

---

## 📋 目录

- [概述](#概述)
- [重构改进](#重构改进)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [使用示例](#使用示例)
- [模块说明](#模块说明)
- [常见问题](#常见问题)
- [迁移指南](#迁移指南)

---

## 概述

Space Signals Pipeline 专注于从 Space NAS (`\\space\signal`) 读取因子数据并入库到 PostgreSQL 数据库。

### 核心功能

- ✅ **二级分类映射**: 支持 `一级分类 -> 二级分类 -> [signals]` 的层级结构
- ✅ **智能路由**: 根据分类自动路由到不同的目标表
- ✅ **未映射处理**: 未映射的 signals 仅记录日志，不入库
- ✅ **高效 UPSERT**: 使用临时表 + COPY + ON CONFLICT 实现高性能入库
- ✅ **数据清洗**: 自动标准化日期、股票代码、数值格式
- ✅ **定时调度**: 支持每日自动运行

### 不再支持的功能

- ❌ ~~Theme 数据处理~~ (请使用专用脚本)
- ❌ ~~Forbid 数据处理~~ (请使用专用脚本)
- ❌ ~~默认 `space_factors` 表~~ (所有数据路由到分类表)

---

## 重构改进

### 1. 二级分类映射

**旧版** (一级分类):
```yaml
growth:
  - qop_stb
  - qop_acc
  - da2ev
```
➜ 表名: `quantitative_growth_signals`

**新版** (二级分类):
```yaml
growth:
  efficiency:
    - qop_stb
    - qop_acc
  investment:
    - da2ev
```
➜ 表名:
- `quantitative_growth_efficiency_signals`
- `quantitative_growth_investment_signals`

### 2. 代码结构化

```
AIQuantLab/
├── run_space_data_pipeline.py           # 入口（精简）
├── src/
│   ├── data_service/pipelines/space_signals/
│   │   ├── mapping.py                   # 映射加载
│   │   └── table_utils.py               # 表管理
│   ├── tasks/
│   │   └── space_signals_ingest.py      # 任务处理
│   └── scheduler/
│       └── space_signals_scheduler.py   # 定时调度
└── configs/
    └── field_mappings/
        ├── factor_mapping.yaml          # 映射配置（你维护）
        └── factor_mapping_example.yaml  # 配置样例
```

### 3. 未映射信号处理

**旧版**: 未映射的 signals 写入默认表 `space_factors`

**新版**: 未映射的 signals 仅记录到日志文件
- 日志路径: `logs/missing_signals_YYYYMMDD.log`
- 每次运行追加记录
- 包含时间戳和日期范围

---

## 快速开始

### 1. 激活虚拟环境

```powershell
# Windows PowerShell
PS F:\AIQuantLab> & f:/AIQuantLab/.venv/Scripts/Activate.ps1
```

### 2. 配置因子映射

编辑 `configs/field_mappings/factor_mapping.yaml`，参考 `factor_mapping_example.yaml`:

```yaml
growth:
  efficiency:
    - qop_stb
    - qop_acc
  investment:
    - da2ev

value:
  valuation:
    - pegl
    - pegs

other:
  - high_beta
  - high_VolVar
```

### 3. 运行管线

```bash
# 处理最近20天的所有 signals
python run_space_data_pipeline.py --latest --range-days 20 --data-type signals

# 处理指定日期范围
python run_space_data_pipeline.py --latest --start-date 20240101 --end-date 20241231

# 处理特定 signals
python run_space_data_pipeline.py --signals qop_stb qop_acc --range-days 30
```

### 4. 检查结果

```bash
# 查看处理日志
cat space_pipeline_20241022.log

# 查看未映射的 signals
cat logs/missing_signals_20241022.log
```

---

## 配置说明

### 1. Space 连接配置

文件: `configs/space_disk/space_config.yaml`

```yaml
nas:
  host: "\\\\space"
  user: "space\\bsshare"
  password: "!@#$QWERasdf"

paths:
  signal_path: "\\\\space\\signal"

database:
  schema: "ai_is"  # 目标 schema

loader:
  overlap_days: 20  # 回溯天数
  batch_size: 100000
```

### 2. 因子映射配置

文件: `configs/field_mappings/factor_mapping.yaml`

#### 格式说明

**二级分类**:
```yaml
一级分类:
  二级分类:
    - signal1
    - signal2
```
➜ 表名: `quantitative_{一级}_{二级}_signals`

**扁平分类** (仅一级):
```yaml
一级分类:
  - signal1
  - signal2
```
➜ 表名: `quantitative_{一级}_signals`

**特殊 other 类**:
```yaml
other:
  - signal1
  - signal2
```
➜ 表名: `quantitative_other_signals`

#### 映射示例

```yaml
# 成长类 - 二级分类
growth:
  efficiency:      # 盈利效率
    - qop_stb
    - qop_acc
  investment:      # 投资增长
    - da2ev
    - dafc2ia

# 价值类 - 二级分类
value:
  valuation:       # 估值
    - pegl
    - pegs
  cashflow:        # 现金流
    - pcf_ratio
    - fcf_yield

# 其他 - 扁平分类
other:
  - high_beta
  - high_dev
  - high_VolVar
```

---

## 使用示例

### 示例 1: 处理所有 signals

```bash
# 处理最近20天的所有可用 signals
python run_space_data_pipeline.py --latest --range-days 20 --data-type signals
```

**执行流程**:
1. 连接 Space NAS (`\\space\signal`)
2. 列出所有可用的 signal 目录
3. 逐个读取数据（按日期范围过滤）
4. 根据映射路由到目标表
5. 未映射的记录到日志

**输出示例**:
```
2024-10-22 10:00:00 - INFO - Processing ALL signals: 20241002 ~ 20241022
2024-10-22 10:00:01 - INFO - Found 120 signals to process
Processing signals: 100%|██████████| 120/120 [05:30<00:00,  2.75s/signal]
2024-10-22 10:05:30 - INFO - Processing completed: 115 success, 5 skipped, 0 failed
2024-10-22 10:05:30 - WARNING - Unmapped signals logged to: logs/missing_signals_20241022.log (count=5)
```

### 示例 2: 处理特定 signals

```bash
# 只处理指定的 signals
python run_space_data_pipeline.py \
  --signals qop_stb qop_acc da2ev pegl \
  --start-date 20240101 \
  --end-date 20241231
```

**适用场景**:
- 重新处理某些因子
- 测试新添加的映射
- 修复数据问题

### 示例 3: 定时调度

```bash
# 启动调度器（每天凌晨1点运行）
python -m src.scheduler.space_signals_scheduler
```

**调度配置** (在 `space_config.yaml`):
```yaml
scheduler:
  enabled: true
  schedule:
    hour: 1
    minute: 0
  max_job_retries: 3
  job_retry_delay: 300  # 5分钟
```

---

## 模块说明

### 1. 映射模块 (`mapping.py`)

**功能**:
- 加载 YAML 映射配置
- 支持二级分类和扁平分类
- 提供信号 → 分类的快速查询

**API**:
```python
from src.data_service.pipelines.space_signals.mapping import FactorMapping

mapper = FactorMapping('configs/field_mappings/factor_mapping.yaml')

# 查询分类
level1, level2 = mapper.category_of('qop_stb')  # ('growth', 'efficiency')

# 获取统计
stats = mapper.statistics()

# 检查未映射
unmapped = mapper.get_unmapped_signals(['qop_stb', 'unknown_factor'])
```

### 2. 表工具模块 (`table_utils.py`)

**功能**:
- 生成标准化表名
- 创建因子表（使用 SQLAlchemy 类型）
- 验证表结构

**API**:
```python
from src.data_service.pipelines.space_signals.table_utils import (
    generate_table_name,
    ensure_factor_table
)

# 生成表名
table = generate_table_name('growth', 'efficiency')
# 'quantitative_growth_efficiency_signals'

# 确保表存在
ensure_factor_table(db, table, schema='ai_is')
```

**表结构**:
```sql
CREATE TABLE ai_is.quantitative_growth_efficiency_signals (
    trade_date DATE NOT NULL,
    stock_code VARCHAR(10) NOT NULL,
    factor_name VARCHAR(128) NOT NULL,
    factor_value FLOAT,
    UNIQUE (trade_date, stock_code, factor_name)
);
```

### 3. 任务模块 (`space_signals_ingest.py`)

**功能**:
- 从 Space NAS 读取数据
- 数据标准化和清洗
- 路由到目标表
- 批量 UPSERT 入库

**API**:
```python
from src.tasks.space_signals_ingest import SpaceSignalsIngest

task = SpaceSignalsIngest(mapping_path='configs/field_mappings/factor_mapping.yaml')

# 处理所有 signals
task.run_latest(start_date=20240101, end_date=20241231)

# 处理特定 signals
task.run_specific(['qop_stb', 'qop_acc'], start_date=20240101, end_date=20241231)
```

### 4. 调度器模块 (`space_signals_scheduler.py`)

**功能**:
- 定时执行任务
- 错误重试机制
- 日志记录

**使用**:
```bash
# 启动调度器
python -m src.scheduler.space_signals_scheduler

# 测试运行（立即执行一次）
python -c "from src.scheduler.space_signals_scheduler import SpaceSignalsScheduler; SpaceSignalsScheduler().run_once()"
```

---

## 常见问题

### Q1: 如何添加新的因子？

**A**: 编辑 `configs/field_mappings/factor_mapping.yaml`，在适当的分类下添加:

```yaml
growth:
  efficiency:
    - qop_stb
    - qop_acc
    - new_factor  # 添加这一行
```

然后重新运行管线即可，表会自动创建（如果不存在）。

### Q2: 如何处理未映射的 signals？

**A**: 检查 `logs/missing_signals_YYYYMMDD.log`，然后：

1. 将需要的 signals 添加到 `factor_mapping.yaml`
2. 不需要的 signals 可以忽略（会继续记录日志）

### Q3: 数据重复怎么办？

**A**: 系统使用 UPSERT 模式（ON CONFLICT），主键为 `(trade_date, stock_code, factor_name)`，重复数据会自动更新，不会产生重复记录。

### Q4: 如何修改目标 schema？

**A**: 编辑 `configs/space_disk/space_config.yaml`:

```yaml
database:
  schema: "your_schema"  # 修改这里
```

### Q5: 性能优化建议

**性能参数** (在 `space_signals_ingest.py`):
```python
self.db.save_dataframe(
    df=df_clean,
    table_name=table_name,
    batch_size=100000,        # 批次大小（可调）
    use_parallel=True,        # 并行处理
    upsert_batch_rows=2000000 # UPSERT 分批阈值
)
```

**建议**:
- 小数据集 (< 10万行): `batch_size=50000`
- 大数据集 (> 100万行): `batch_size=100000-200000`
- 极大数据集 (> 1000万行): 启用 `upsert_batch_rows=2000000`

### Q6: 如何查看入库的数据？

```sql
-- 查看某个因子的数据
SELECT * FROM ai_is.quantitative_growth_efficiency_signals
WHERE factor_name = 'qop_stb'
  AND trade_date >= '2024-01-01'
ORDER BY trade_date DESC, stock_code
LIMIT 100;

-- 统计表中的因子数量
SELECT factor_name, COUNT(*) as cnt
FROM ai_is.quantitative_growth_efficiency_signals
GROUP BY factor_name
ORDER BY cnt DESC;

-- 查看日期范围
SELECT factor_name, 
       MIN(trade_date) as first_date,
       MAX(trade_date) as last_date,
       COUNT(DISTINCT trade_date) as days,
       COUNT(*) as records
FROM ai_is.quantitative_growth_efficiency_signals
GROUP BY factor_name;
```

---

## 迁移指南

### 从旧版迁移到新版

#### 1. 备份旧配置

```bash
# 备份旧的 factor_mapping.yaml
cp configs/field_mappings/factor_mapping.yaml \
   configs/field_mappings/factor_mapping_v1_backup.yaml
```

#### 2. 更新映射配置

**旧版格式**:
```yaml
growth:
  - qop_stb
  - qop_acc
  - da2ev
  - dafc2ia

value:
  - pegl
  - pegs
```

**新版格式**:
```yaml
growth:
  efficiency:
    - qop_stb
    - qop_acc
  investment:
    - da2ev
    - dafc2ia

value:
  valuation:
    - pegl
    - pegs
```

#### 3. 运行测试

```bash
# 测试单个因子
python run_space_data_pipeline.py --signals qop_stb --range-days 1

# 检查日志
cat space_pipeline_*.log
```

#### 4. 全量迁移

```bash
# 处理所有因子
python run_space_data_pipeline.py --latest --range-days 20
```

#### 5. 数据验证

```sql
-- 检查新表是否创建成功
SELECT table_name 
FROM information_schema.tables 
WHERE table_schema = 'ai_is' 
  AND table_name LIKE 'quantitative_%_signals'
ORDER BY table_name;

-- 比对记录数
SELECT 
    table_name,
    (SELECT COUNT(*) FROM [table_name]) as cnt
FROM information_schema.tables 
WHERE table_schema = 'ai_is' 
  AND table_name LIKE 'quantitative_%';
```

---

## 技术细节

### 表命名规范

| 分类结构 | 示例 | 表名 |
|---------|------|------|
| 二级分类 | growth → efficiency | `quantitative_growth_efficiency_signals` |
| 二级分类 | value → valuation | `quantitative_value_valuation_signals` |
| 扁平分类 | momentum | `quantitative_momentum_signals` |
| other 类 | other | `quantitative_other_signals` |

### 数据处理流程

```
1. 读取 Space NAS
   ↓
2. 日期过滤 (start_date ~ end_date)
   ↓
3. 数据标准化
   - trade_date: datetime64[ns]
   - stock_code: str (6位补零)
   - factor_value: float64
   ↓
4. 映射查询
   ↓
5. 路由到目标表
   ↓
6. UPSERT 入库
   - 临时表 + COPY
   - ON CONFLICT UPDATE
   - 唯一键: (trade_date, stock_code, factor_name)
```

### 性能指标

| 数据量 | 处理时间 | 速度 |
|-------|---------|------|
| 10万行 | ~3秒 | 3.3万行/秒 |
| 100万行 | ~20秒 | 5万行/秒 |
| 1000万行 | ~3分钟 | 5.5万行/秒 |

*基于 PostgreSQL 12, 单表 UPSERT, 网络延迟 < 5ms*

---

## 联系与支持

如有问题或建议，请联系：

- **维护者**: yuye zhang
- **邮箱**: zhangyuye@bosera.com
- **文档**: `docs/space_signals_pipeline_README.md`

---

**最后更新**: 2024-10-22

