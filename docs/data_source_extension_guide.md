# 数据源 / 新表接入指南（配置驱动取数）

本项目的数据取数尽量做到“**不写 SQL**”：你只需要在 `configs/` 里把 **表元信息** 和 **字段映射** 接好，`MarketDataProvider` 就能自动拼 SQL 拉数，并返回统一结构的 DataFrame。

本文用于后续维护：当你需要把 **Wind / 聚源 / 其它数据库** 的新表（或新字段）接入进来时，应该改哪些配置、哪些代码、如何自测。

---

## 0. 先理解现有结构（你要改的点都在这里）

### 0.1 配置入口：`configs/field_mapping.yaml`

- 这是“字段映射总入口”，通过 `imports:` 把各个子配置加载进来：
  - `configs/field_mappings/market_data.yaml`
  - `configs/field_mappings/index_data.yaml`
  - `configs/field_mappings/financial_data.yaml`
  - ...

### 0.2 表元信息与代码规则：`configs/db/table_config.yaml`

这个文件承担两类配置：

1) **`code_format_rules`**：股票/指数代码的输入/输出格式转换规则  
2) **`tables`**：每张底表的关键字段（日期字段、代码字段、描述等）

### 0.3 取数内核：`src/data_service/data_loading/market_data.py`

- `MarketDataProvider` 负责：
  - 从 `field_mapping.yaml` 加载你指定的 section（默认 `market_data`）；
  - 从 `table_config.yaml` 获取表的 `date_field` / `code_field`；
  - 按字段配置自动拼 SQL；
  - 统一输出 `trade_date`（datetime）和 `stock_code`（字符串，按 `remove_all_suffix` 去后缀）。

### 0.4 数据源引擎：`src/utils/db_connection.py` + `configs/db/*.yaml`

- `configs/db/wind_db.yaml`、`gogoal_db.yaml` 等定义连接字符串；
- `src/utils/db_connection.py` 初始化 SQLAlchemy engine，并暴露 `db_config.get_wind_engine()` 等。

---

## 1. 场景 A：在现有数据源里新增一张表（最常见）

例如：Wind 新表、Wind 指数表、Gogoal/聚源新表。

### 1.1 在 `configs/db/table_config.yaml` 增加表配置

在 `tables:` 下新增条目（最少需要 `date_field`、`code_field`）：

```yaml
tables:
  <schema>.<dbo>.<table_name>:
    date_field: <TRADE_DT>
    code_field: <S_INFO_WINDCODE>
    code_transform_sequence: []
    description: "<一句话描述>"
    database_type: 'wind'   # 仅用于描述/日志（不决定 engine）
    output_transform_sequence:
      - 'output_format.remove_all_suffix'
```

注意：
- `MarketDataProvider` 会用这两个字段拼 SQL（WHERE / PARTITION BY / ORDER BY）。
- 当前 `MarketDataProvider` **实际使用的是 `remove_all_suffix` 规则来统一输出**（不依赖 `output_transform_sequence`），但建议仍写上，便于未来统一。

### 1.2 在 `configs/field_mappings/*.yaml` 增加字段映射

选择合适的 section 文件：
- 行情类 → `configs/field_mappings/market_data.yaml`
- 指数类 → `configs/field_mappings/index_data.yaml`
- 财务类 → `configs/field_mappings/financial_data.yaml`
- 如果是全新类别，建议新建一个 `configs/field_mappings/<new_section>.yaml`（并在 `field_mapping.yaml` 里 import）

字段映射模板：

```yaml
<section_name>:
  <logical_field_name>:
    value_name: <COLUMN_NAME_IN_DB>
    data_source: wind        # 必须能在 MarketDataProvider.engines 里找到
    table: <schema>.<dbo>.<table_name>
    description: "<字段含义>"
    data_type: <可选：文档用途>
    unit: "<可选：单位>"
    is_lag: true             # 是否允许 feature_lag 自动生成滞后特征
```

关键点：
- `data_source` 不是数据库名，而是 `MarketDataProvider.engines` 的 key（目前内置 `wind` / `gogoal`）。
- `is_lag=true` 才会在 `fetch_data(feature_lag=N)` 时自动生成 `xxx_lag_0..N-1`。

### 1.3 如果你新建了映射文件，别忘了 import

在 `configs/field_mapping.yaml` 的 `imports:` 中加一行，例如：

```yaml
imports:
  - field_mappings/my_new_section.yaml
```

---

## 2. 场景 B：在现有数据源里新增“代码后缀类型”（.CSI / .WI 等）

`MarketDataProvider` 默认会在取数后对 `stock_code` 做“去后缀标准化”，规则来自：
- `configs/db/table_config.yaml` → `code_format_rules.output_format.remove_all_suffix.suffixes`

### 2.1 推荐的做法（输出统一去后缀）

在 `remove_all_suffix.suffixes` 增加你需要支持的后缀：

```yaml
code_format_rules:
  output_format:
    remove_all_suffix:
      type: 'remove_suffix'
      suffixes: ['.SZ', '.SH', '.BJ', '.SHE', '.SHA', 'SZ', 'SH', 'BJ', '.WI', '.CSI']
```

注意：
- 指数代码不一定是 6 位纯数字（例如可能存在 `n99003.CSI`），去后缀后会得到 `n99003`，这是正常的。
- 不要对含字母的代码使用 `pad_zeros`（会把字母丢掉，只剩数字）。

### 2.2 可选：需要“恢复后缀”时怎么做

目前代码只支持 `add_suffix`（基于正则 `pattern`），可以按需在 `add_market_suffix.rules` 补规则；建议先注释为示例，等确认真实编码规则后再启用。

---

## 3. 场景 C：新增一个全新的数据源（比如新库 / 新引擎）

当你要新增一个 `data_source: xxx`（例如 `juyuan`），你需要让三处“认得它”：

### 3.1 新增 DB 连接配置：`configs/db/xxx_db.yaml`

仿照 `configs/db/wind_db.yaml` 写一份：

```yaml
connection_string: ${XXX_DB_CONNECTION_STRING}
pool_size: 10
max_overflow: 20
pool_timeout: 30
pool_recycle: 3600
```

推荐把连接串放到环境变量，避免明文账号密码进仓库。

### 3.2 让 `ConfigLoader` 加载新配置：`src/utils/config_loader.py`

- `get_all_db_configs()` 里维护了 db_types 列表；
- 将你的新类型加进去（例如 `juyuan`）。

### 3.3 让 `Database_connection` 初始化新 engine：`src/utils/db_connection.py`

需要新增：
- `self.juyuan_db = db_configs['juyuan']`
- `self.juyuan_engine = create_db_engine(self.juyuan_db)`
- `def get_juyuan_engine(self): ...`

### 3.4 让 `MarketDataProvider` 能路由到新 engine：`src/data_service/data_loading/market_data.py`

在 `self.engines = {...}` 中加入：

```python
self.engines = {
    'wind': db_config.get_wind_engine(),
    'gogoal': db_config.get_gogoal_engine(),
    'juyuan': db_config.get_juyuan_engine(),
}
```

然后你的字段映射里就可以写：

```yaml
data_source: juyuan
```

> 重要：`MarketDataProvider` 的 SQL 生成目前偏向 SQL Server（`CROSS APPLY` 等），如果新库不是 SQL Server 语法，需要在 `_build_query` 里做方言适配。

---

## 4. Provider 使用与 section 维护（避免“字段冲突”）

现在 `MarketDataProvider` 支持 `sections` 参数：

- 股票行情（默认）：`MarketDataProvider()` 等价于 `MarketDataProvider(sections=['market_data'])`
- 只取指数：`MarketDataProvider(sections=['index_data'])`
- 混合取数（股票 + 指数一起）：`MarketDataProvider(sections=['market_data', 'index_data'])`

推荐实践：
- 如果某个 section 字段较多、且调用语义明确，建议加一个“薄封装 provider”，例如我们新增的：
  - `src/data_service/data_loading/index_data.py` → `IndexDataProvider`

薄封装模板（直接转发，不重复实现 SQL）：

```python
class MySectionProvider:
    def __init__(self):
        self._provider = MarketDataProvider(sections=['my_section'])

    def fetch(self, ...):
        return self._provider.fetch_data(...)
```

---

## 5. 自测（强烈建议每次接表都做一次）

### 5.1 最小取数验证（字段映射 + 表配置是否正确）

你可以在项目根目录新建一个临时脚本（或复用现有脚本模板）：

- 参考：`index_benchmark_manual_test.py`

验证点：
- `fields` 里的每个逻辑字段都能 `_get_field_info()` 通过；
- SQL 能跑通（不会报 `Unknown table` / `Unknown field`）；
- 返回 DataFrame 非空；
- `stock_code` 是否符合预期（是否去后缀、是否被错误 pad 了）。

### 5.2 常见坑位 checklist

- **忘了加 `tables` 配置**：会报 `Unknown table: ...`
- **`data_source` 写错**：会报 `Unknown data source: ...`
- **日期格式**：`MarketDataProvider.fetch_data` 要求 `YYYYMMDD`
- **指数代码带字母**：不要套用 `ensure_6digits`
- **`stock_code_prefixes`**：只适合数字前缀筛选（A 股 0/3/6），指数一般应传 `None`
- **交易日历对齐**：`_get_trading_dates` 使用 AShareCalendar + SSE，如果你接的是非 A 股交易日历数据，可能需要扩展逻辑

---

## 6. 交付规范（避免“过一段时间忘了”）

每次新增表 / 新增字段，建议同时提交：
- 配置改动（`table_config.yaml` / `field_mappings/*.yaml` / `field_mapping.yaml`）
- 一个最小可复现的手工测试脚本或说明（可放 `docs/` 或根目录脚本，明确日期范围与 code 示例）
- 在相关 doc 中补一条记录（例如在本文件末尾追加“变更记录”）

