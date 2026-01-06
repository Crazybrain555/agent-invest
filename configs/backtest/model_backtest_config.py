#!/usr/bin/env python3
"""
模型回测配置类
用于定义回测的所有默认参数，便于命令行参数覆盖和后续扩展
"""


class ModelBacktestConfig:
    """模型回测配置类 - 纯静态配置，不包含业务逻辑"""
    
    # =====================================================
    # 📅 时间范围设置
    # =====================================================
    start_date = "20210101"
    end_date = "20241231"
    
    # =====================================================
    # 📁 模型和数据路径
    # =====================================================
    model_path = r'outputs/DFZQ_GRU_MODEL_vd_20190101_20211231_t_20080101_20181231_l2_lr3e-05_attn_pv_v5_pv_v4_price&trade_pt10818_20250724_091947'
    dataset_path = None  # 将从experiment_config.json中自动读取
    backtest_result_path = None  # 如果是None，默认在model_path/bt_results下面
    data_split = None  # 回测推理阶段使用的split；None表示忽略split，仅按日期过滤全量索引
    auto_extend_dataset = True  # 回测前是否自动扩充数据集覆盖范围
    dataset_extend_threads = None  # 生成索引时的线程数（传递给DuckDB）
    
    # =====================================================
    # 💰 资金和持仓配置
    # =====================================================
    initial_capital = 1000000.0  # 初始资金
    max_position_size = 0.1  # 单只股票最大权重10%
    max_stocks = 50  # 最多持有50只股票
    min_market_cap = 1e8 / 10000  # 最小市值1亿（万元单位）

    # =====================================================
    # 🧩 股票池配置（ALL + 指数池）
    # =====================================================
    pool_codes = ["000300.SH", "000905.SH", "000852.SH"]  # 默认指数池
    pool_table = "ai_is.stk_pool_of_index"
    pool_signal_value = 1  # 仅保留 signal=1 的成份
    pool_top_n = 50  # 股票池内选股数量
    market_top_n = max_stocks  # 全市场选股数量（默认沿用 max_stocks）
    
    # =====================================================
    # ⚖️ 权重分配和中性化配置
    # =====================================================
    weight_method = "equal"  # 权重分配方法: equal(等权重), factor_score(因子得分加权)
    neutralize_method = ["industry", "market_cap"]  # 中性化方法: industry(行业), market_cap(市值)
    neutralize_industry_name = "CSI"  # 行业分类标准
    neutralize_algo = "ols"  # 中性化算法: ols(普通最小二乘), wls(加权最小二乘)
    
    # =====================================================
    # ⚡ 交易策略配置
    # =====================================================
    rebalance_frequency = "10D"  # 调仓频率: 1D, 5D, 10D, 1M, 1Q等
    trade_at = "vwap"  # 交易价格: close(收盘价), vwap(成交量加权平均价)
    trade_cost_rate = 2 / 1000  # 交易费率0.05%（单边）
    slippage_ratio = 0.0001  # 滑点比例
    benchmark_code = "000852.SH"  # 基准指数代码（默认中证1000）
    
    # =====================================================
    # 🧮 因子计算配置
    # =====================================================
    factor_return_period = 20  # 因子收益率的未来收益计算周期
    factor_return_calculation_frequency = 20  # 因子收益率的截面回归计算频率
    factor_shift = 1  # 因子滞后一期防止偷看历史
    ic_calculation_period = 20  # IC计算周期
    ic_method = 'spearman'  # IC计算方法: pearson, spearman
    
    # =====================================================
    # 📶 信号处理配置
    # =====================================================
    signal_negative = False  # 信号是否取反
    
    # =====================================================
    # 📊 输出和日志配置
    # =====================================================
    save_excel = True  # 是否保存Excel报告
    print_results = True  # 是否打印详细结果
    enable_detailed_log = False  # 是否启用详细交易记录输出
    detailed_log_path = "logs/detailed_trading_log.csv"  # 详细交易记录文件路径
    log_holdings = True  # 是否记录持仓详情
    log_trades = True  # 是否记录交易详情
    log_costs = True  # 是否记录费用详情
    
    # =====================================================
    # 💾 因子输出和保存配置
    # =====================================================
    factor_target_format = "backtest"  # 因子输出格式: backtest, wind, live
    factor_save_formats = ["csv"]  # 因子保存格式: csv, parquet, database
    enable_factor_save = True  # 是否启用因子保存
    factor_save_total = False  # 是否生成 model_factor_total.csv（默认否）
    
    # =====================================================
    # 🔗 DB补齐配置 (仅供 FactorGenerator / db_fetcher 使用)
    # =====================================================
    class FetchCfg:
        """
        DB数据补齐配置类
        **完全独立** 于 build_pv_dataset_streaming，
        专门用于因子生成时的数据库补齐功能
        """
        # ← 下列默认值 = 当初 dataset 初始化脚本里用的那一套
        seq_len: int = 30                       # 序列长度，与模型训练时保持一致
        features_tables = [
            "ai_is.inter_train_factors_mkt_processed_v3",
            "ai_is.quantitative_other_signals",
        ]
        labels_table = "ai_is.training_label_v1"  # 标签表（补齐时不使用）
        stats_table = "ai_is.train_signals_std_std_2008_2018_mad8p0"  # 统计表
        restricted_table = "ai_is.forbid_pool_comprehensive"  # 股票池限制表
        
        # 数据处理参数
        factor_based_nan_handling = True        # 启用因子配置驱动的NaN处理
        consecutive_nan_threshold = None        # 连续NaN阈值，超过则不填充
        clip_std = True                         # 是否启用标准差截尾
        winsorise_labels = True                 # 是否对标签进行缩尾处理
        label_shift = 10                        # 标签移位，用于标准化计算
        
        # 性能参数
        duck_threads = 8                        # DuckDB线程数
        duck_memory = "16GB"                    # DuckDB内存限制
        duck_cache = "4GB"                      # DuckDB缓存大小
        
        # 股票过滤参数
        code_prefix_blacklist = ["9"]           # 股票代码前缀黑名单（默认过滤 B 股）
        code_blacklist = []                     # 完整股票代码黑名单
        # 透视宽表时的因子分批大小（控制内存峰值）
        max_factors_per_batch = 16
    
    # 实例化fetch配置
    fetch = FetchCfg()
    # 回测默认：不覆盖实验/数据集解析得到的序列长度；统计表使用新表
    fetch.seq_len = None
    fetch.stats_table = "ai_is.train_signals_std_std_2008_2018_mad8p0"
