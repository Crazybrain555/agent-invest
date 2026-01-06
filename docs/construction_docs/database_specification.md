# 博时基金人工智能实验室数据库规范

## 1. 总体说明

### 1.1 适用范围
本规范适用于博时基金人工智能实验室（Bosera AI Lab）的所有数据入库、取数以及后续衍生数据分析，包括但不限于：
- 因子库
- 模型训练数据
- 回测结果
- 模拟盘/实盘净值及持仓数据等

### 1.2 数据库环境
- 数据库类型：TDSQL
- JDBC连接地址：jdbc:postgresql://10.18.37.30:11033,10.18.37.31:11025/postgres?targetServerType=primary&loadBalanceHosts=true&sslmode=disable&reWriteBatchedInserts=true&binaryTransfer=false
- 命令行登录方式：工具登录     psql -h 10.18.37.30 -p 11033 -U ai_is -d ai_is
- 账号：ai_is
- Schema：ai_is
- 端口：11033
- 密码：MjTlahb3bEYf5

## 2. 命名规范

### 2.1 数据库名称
**格式**：全部小写英文字母，单词间使用下划线分隔。

**示例**：bosera_ai_lab

**说明**：
- 名称简洁易懂，避免使用保留字或特殊字符。

### 2.2 Schema 名称
**格式**：全部小写英文字母，单词间使用下划线分隔，目前只有一个ai_is。

**示例**：ai_is

**说明**：
- 与部门、模块或系统名称紧密关联。

### 2.3 表名称
**格式**：全部小写英文字母，单词间使用下划线分隔。

**示例**：stock_prices、model_dataset

**说明**：
- 表名直接体现数据内容。
- 避免使用复数形式或保留字（如user）。
- 表多时，可增加适当前缀，如ai_、risk_。

### 2.4 字段名称
**格式**：全部小写英文字母，单词间使用下划线分隔。

**示例**：trade_date、stock_code

**说明**：
- 字段名简短明了，避免歧义。
- 避免使用保留字（如date、order）。
- 名字体现字段类型或单位（如pct、amount）。

## 3. 数据类型与通用规范

### 3.1 日期和时间
- 类型：TIMESTAMP
- 格式：YYYY-MM-DD HH:MI:SS（24小时制）
- 说明：统一用TIMESTAMP，精度可选（毫秒、微秒）。

### 3.2 股票标识
- 类型：VARCHAR(10) ~ VARCHAR(20)
- 格式：[股票代码]
- 示例：
  - A股：000001
  - 港股：0700
  - 美股：AAPL
- 说明：兼容全球市场，预留足够长度。

### 3.3 通用数值类型
- 类型：DECIMAL(20, 8)
- 适用：因子值、收益率、权重等
- 说明：
  - 20：整数最长位数
  - 8：小数精度
  - 根据业务适当调整，但需保证精度。

### 3.4 JSON / 文本类型
存储复杂数据结构时用JSONB类型或TEXT存储JSON字符串。

## 4. 数据字典示例

关于"生效日期"与"结束日期"：
- 涉及时间区间的表需包含effective_date与end_date。
- end_date为空（NULL）表示持续有效。

### 4.1 模型训练数据（特征工程）
表名：model_dataset

| 字段 | 类型 | 说明 |
|------|------|------|
| date | TIMESTAMP | 数据日期 |
| stock_code | VARCHAR(20) | 股票标识 |
| feature_name | VARCHAR(50) | 特征名称 |
| feature_value | DECIMAL(20,8) | 特征值 |

### 4.2 历史量化因子数据
表名：historical_quant_factors

| 字段 | 类型 | 说明 |
|------|------|------|
| date | TIMESTAMP | 因子日期 |
| stock_code | VARCHAR(20) | 股票标识 |
| factor_name | VARCHAR(50) | 因子名称 |
| factor_value | DECIMAL(20,8) | 因子值 |

### 4.3 AI生成因子数据
表名：ai_generated_factors

| 字段 | 类型 | 说明 |
|------|------|------|
| date | TIMESTAMP | 日期 |
| stock_code | VARCHAR(20) | 股票标识 |
| factor_name | VARCHAR(50) | 因子名称（带前缀如ai_） |
| factor_value | DECIMAL(20,8) | 因子值 |

### 4.4 AI模型回测结果
表名：ai_backtest_results

| 字段 | 类型 | 说明 |
|------|------|------|
| backtest_date | TIMESTAMP | 回测日期 |
| stock_code | VARCHAR(20) | 股票标识 |
| model_name | VARCHAR(50) | 模型名称 |
| return | DECIMAL(20,8) | 回测收益 |
| cum_return | DECIMAL(20,8) | 累计收益 |

### 4.5 AI生成持仓数据（恒生对接）
表名：ai_generated_positions

| 字段 | 类型 | 说明 |
|------|------|------|
| position_date | TIMESTAMP | 持仓生成日期 |
| stock_code | VARCHAR(20) | 股票标识 |
| model_name | VARCHAR(50) | 模型名称 |
| position_weight | DECIMAL(20,8) | 权重 |

### 4.6 AI模拟盘/实盘净值及持仓
表名：ai_portfolio_nav

| 字段 | 类型 | 说明 |
|------|------|------|
| trade_date | TIMESTAMP | 交易日期 |
| portfolio_name | VARCHAR(50) | 组合名称 |
| net_asset_value | DECIMAL(20,8) | 净值 |
| holding_details | JSONB | 持仓详情 |

### 4.7 禁投池数据（时间区间）
表名：restricted_stock_pool

| 字段 | 类型 | 说明 |
|------|------|------|
| effective_date | TIMESTAMP | 生效日期 |
| end_date | TIMESTAMP | 结束日期，NULL表示持续禁投 |
| stock_code | VARCHAR(20) | 股票标识 |
| reason | VARCHAR(255) | 禁投原因 |

### 4.8 另类因子数据
表名：alternative_factors

| 字段 | 类型 | 说明 |
|------|------|------|
| trade_date | TIMESTAMP | 因子日期 |
| stock_code | VARCHAR(20) | 股票标识 |
| factor_name | VARCHAR(50) | 因子名称 |
| factor_value | DECIMAL(20,8) | 因子值 |
| data_source | VARCHAR(50) | 数据来源（如：社交媒体、卫星图像、信用卡等） |
| update_frequency | VARCHAR(20) | 更新频率（如：daily、weekly、monthly） |
| confidence_score | DECIMAL(5,4) | 数据可信度评分（0-1） |

### 4.9 训练中间因子数据
表名：inter_train_factors
曾用：intermediate_training_factors


| 字段 | 类型 | 说明 |
|------|------|------|
| training_index | VARCHAR(50) | 训练序列标识 |
| trade_date | TIMESTAMP | 因子日期 |
| stock_code | VARCHAR(20) | 股票标识 |
| factor_name | VARCHAR(50) | 中间因子名称 |
| factor_value | DECIMAL(20,8) | 因子值 |
| model_version | VARCHAR(50) | 产生该因子的模型版本 |
| is_temporary | BOOLEAN | 是否为临时因子（用完可删除） |
| parent_factor | VARCHAR(50) | 源自哪个原始因子（可为NULL） |

### 4.10 市场数据归一化宽表
表名：intermediate_training_factors_market_normalize_lag{N}_countday{D}

| 字段 | 类型 | 说明 |
|------|------|------|
| trade_date | TIMESTAMP | 数据日期 |
| stock_code | VARCHAR(20) | 股票标识 |
| adj_open | DECIMAL(20,8) | 前复权开盘价 |
| adj_high | DECIMAL(20,8) | 前复权最高价 |
| adj_low | DECIMAL(20,8) | 前复权最低价 |
| adj_close | DECIMAL(20,8) | 前复权收盘价 |
| vwap | DECIMAL(20,8) | 成交量加权平均价 |
| volume | DECIMAL(20,8) | 成交量 |
| amount | DECIMAL(20,8) | 成交额 |
| model_version | VARCHAR(50) | 模型版本号 |
| insert_time | TIMESTAMP | 数据插入时间 |
| is_temporary | BOOLEAN | 是否为临时数据 |

说明：
- 表名中的 {N} 表示滞后特征数量，{D} 表示时间粒度（天数）
- 所有价格相关字段均已通过 adj_close 进行归一化处理
- 数据按 date 和 stock_code 进行索引
- 支持增量更新，通过 insert_time 追踪数据更新时间

### 4.11 训练标签数据 (长表结构)
表名规范：`training_label_ls{N}_adj{METHOD}[_params]`
- `{N}`: label_shift (e.g., 10)
- `{METHOD}`: 调整方法 (e.g., `raw`, `topcor`, `mktneutral`)
- `[_params]`: 可选，包含关键参数 (e.g., `_cr30_cw60` for topcor)

示例表名: `training_label_ls10_adj_topcor_cr30_cw60`

| 字段          | 类型         | 说明                                    |
|---------------|--------------|-----------------------------------------|
| trade_date    | TIMESTAMP    | 交易日期 (标签对应的 T 日)               |
| stock_code    | VARCHAR(20)  | 股票标识                                |
| field_name    | VARCHAR(50)  | 标签字段名 (`label_raw`, `label_adj`)     |
| value         | DECIMAL(20,8)| 标签数值                                |
| label_shift   | INTEGER      | 标签预测天数跨度 (e.g., 10)             |

索引建议：
- 复合主键: (`trade_date`, `stock_code`, `field_name`, `label_shift`)
- 可选索引: (`field_name`, `trade_date`)

## 5. 扩展与维护

### 5.1 字段与类型变动
业务变动时需兼容现有结构，保证统一性。

### 5.2 文档更新同步
字段变更时及时更新数据字典，确保使用方理解一致。

### 5.3 索引与分区策略
大规模表需单独制定索引与分区策略。

### 5.4 权限与安全
分层授权，敏感数据增加安全措施或加密方案。

### 5.5 避免命名冲突
避免与保留字相同，否则添加业务后缀。

## 6. 总结
- 在原基础上增加effective_date与end_date字段，适应禁投池等场景。
- 后续业务扩展，及时迭代，与团队充分沟通，保障数据准确性与可维护性。