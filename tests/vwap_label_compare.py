import pandas as pd
from src.data_service.data_loading.local_testdb_data import LocalTestDBDataProvider

provider = LocalTestDBDataProvider()

# 1. 拉取VWAP数据
print("\n=== 1. 拉取VWAP数据 ===")
df_vwap = provider.fetch_data(
    table='ai_is.intermediate_training_factors_market_normalize_lag30_countday1',
    start_date='20030101',
    end_date='20030120',
    stock_codes=['000001'],
    fields=['vwap_lag_0'],
    format='wide'
)
df_vwap = df_vwap.sort_values('trade_date')
print(df_vwap[['trade_date', 'stock_code', 'vwap_lag_0']])

# 2. 手动计算未来10日VWAP收益率
print("\n=== 2. 手动计算未来10日VWAP收益率 ===")
vwap_series = df_vwap.set_index('trade_date')['vwap_lag_0']
future_return = (vwap_series.shift(-10) / vwap_series - 1).dropna()
future_return = future_return.reset_index()
future_return.columns = ['trade_date', 'manual_vwap_return_10d']
print(future_return)

# 3. 拉取label表数据
print("\n=== 3. 拉取label表数据（label_raw） ===")
df_label = provider.fetch_data(
    table='ai_is.training_label_ls10_adj_topcor_cr30_cw240',
    start_date='20030101',
    end_date='20030120',
    stock_codes=['000001'],
    fields=['label_raw'],
    format='long'
)
df_label = df_label[df_label['field_name'] == 'label_raw']
df_label = df_label.sort_values('trade_date')
print(df_label[['trade_date', 'stock_code', 'value']])

# 4. 合并比对
print("\n=== 4. 手动计算 vs 数据库label_raw 对比 ===")
compare = pd.merge(
    future_return,
    df_label[['trade_date', 'value']],
    on='trade_date',
    how='left'
)
compare.rename(columns={'value': 'db_label_raw'}, inplace=True)
print(compare) 