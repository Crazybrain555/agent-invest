# -*- coding: utf-8 -*-
"""
因子窗口配置文件
定义每个因子对应的z_windows列表，用于从数据库中筛选对应的标准化窗口数据

"""

# 每个因子对应一组 z_windows（升序、去重）
FACTOR_WINDOWS = {
    # ① 价格
    "adj_close_mar": [1],
    "adj_open_mar":  [1],
    "adj_high_mar":  [1],
    "adj_low_mar":   [1],
    "vwap_mar":      [1,30],
    
    # ② 估值 / 规模双通道 1 + 252  （先 inv_log 再 z，我已经在表数据做过了，所以不用，只要提取对应的zwindows就好了）
    
    # "pe_ttm":   [1],
    # "pb_ratio": [1],
    # "dividend_yield_12m": [252],
    # "float_market_cap":   [1],
    
    
    # ③ 流动性 / 资金流 单通道 20
    "TRADES_COUNT_mar":          [30],
    "large_buy_value_mar":          [30],
    "large_sell_value_mar":          [30],
    "med_buy_value_mar":          [30],
    "med_sell_value_mar":          [30],
    "small_buy_value_mar":          [30],
    "small_sell_value_mar":          [30],
    "inst_buy_value_mar":          [30],
    "inst_sell_value_mar":          [30],
    "large_net_inflow_mar":          [30],


    
    # "large_net_inflow_mar":      [30],   
    # "large_net_inflow":      [66],
    # "moneyflow_pct":         [66],
    "amount_mar":            [30],  #
    # "large_buy_rate_mar":        [30],
    "large_buy_rate":       [0],
    "large_sell_rate":       [0],
    # "initiative_buy_rate_mar":   [30],
    "initiative_sell_rate":   [0],
    
    # "moneyflow_pct":         [0],
    # "close_net_inflow_rate": [0],
    # "pct_change":            [0],
    
    # ④ 技术 / 情绪
    "turnover_rate":  [0],   
    "swing":          [0],
    
    #高频
    "High_VolKurt" : [0],
    "High_PVcor" : [0],
    "MinuVol_call" : [0],
    "MinuVol_coc" : [0],
    "MinuVol_diff" : [0],
    "MinuVol_intra" : [0],
    "MinuVol_rate" : [0],
    "amresid_amount" : [0],
    "apm" : [0],
    "high_RetDV" : [0],
    "high_RetKurt" : [0],
    "high_RetSkew" : [0],
    "high_RetVar" : [0],
    "high_VolSkew" : [0],
    "high_beta" : [0],
    "high_dev" : [0],
    "high_hprice" : [0],
    "high_pvi" : [0],
    "high_vol_close" : [0],
    "high_vol_open" : [0],
    "high_vr" : [0],
    "price2vol" : [0],
    "r2_amount_pm" : [0],
    "residpos_amount_pm" : [0],
     
    
    # ⑤ 状态哑变量
    "up_down_limit_status": [0],
}

def get_all_factor_names():
    """获取所有因子名称（带窗口后缀）"""
    return [f"{fac}_w{w}" for fac, ws in FACTOR_WINDOWS.items() for w in ws]

def get_base_windows():
    """获取每个因子的基准窗口（最小窗口，用于mask计算）"""
    return {k: min(v) for k, v in FACTOR_WINDOWS.items()} 