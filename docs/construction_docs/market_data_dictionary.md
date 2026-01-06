# 市场数据字段数据字典

> 📅 更新时间: 2025-06-12 19:00:00  
> 📂 配置文件: `configs/field_mappings/market_data.yaml`  
> 🎯 版本: v1.1  

## 📋 目录

- [📊 统计摘要](#-统计摘要)
- [🔍 快速参考](#-快速参考)
- [📖 详细字段列表](#-详细字段列表)
  - [按数据类型分类](#按数据类型分类)
  - [按数据源分类](#按数据源分类)  
- [🔎 搜索索引](#-搜索索引)
- [📚 使用说明](#-使用说明)

---

## 📊 统计摘要

- **总字段数**: 53
- **数据类型数**: 14
- **数据源数**: 2
- **数据表数**: 5

### 按数据类型分布

- **amount**: 5个字段
- **cash_flow**: 2个字段
- **change**: 1个字段
- **factor**: 1个字段
- **forecast**: 3个字段
- **percentage**: 1个字段
- **price**: 12个字段
- **profit**: 1个字段
- **ratio**: 20个字段
- **revenue**: 1个字段
- **shares**: 3个字段
- **status**: 3个字段
- **value**: 2个字段
- **volume**: 2个字段

### 按数据源分布

- **gogoal**: 3个字段
- **wind**: 50个字段

### 按数据表分布

- **con_forecast_roll_stk**: 3个字段
- **AShareEODDerivativeIndicator**: 16个字段
- **AShareEODPrices**: 18个字段
- **AShareValuationIndicator**: 12个字段
- **AShareL2Indicators**: 4个字段

## 🔍 快速参考

### Level2主买卖/大单/委托相关
```
initiative_buy_rate, initiative_buy_money, initiative_sell_rate, initiative_sell_money,
large_buy_rate, large_buy_money, large_sell_rate, large_sell_money, entrust_rate
```

### 核心价格字段
```
adj_close, adj_high, adj_low, adj_open, adj_preclose, close, high, low, open
```

### 成交量字段
```
avg_volume_3m, volume
```

### 估值比率字段
```
pb_ratio, pcf_ncf_lyr, pcf_ncf_ttm, pcf_ocf_lyr, pcf_ocf_ttm, pe_deducted_ttm, pe_ratio, pe_ttm, ps_lyr, ps_ttm
```

### 状态字段
```
lowest_highest_status, trade_status, up_down_limit_status
```


## 📖 详细字段列表


### 按数据类型分类

### 💰 价格类

| 字段名 | 数据库字段 | 描述 | 单位 | 滞后 | 数据源 | 表名 |
|--------|------------|------|------|------|--------|------|
| `adj_close` | `S_DQ_ADJCLOSE` | 日收盘价(后复权) | 元 | ✓ | wind | AShareEODPrices |
| `adj_high` | `S_DQ_ADJHIGH` | 日最高价(后复权) | 元 | ✓ | wind | AShareEODPrices |
| `adj_low` | `S_DQ_ADJLOW` | 日最低价(后复权) | 元 | ✓ | wind | AShareEODPrices |
| `adj_open` | `S_DQ_ADJOPEN` | 日开盘价(后复权) | 元 | ✓ | wind | AShareEODPrices |
| `adj_preclose` | `S_DQ_ADJPRECLOSE` | 复权昨收盘价(0元) | 元 | ✓ | wind | AShareEODPrices |
| `close` | `S_DQ_CLOSE` | 收盘价 | 元 | ✓ | wind | AShareEODPrices |
| `high` | `S_DQ_HIGH` | 最高价 | 元 | ✓ | wind | AShareEODPrices |
| `limit_down` | `S_DQ_STOPPING` | 跌停价 | 元 | ✓ | wind | AShareEODPrices |
| `limit_up` | `S_DQ_LIMIT` | 涨停价 | 元 | ✓ | wind | AShareEODPrices |
| `low` | `S_DQ_LOW` | 最低价 | 元 | ✓ | wind | AShareEODPrices |
| `open` | `S_DQ_OPEN` | 开盘价 | 元 | ✓ | wind | AShareEODPrices |
| `vwap` | `S_DQ_AVGPRICE` | 均价(VWAP) | 元 | ✓ | wind | AShareEODPrices |

### 📊 成交量类

| 字段名 | 数据库字段 | 描述 | 单位 | 滞后 | 数据源 | 表名 |
|--------|------------|------|------|------|--------|------|
| `avg_volume_3m` | `AVGVOLUME_3M` | 最近3个月平均成交量 | 股 | ✓ | wind | AShareEODDerivativeIndicator |
| `volume` | `S_DQ_VOLUME` | 成交量 | 股 | ✓ | wind | AShareEODPrices |

### 💱 成交额类

| 字段名 | 数据库字段 | 描述 | 单位 | 滞后 | 数据源 | 表名 |
|--------|------------|------|------|------|--------|------|
| `amount` | `S_DQ_AMOUNT` | 成交金额 | 千元 | ✓ | wind | AShareEODPrices |
| `initiative_buy_money` | `S_LI_INITIATIVEBUYMONEY` | 主买总额 | 万元 | ✓ | wind | AShareL2Indicators |
| `initiative_sell_money` | `S_LI_INITIATIVESELLMONEY` | 主卖总额 | 万元 | ✓ | wind | AShareL2Indicators |
| `large_buy_money` | `S_LI_LARGEBUYMONEY` | 大买总额 | 万元 | ✓ | wind | AShareL2Indicators |
| `large_sell_money` | `S_LI_LARGESELLMONEY` | 大卖总额 | 万元 | ✓ | wind | AShareL2Indicators |

### 📊 比率类

| 字段名 | 数据库字段 | 描述 | 单位 | 滞后 | 数据源 | 表名 |
|--------|------------|------|------|------|--------|------|
| `dividend_yield_12m` | `S_VAL_DIVIDENDYIELD2` | 股息率(近12个月) | % | ✓ | wind | AShareValuationIndicator |
| `free_turnover_rate` | `S_DQ_FREETURNOVER` | 换手率(自由流通股本) | % | ✓ | wind | AShareEODDerivativeIndicator |
| `pb_mrq` | `PB_MRQ` | 市净率(MRQ,含负值) | 倍 | ✓ | wind | AShareValuationIndicator |
| `pb_ratio` | `S_VAL_PB_LF` | 市净率(LF,含负值) | 倍 | ✓ | wind | AShareValuationIndicator |
| `pcf_ncf_lyr` | `S_VAL_PCF_NFLYR_ARD` | 市现率(现金净流量LYR,含负值) | 倍 | ✓ | wind | AShareValuationIndicator |
| `pcf_ncf_ttm` | `S_VAL_PCF_NCFTTM` | 市现率(现金净流量TTM,含负值) | 倍 | ✓ | wind | AShareValuationIndicator |
| `pcf_ocf_lyr` | `S_VAL_PCF_OCFLYR_ARD` | 市现率(经营现金流LYR,含负值) | 倍 | ✓ | wind | AShareValuationIndicator |
| `pcf_ocf_ttm` | `S_VAL_PCF_OCFTTM` | 市现率(经营现金流TTM,含负值) | 倍 | ✓ | wind | AShareValuationIndicator |
| `pe_deducted_ttm` | `S_VAL_PE_DEDUCTED_TTM` | 市盈率(TTM,扣非,含负值) | 倍 | ✓ | wind | AShareValuationIndicator |
| `pe_ratio` | `S_VAL_PE_LYR_ARD` | 市盈率(LYR,含负值) | 倍 | ✓ | wind | AShareValuationIndicator |
| `pe_ttm` | `S_VAL_PE_TTM` | 市盈率(TTM,含负值) | 倍 | ✓ | wind | AShareValuationIndicator |
| `price_dividend_ratio` | `S_PRICE_DIV_DPS` | 股价/每股派息 | 倍 | ✓ | wind | AShareEODDerivativeIndicator |
| `ps_lyr` | `S_VAL_PS_LYR_ARD` | 市销率(LYR,含负值) | 倍 | ✓ | wind | AShareValuationIndicator |
| `ps_ttm` | `S_VAL_PS_TTM` | 市销率(TTM,含负值) | 倍 | ✓ | wind | AShareValuationIndicator |
| `swing` | `SWING` | 振幅 | % | ✓ | wind | AShareEODDerivativeIndicator |
| `turnover_rate` | `S_DQ_TURN` | 换手率 | % | ✓ | wind | AShareEODDerivativeIndicator |
| `initiative_buy_rate` | `S_LI_INITIATIVEBUYRATE` | 主买比率 | % | ✓ | wind | AShareL2Indicators |
| `initiative_sell_rate` | `S_LI_INITIATIVESELLRATE` | 主卖比率 | % | ✓ | wind | AShareL2Indicators |
| `large_buy_rate` | `S_LI_LARGEBUYRATE` | 大买比率 | % | ✓ | wind | AShareL2Indicators |
| `large_sell_rate` | `S_LI_LARGESELLRATE` | 大卖比率 | % | ✓ | wind | AShareL2Indicators |
| `entrust_rate` | `S_LI_ENTRUSTRATE` | 总委比 | % | ✓ | wind | AShareL2Indicators |

### 💎 市值类

| 字段名 | 数据库字段 | 描述 | 单位 | 滞后 | 数据源 | 表名 |
|--------|------------|------|------|------|--------|------|
| `float_market_cap` | `S_DQ_MV` | 流通市值 | 亿元 | ✓ | wind | AShareEODDerivativeIndicator |
| `market_cap` | `S_VAL_MV` | 总市值 | 亿元 | ✓ | wind | AShareEODDerivativeIndicator |

### 📈 股本类

| 字段名 | 数据库字段 | 描述 | 单位 | 滞后 | 数据源 | 表名 |
|--------|------------|------|------|------|--------|------|
| `float_shares` | `FLOAT_A_SHR_TODAY` | 当日流通股本 | 万股 | ✓ | wind | AShareEODDerivativeIndicator |
| `free_shares` | `FREE_SHARES_TODAY` | 自由流通股本 | 万股 | ✓ | wind | AShareEODDerivativeIndicator |
| `total_shares` | `TOT_SHR_TODAY` | 当日总股本 | 万股 | ✓ | wind | AShareEODDerivativeIndicator |

### 📈 涨跌类

| 字段名 | 数据库字段 | 描述 | 单位 | 滞后 | 数据源 | 表名 |
|--------|------------|------|------|------|--------|------|
| `change` | `S_DQ_CHANGE` | 涨跌 | 元 | ✓ | wind | AShareEODPrices |

### 📊 百分比类

| 字段名 | 数据库字段 | 描述 | 单位 | 滞后 | 数据源 | 表名 |
|--------|------------|------|------|------|--------|------|
| `pct_change` | `S_DQ_PCTCHANGE` | 涨跌幅 | % | ✓ | wind | AShareEODPrices |

### 🔢 因子类

| 字段名 | 数据库字段 | 描述 | 单位 | 滞后 | 数据源 | 表名 |
|--------|------------|------|------|------|--------|------|
| `adj_factor` | `S_DQ_ADJFACTOR` | 复权因子 | 无 | ✓ | wind | AShareEODPrices |

### 🏷️ 状态类

| 字段名 | 数据库字段 | 描述 | 单位 | 滞后 | 数据源 | 表名 |
|--------|------------|------|------|------|--------|------|
| `lowest_highest_status` | `LOWEST_HIGHEST_STATUS` | 最高最低价状态 | 无 | ✗ | wind | AShareEODDerivativeIndicator |
| `trade_status` | `S_DQ_TRADESTATUS` | 交易状态 | 无 | ✗ | wind | AShareEODPrices |
| `up_down_limit_status` | `UP_DOWN_LIMIT_STATUS` | 涨跌停状态 | 无 | ✗ | wind | AShareEODDerivativeIndicator | 1涨停  0交易 -1跌停

其中：证券交易状态
-2
待核查
-1
交易
0
停牌
1
新上市
2
除息
3
除权
4
除权除息
5
拆股
6
合并
7
特别处理                                       


### 🔮 预测类

| 字段名 | 数据库字段 | 描述 | 单位 | 滞后 | 数据源 | 表名 |
|--------|------------|------|------|------|--------|------|
| `consensus_forecast` | `con_np_roll` | 共识预测净利润 | 万元 | ✓ | gogoal | con_forecast_roll_stk |
| `consensus_np` | `con_np_roll` | 一致预期净利润 | 万元 | ✓ | gogoal | con_forecast_roll_stk |
| `consensus_np_growth_2y` | `con_npcgrate_2y_roll` | 一致预期净利润两年复合增长率 | % | ✓ | gogoal | con_forecast_roll_stk |

### 💰 利润类

| 字段名 | 数据库字段 | 描述 | 单位 | 滞后 | 数据源 | 表名 |
|--------|------------|------|------|------|--------|------|
| `net_profit_ttm` | `NET_PROFIT_PARENT_COMP_TTM` | 归属母公司净利润(TTM) | 万元 | ✓ | wind | AShareEODDerivativeIndicator |

### 💸 现金流类

| 字段名 | 数据库字段 | 描述 | 单位 | 滞后 | 数据源 | 表名 |
|--------|------------|------|------|------|--------|------|
| `net_cash_increase_ttm` | `NET_INCR_CASH_CASH_EQU_TTM` | 现金及现金等价物净增加额(TTM) | 万元 | ✓ | wind | AShareEODDerivativeIndicator |
| `operating_cash_flow_ttm` | `NET_CASH_FLOWS_OPER_ACT_TTM` | 经营活动产生的现金流量净额(TTM) | 万元 | ✓ | wind | AShareEODDerivativeIndicator |

### 💰 收入类

| 字段名 | 数据库字段 | 描述 | 单位 | 滞后 | 数据源 | 表名 |
|--------|------------|------|------|------|--------|------|
| `operating_revenue_ttm` | `OPER_REV_TTM` | 营业收入(TTM) | 万元 | ✓ | wind | AShareEODDerivativeIndicator |

### 按数据源分类

### 🎯 聚源数据

| 字段名 | 数据库字段 | 描述 | 单位 | 滞后 | 数据源 | 表名 |
|--------|------------|------|------|------|--------|------|
| `consensus_forecast` | `con_np_roll` | 共识预测净利润 | 万元 | ✓ | gogoal | con_forecast_roll_stk |
| `consensus_np` | `con_np_roll` | 一致预期净利润 | 万元 | ✓ | gogoal | con_forecast_roll_stk |
| `consensus_np_growth_2y` | `con_npcgrate_2y_roll` | 一致预期净利润两年复合增长率 | % | ✓ | gogoal | con_forecast_roll_stk |

### 🌪️ Wind数据

| 字段名 | 数据库字段 | 描述 | 单位 | 滞后 | 数据源 | 表名 |
|--------|------------|------|------|------|--------|------|
| `adj_close` | `S_DQ_ADJCLOSE` | 日收盘价(后复权) | 元 | ✓ | wind | AShareEODPrices |
| `adj_factor` | `S_DQ_ADJFACTOR` | 复权因子 | 无 | ✓ | wind | AShareEODPrices |
| `adj_high` | `S_DQ_ADJHIGH` | 日最高价(后复权) | 元 | ✓ | wind | AShareEODPrices |
| `adj_low` | `S_DQ_ADJLOW` | 日最低价(后复权) | 元 | ✓ | wind | AShareEODPrices |
| `adj_open` | `S_DQ_ADJOPEN` | 日开盘价(后复权) | 元 | ✓ | wind | AShareEODPrices |
| `adj_preclose` | `S_DQ_ADJPRECLOSE` | 复权昨收盘价(0元) | 元 | ✓ | wind | AShareEODPrices |
| `amount` | `S_DQ_AMOUNT` | 成交金额 | 千元 | ✓ | wind | AShareEODPrices |
| `avg_volume_3m` | `AVGVOLUME_3M` | 最近3个月平均成交量 | 股 | ✓ | wind | AShareEODDerivativeIndicator |
| `change` | `S_DQ_CHANGE` | 涨跌 | 元 | ✓ | wind | AShareEODPrices |
| `close` | `S_DQ_CLOSE` | 收盘价 | 元 | ✓ | wind | AShareEODPrices |
| `dividend_yield_12m` | `S_VAL_DIVIDENDYIELD2` | 股息率(近12个月) | % | ✓ | wind | AShareValuationIndicator |
| `float_market_cap` | `S_DQ_MV` | 流通市值 | 亿元 | ✓ | wind | AShareEODDerivativeIndicator |
| `float_shares` | `FLOAT_A_SHR_TODAY` | 当日流通股本 | 万股 | ✓ | wind | AShareEODDerivativeIndicator |
| `free_shares` | `FREE_SHARES_TODAY` | 自由流通股本 | 万股 | ✓ | wind | AShareEODDerivativeIndicator |
| `free_turnover_rate` | `S_DQ_FREETURNOVER` | 换手率(自由流通股本) | % | ✓ | wind | AShareEODDerivativeIndicator |
| `high` | `S_DQ_HIGH` | 最高价 | 元 | ✓ | wind | AShareEODPrices |
| `limit_down` | `S_DQ_STOPPING` | 跌停价 | 元 | ✓ | wind | AShareEODPrices |
| `limit_up` | `S_DQ_LIMIT` | 涨停价 | 元 | ✓ | wind | AShareEODPrices |
| `low` | `S_DQ_LOW` | 最低价 | 元 | ✓ | wind | AShareEODPrices |
| `lowest_highest_status` | `LOWEST_HIGHEST_STATUS` | 最高最低价状态 | 无 | ✗ | wind | AShareEODDerivativeIndicator |
| `market_cap` | `S_VAL_MV` | 总市值 | 亿元 | ✓ | wind | AShareEODDerivativeIndicator |
| `net_cash_increase_ttm` | `NET_INCR_CASH_CASH_EQU_TTM` | 现金及现金等价物净增加额(TTM) | 万元 | ✓ | wind | AShareEODDerivativeIndicator |
| `net_profit_ttm` | `NET_PROFIT_PARENT_COMP_TTM` | 归属母公司净利润(TTM) | 万元 | ✓ | wind | AShareEODDerivativeIndicator |
| `open` | `S_DQ_OPEN` | 开盘价 | 元 | ✓ | wind | AShareEODPrices |
| `operating_cash_flow_ttm` | `NET_CASH_FLOWS_OPER_ACT_TTM` | 经营活动产生的现金流量净额(TTM) | 万元 | ✓ | wind | AShareEODDerivativeIndicator |
| `operating_revenue_ttm` | `OPER_REV_TTM` | 营业收入(TTM) | 万元 | ✓ | wind | AShareEODDerivativeIndicator |
| `pb_mrq` | `PB_MRQ` | 市净率(MRQ,含负值) | 倍 | ✓ | wind | AShareValuationIndicator |
| `pb_ratio` | `S_VAL_PB_LF` | 市净率(LF,含负值) | 倍 | ✓ | wind | AShareValuationIndicator |
| `pcf_ncf_lyr` | `S_VAL_PCF_NFLYR_ARD` | 市现率(现金净流量LYR,含负值) | 倍 | ✓ | wind | AShareValuationIndicator |
| `pcf_ncf_ttm` | `S_VAL_PCF_NCFTTM` | 市现率(现金净流量TTM,含负值) | 倍 | ✓ | wind | AShareValuationIndicator |
| `pcf_ocf_lyr` | `S_VAL_PCF_OCFLYR_ARD`