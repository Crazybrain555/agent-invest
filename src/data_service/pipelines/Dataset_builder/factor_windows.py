# -*- coding: utf-8 -*-
"""
因子窗口配置文件
定义每个因子对应的z_windows列表，用于从数据库中筛选对应的标准化窗口数据

"""
import logging

logger = logging.getLogger(__name__)

# 每个因子对应一组 z_windows（升序、去重）
FACTOR_WINDOWS = {
    # ① 价格
    "adj_close_mar": [1],
    "adj_open_mar":  [1],
    "adj_high_mar":  [1],
    "adj_low_mar":   [1],
    "vwap_mar":      [1,30,60,90],
    
    # ② 估值 / 规模双通道 1 + 252  （先 inv_log 再 z，我已经在表数据做过了，所以不用，只要提取对应的zwindows就好了）
    
    # "pe_ttm":   [1],
    # "pb_ratio": [1],
    # "dividend_yield_12m": [252],
    # "float_market_cap":   [1],
    
    
    # # ③ 流动性 / 资金流 单通道 20
    # "TRADES_COUNT_mar":          [30],
    # "large_buy_value_mar":          [30],
    # "large_sell_value_mar":          [30],
    # "med_buy_value_mar":          [30],
    # "med_sell_value_mar":          [30],
    # "small_buy_value_mar":          [30],
    # "small_sell_value_mar":          [30],
    # "inst_buy_value_mar":          [30],
    # "inst_sell_value_mar":          [30],
    # "large_net_inflow_mar":          [30],
    
    
    
    # # "large_net_inflow_mar":      [30],   
    # # "large_net_inflow":      [66],
    # # "moneyflow_pct":         [66],
    # "amount_mar":            [30],  #
    # # "large_buy_rate_mar":        [30],
    # "large_buy_rate":       [0],
    # "large_sell_rate":       [0],
    # # "initiative_buy_rate_mar":   [30],
    # "initiative_sell_rate":   [0],
    
    # # "moneyflow_pct":         [0],
    # # "close_net_inflow_rate": [0],
    # # "pct_change":            [0],
    
    # ④ 技术 / 情绪
    "turnover_rate":  [0],   
    "swing":          [0],
    
    #高频
    # "high_VolKurt" : [0],
    "high_PVcor" : [0],
    "MinuVol_Call" : [0],
    # "MinuVol_COC" : [0],
    # "MinuVol_diff" : [0],
    # "MinuVol_intra" : [0],
    "MinuVol_rate" : [0],
    "amresid_amount" : [0],
    "apm" : [0],
    # "high_RetDV" : [0],
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
    # "lgtratio" : [0],    
    "price2vol" : [0],
    # "r2_amount_pm" : [0],
    "residpos_amount_pm" : [0],
     
    
    # ⑤ 状态哑变量
    "up_down_limit_status": [0],
    
    # ==================== Space Signals 因子 ====================
    
    # ⑥ Growth - Profitability (成长-盈利增长和质量) - 32个因子
    "nde2p": [0],              # 季度净利润同比变化占总市值比（含快报预告）
    "ne2e_q": [0],             # 季度净利润同比增长率（含快报预告）
    "npegl": [0],              # 同pegl（未来2年预期增长率/PE，含快报预告）
    "sup_con_np_yg": [0],      # 定期报告（含预告快报）相对分析师预测数据的增长
    "de2p": [0],               # 季度净利润同比变化/总市值
    "nqdroe": [0],             # 季度ROE同比变化（含快报预告）
    "nfes1": [0],              # 净利润同比变化/总资产（含快报预告）
    "opg_q": [0],              # 季度营业利润同比增长率
    "qdroe": [0],              # 季度ROE同比变化
    "sue": [0],                # 历史季度盈利惊喜（净利润同比变化的Z值）
    "fes1": [0],               # 每股季度净利润同比变化占每股总资产比例
    "kfes1": [0],              # 每股季度扣非净利润同比变化占每股总资产比例
    "kqdroa": [0],             # 扣非季度ROA同比变化
    "kqdroe": [0],             # 扣非季度ROE同比变化
    "npegs": [0],              # 同pegs（未来1年预期增长率/PE，含快报预告）
    "qdroa": [0],              # 季度ROA同比变化
    "rrqop_qcpacf": [0],       # 营业利润对资本支出的残差回归（特质利润）
    "rrqop_qncfoa": [0],       # 营业利润对经营现金流的残差回归（特质利润）
    "de2e": [0],               # TTM净利润同比增长率
    "npg_q": [0],              # 季度净利润同比增长率（分母最小1000万）
    "npg_ttm": [0],            # TTM净利润同比增长率（分母最小1000万）
    "npg3_ttm": [0],           # TTM净利润相比3年前的增长率
    "qop_stb": [0],            # 8期季度营业利润同比增长率均值/标准差
    "qop_acc": [0],            # 8期季度营业利润对时间回归的二次项系数
    "qop_dsd": [0],            # 一阶差分（8期季度营业利润同比增长率均值/标准差）
    "qe_stb": [0],             # 8期季度净利润同比增长率均值/标准差
    "roeg_ttm": [0],           # TTM ROE同比变化
    "epsg_ttm": [0],           # TTM EPS同比变化
    "rqnp_rqcpbe": [0],        # 季度净利润同比增长率-季度职工薪酬同比增长率
    "rqop_rqcpbe": [0],        # 季度营业利润同比增长率-季度职工薪酬同比增长率
    "rqop_rqlgae": [0],        # 季度营业利润同比增长率-季度管理费用同比增长率
    "rqop_rcpg": [0],          # 季度营业利润同比增长率-季度采购商品同比增长率
    
    # ⑦ Analyst - Coverage & Rating (分析师-覆盖度和评级) - 10个因子
    "cvg_og1": [0],            # 1个月覆盖度（机构数）
    "cvg_og2": [0],            # 2个月覆盖度（机构数）
    "cvg_og3": [0],            # 3个月覆盖度（机构数）
    "cvg1": [0],               # 1个月覆盖度（同一机构不同日期算多次）
    "cvg2": [0],               # 2个月覆盖度（同一机构不同日期算多次）
    "cvg3": [0],               # 3个月覆盖度（同一机构不同日期算多次）
    "eps_variability2": [0],   # n个月分析师分歧度（至少3家覆盖）
    "drec": [0],               # 最新一致预期评级变化（本月-上月）
    "recud_180_30": [0],       # 评级方向加权（180工作日，30半衰期）
    "rec": [0],                # 最新一致预期评级（90天内3家加权）
    "rec2": [0],               # 最新一致预期评级（90天内1家加权）
    
    # ⑧ Analyst - Earnings Revision (分析师-盈利预测修正) - 10个因子
    "darev_120_60": [0],       # 盈利预测同比变化/总市值（120天，60半衰期）
    "darev_40_10": [0],        # 盈利预测同比变化/总市值（40天，10半衰期）
    "darev_60_20": [0],        # 盈利预测同比变化/总市值（60天，20半衰期）
    "grev3": [0],              # 盈利预测变动（相比3个月前）
    "rev12": [0],              # 过去20天盈利预测相比前一天加权变化值/总市值
    "revudsratio": [0],        # EPS扩散度（上调数-下调数）/（上调数+下调数）
    "revud_120_60": [0],       # 盈利预测方向变化（120天，60半衰期）
    "revuds_120_60": [0],      # 盈利预测方向变化（120天，60半衰期）
    "revuds_40_10": [0],       # 盈利预测方向变化（40天，10半衰期）
    "revuds_60_20": [0],       # 盈利预测方向变化（60天，20半衰期）
    
    # ⑨ Sentiment - Volatility (情绪-波动率) - 37个因子
    "highlow12m": [0],         # 12个月最高价/最低价
    "highlow1m": [0],          # 1个月最高价/最低价
    "highlow24m": [0],         # 24个月最高价/最低价
    "highlow3m": [0],          # 3个月最高价/最低价
    "highlow6m": [0],          # 6个月最高价/最低价
    "i_volatility": [0],       # 残差波动率
    "ivo": [0],                # 残差波动率
    "ivo_ff12m": [0],          # 过去12个月残差波动率（22交易日）
    "ivo_ff1m": [0],           # 过去1个月残差波动率（22交易日）
    "ivo_ff24m": [0],          # 过去24个月残差波动率（22交易日）
    "ivo_ff3m": [0],           # 过去3个月残差波动率（22交易日）
    "ivo_ff6m": [0],           # 过去6个月残差波动率（22交易日）
    "ivr": [0],                # 过去1个月残差波动率（22交易日）
    "ivr_ff12m": [0],          # 过去12个月R方（22交易日）
    "ivr_ff1m": [0],           # 过去1个月R方（22交易日）
    "ivr_ff24m": [0],          # 过去24个月R方（22交易日）
    "ivr_ff3m": [0],           # 过去3个月R方（22交易日）
    "ivr_ff6m": [0],           # 过去6个月R方（22交易日）
    "volatility_actual12m": [0],  # 过去12个月真实波幅均值
    "volatility_actual1m": [0],   # 过去1个月真实波幅均值
    "volatility_actual24m": [0],  # 过去24个月真实波幅均值
    "volatility_actual3m": [0],   # 过去3个月真实波幅均值
    "volatility_actual6m": [0],   # 过去6个月真实波幅均值
    "volatility12m": [0],      # 过去12个月波动率（22交易日）
    "volatility1m": [0],       # 过去1个月波动率（22交易日）
    "volatility24m": [0],      # 过去24个月波动率（22交易日）
    "volatility3m": [0],       # 过去3个月波动率（22交易日）
    "volatility6m": [0],       # 过去6个月波动率（22交易日）
    "volume_volatility12m": [0],  # 成交量波动率12个月
    "volume_volatility1m": [0],   # 成交量波动率1个月
    "volume_volatility24m": [0],  # 成交量波动率24个月
    "volume_volatility3m": [0],   # 成交量波动率3个月
    "volume_volatility6m": [0],   # 成交量波动率6个月
    "maxret33": [0],           # 最近3个月最大3日收益率均值
    "maxret66": [0],           # 最近6个月最大6日收益率均值
    "maxret11": [0],           # 最近1个月日收益率最大值
    "beta3m": [0],             # 个股收益率相对中证全指过去3个月回归系数
    
    # ⑩ Sentiment - Momentum (情绪-动量) - 23个因子
    "cto6m": [0],              # 隔日动量（剔除最近1月后5个月）
    "cthl6m": [0],             # 高低开动量（剔除最近1月后5个月）
    "cthl9m": [0],             # 高低开动量（剔除最近1月后8个月）
    "cto12m": [0],             # 隔日动量（剔除最近1月后11个月）
    "cto9m": [0],              # 隔日动量（剔除最近1月后8个月）
    "drmom12m": [0],           # 收益率排名动量（剔除最近1月后11个月）
    "drmom9m": [0],            # 收益率排名动量（剔除最近1月后8个月）
    "cthl12m": [0],            # 高低开动量（剔除最近1月后11个月）
    "cto24m": [0],             # 隔日动量（剔除最近1月后23个月）
    "drmom6m": [0],            # 收益率排名动量（剔除最近1月后5个月）
    "cthl24m": [0],            # 高低开动量（剔除最近1月后23个月）
    "drmom24m": [0],           # 收益率排名动量（剔除最近1月后23个月）
    "momvol7m": [0],           # 过去7个月波动率调整后动量
    "momentum_t12": [0],       # 过去1年价格对时间回归的t值（剔除最近1月）
    "momentum": [0],           # 风险模型动量因子
    "momvol10m": [0],          # 过去10个月波动率调整后动量
    "demom8": [0],             # 过去8个月对数收益率和（剔除最近1月，头尾减半）
    "demom7": [0],             # 过去7个月对数收益率和（剔除最近1月，头尾减半）
    "demom9": [0],             # 过去9个月对数收益率和（剔除最近1月，头尾减半）
    "momvol12m": [0],          # 过去12个月波动率调整后动量
    "demom10": [0],            # 过去10个月对数收益率和（剔除最近1月，头尾减半）
    "m52w": [0],               # 当前价格/过去52周最高价格
    "momvol24m": [0],          # 过去24个月波动率调整后动量
    
    # ⑪ Sentiment - Price & Return (情绪-价格和收益率) - 21个因子
    "mad120": [0],             # 当前价/20日均价-1
    "mad560": [0],             # 5日均价/60日均价
    "pairs": [0],              # 个股对行业协整关系（均值回复）
    "pairs_l": [0],            # 个股对行业协整关系（长期）
    # "price2vol": [0],        # 已在高频因子中定义
    "ret12m": [0],             # 过去12个月涨跌幅
    "ret12m_1m": [0],          # 过去12个月涨跌幅（剔除最近1月）
    "ret1m": [0],              # 过去1个月涨跌幅
    "ret24m": [0],             # 过去24个月涨跌幅
    "ret24m_1m": [0],          # 过去24个月涨跌幅（剔除最近1月）
    "ret3m": [0],              # 过去3个月涨跌幅
    "ret3m_1m": [0],           # 过去3个月涨跌幅（剔除最近1月）
    "ret6m": [0],              # 过去6个月涨跌幅
    "ret6m_1m": [0],           # 过去6个月涨跌幅（剔除最近1月）
    "revs": [0],               # 价格反转（最近40日回报时间加权）
    "revsTA": [0],             # 单笔成交金额反转（高低单笔金额差异）
    "revsTV": [0],             # 单笔成交量反转（高低单笔量差异）
    "spb": [0],                # 类似pairs（收益率回归相关性分组）
    "tprtn1": [0],             # 一致预期潜在收益率（3家以上）
    "tprtn2": [0],             # 一致预期潜在收益率（1家以上）
    "tskew": [0],              # 最近1年收益率偏态
    
    # ⑫ Sentiment - Value Reversal (情绪-价值反转) - 21个因子
    "mom1_con_pb_roll": [0],   # 最新/1月前一致预期滚动BP-1
    "mom1_con_ps_roll": [0],   # 最新/1月前一致预期滚动SP-1
    "mom3_con_pb_roll": [0],   # 最新/3月前一致预期滚动BP-1
    "nre2p": [0],              # 过去1年相对nEP
    "mom1_con_pe_roll": [0],   # 最新/1月前一致预期滚动EP-1
    "mom3_con_ps_roll": [0],   # 最新/3月前一致预期滚动SP-1
    "rfy12p": [0],             # fy12p当前值相对最近1年Z值
    "mom3_con_pe_roll": [0],   # 最新/3月前一致预期滚动EP-1
    "rcon_pe_roll": [0],       # 一致预期滚动PE当前值相对最近1年Z值
    "rcon_pb_roll": [0],       # 一致预期滚动PB当前值相对最近1年Z值
    "rb2p": [0],               # 历史相对BP
    "rcon_ps_roll": [0],       # 一致预期滚动PS当前值相对最近1年Z值
    "re2p": [0],               # E2P当前值相对最近1年Z值
    "rpegl": [0],              # pegl当前值相对最近1年Z值
    "nrpegl": [0],             # 同rpegl
    "rcon_peg_roll": [0],      # 一致预期滚动PEG当前值相对最近1年Z值
    "rpegs": [0],              # pegs当前值相对最近1年Z值
    "nrpegs": [0],             # 同rpegs
    "rcon_eps_roll": [0],      # 一致预期滚动EPS当前值相对最近1年Z值
    "nrb2p": [0],              # 过去1年相对nBP
    "mom3_con_eps_roll": [0],  # Con_EPS环比增长率（本月/前一月-1）
    
    # ⑬ Sentiment - Liquidity (情绪-流动性) - 28个因子
    "astd12m": [0],            # 12个月成交额/收益率标准差
    "astd1m": [0],             # 1个月成交额/收益率标准差
    "astd24m": [0],            # 24个月成交额/收益率标准差
    "astd3m": [0],             # 3个月成交额/收益率标准差
    "astd6m": [0],             # 6个月成交额/收益率标准差
    "davol": [0],              # 最近20日换手率/最近60日换手率（取负）
    "davol_floatshr": [0],     # 流通股本davol
    "davol_freeshr": [0],      # 自由流通股本davol
    "illiq": [0],              # 非流动性因子（单位成交金额的涨跌幅）
    "liquidty": [0],           # 0.35·STOM+0.35·STOQ+0.30·STOA
    "lowprice": [0],           # 1/昨日收盘价
    "lsf": [0],                # 20日非流动性abs(收益率/成交数量)
    "mad_amount_20240": [0],   # 过去20日/过去240日日均成交额
    "mad_amount_2060": [0],    # 过去20日/过去60日日均成交额
    "mad_volume_20240": [0],   # 过去20日/过去240日日均成交量
    "mad_volume_2060": [0],    # 过去20日/过去60日日均成交量
    "turnover_amount12m": [0], # 最近12个月买卖循环率（取负）
    "turnover_amount1m": [0],  # 最近1个月买卖循环率（取负）
    "turnover_amount24m": [0], # 最近24个月买卖循环率（取负）
    "turnover_amount3m": [0],  # 最近3个月买卖循环率（取负）
    "turnover_amount6m": [0],  # 最近6个月买卖循环率（取负）
    "turnover12m": [0],        # 过去12个月换手率
    "turnover1m": [0],         # 过去1个月换手率
    "turnover24m": [0],        # 过去24个月换手率
    "turnover3m": [0],         # 过去3个月换手率
    "turnover6m": [0],         # 过去6个月换手率
    "vol": [0],                # 最近40天加权换手率
    "vol_freeshr": [0],        # 自由流通股本换手率
}

def get_all_factor_names():
    """获取所有因子名称（带窗口后缀）"""
    return [f"{fac}_w{w}" for fac, ws in FACTOR_WINDOWS.items() for w in ws]

def get_base_windows():
    """获取每个因子的基准窗口（最小窗口，用于mask计算）"""
    return {k: min(v) for k, v in FACTOR_WINDOWS.items()}


