import os
import sys
import torch
import pandas as pd
import numpy as np
from tqdm import tqdm
from datetime import datetime
from scipy.stats import spearmanr, pearsonr
import re

# 添加项目根目录到Python路径
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../../..'))
sys.path.insert(0, project_root)

from src.models.rnn.gru.dfzq_gru.get_configs import DFZQGRUConfig
from src.models.rnn.gru.dfzq_gru.dfzq_gru import DFZQGRU
from src.train.Neural_networks.RNN.DFZQ_GRU.config import TrainingConfig
from src.train.Neural_networks.RNN.DFZQ_GRU.dfzq_Dataloader import get_train_valid_test_loaders
from src.data_service.data_loading.local_testdb_data import LocalTestDBDataProvider

# ---------------------- 配置 ----------------------
MODEL_CKPT = 'outputs/DFZQ_GRU_MODEL_vd_20190101_20211231_t_20080101_20181231_l2_lr3e-05_attn_pv_v5_pv_v4_price&trade_pt10818_20250724_091947/ckpt/best_model.pth'
# 使用完整UNC路径，不再依赖NASConnection默认base_path
NAS_FULL_PATH = r'\\space\signalAI\alpha_lic_pvnet\alpha_agru_day1_p10_f30_0123'
LABEL_TABLE = 'ai_is.training_label_ls10_adj_topcor_cr30_cw240'
LABEL_FIELD = 'label_raw'
START_DATE = '20210101'
END_DATE = '20241231'

# ---------------------- 1. 推理获得模型预测 ----------------------
def get_model_predictions():
    cfg = TrainingConfig()
    device = torch.device('cuda' if torch.cuda.is_available() and not cfg.force_cpu else 'cpu')
    dl_cfg = {
        "dataset_path": cfg.dataset_path,
        "batch_size": cfg.batch_size,
        "num_workers": cfg.num_workers,
        "shuffle": cfg.shuffle,
        "seed": cfg.seed,
        "chunk_size": cfg.chunk_size,
        "memory_limit": cfg.memory_limit,
        "use_fixed_indices": cfg.use_fixed_indices,
    }
    _, _, test_loader = get_train_valid_test_loaders(
        dl_cfg,
        keep_meta_train=False,
        keep_meta_eval=True,
        use_fixed_indices=cfg.use_fixed_indices
    )
    # 加载checkpoint并使用其中保存的配置
    state = torch.load(MODEL_CKPT, map_location=device, weights_only=False)
    
    # 从checkpoint中获取训练配置
    if 'training_config' in state:
        saved_cfg = state['training_config']
        # 处理可能是dict也可能是对象的情况
        if hasattr(saved_cfg, 'hidden_size'):
            # 是对象
            input_size = saved_cfg.input_size
            hidden_size = saved_cfg.hidden_size
            num_layers = saved_cfg.num_layers
            dropout = saved_cfg.dropout
            output_size = saved_cfg.output_size
            attention = saved_cfg.attention
            bidirectional = saved_cfg.bidirectional
        else:
            # 是字典 - 兼容新旧配置键名
            # 新配置使用base_input_size和动态计算的input_size属性
            if 'input_size' in saved_cfg:
                input_size = saved_cfg['input_size']
            elif 'base_input_size' in saved_cfg:
                # 直接使用base_input_size作为input_size（NaN处理已禁用）
                input_size = saved_cfg['base_input_size']
            else:
                raise KeyError("无法在checkpoint配置中找到input_size或base_input_size")
                
            hidden_size = saved_cfg['hidden_size']
            num_layers = saved_cfg['num_layers']
            dropout = saved_cfg['dropout']
            output_size = saved_cfg['output_size']
            attention = saved_cfg['attention']
            bidirectional = saved_cfg['bidirectional']
        
        print(f"使用checkpoint中保存的配置: hidden_size={hidden_size}, input_size={input_size}")
        
        model_cfg = DFZQGRUConfig()
        model_cfg.input_size = input_size  # 使用保存的配置
        model_cfg.hidden_size = hidden_size
        model_cfg.num_layers = num_layers
        model_cfg.dropout = dropout
        model_cfg.output_size = output_size
        model_cfg.attention = attention
        model_cfg.bidirectional = bidirectional
    else:
        # 备用：使用当前配置
        print("未找到保存的训练配置，使用当前配置")
        model_cfg = DFZQGRUConfig()
        model_cfg.input_size = cfg.input_size
        model_cfg.hidden_size = cfg.hidden_size
        model_cfg.num_layers = cfg.num_layers
        model_cfg.dropout = cfg.dropout
        model_cfg.output_size = cfg.output_size
        model_cfg.attention = cfg.attention
        model_cfg.bidirectional = cfg.bidirectional
    
    model = DFZQGRU(model_cfg)
    
    if 'model_state_dict' in state:
        model.load_state_dict(state['model_state_dict'])
    else:
        model.load_state_dict(state)
    model.to(device)
    model.eval()
    # 批量收集
    all_dates = []
    all_codes = []
    all_preds = []
    with torch.no_grad():
        for feats, labels, dates, codes in tqdm(test_loader, desc='Model Predict'):
            feats = feats.permute(0, 2, 1).to(device).float()
            preds, _ = model(feats)
            preds = preds.squeeze(-1).cpu().numpy()
            all_dates.extend(pd.to_datetime(dates))
            all_codes.extend(codes)
            all_preds.extend(preds)
    df_pred = pd.DataFrame({
        'trade_date': all_dates,
        'stock_code': all_codes,
        'model_pred': all_preds
    })
    return df_pred

# ---------------------- 2. 读取label ----------------------
def get_label_df(stock_codes):
    provider = LocalTestDBDataProvider()
    df_label = provider.fetch_data(
        table=LABEL_TABLE,
        start_date=START_DATE,
        end_date=END_DATE,
        stock_codes=stock_codes,
        fields=[LABEL_FIELD],
        format='long'
    )
    # 只保留trade_date, stock_code, value
    df_label = df_label[df_label['field_name'] == LABEL_FIELD][['trade_date', 'stock_code', 'value']]
    df_label = df_label.rename(columns={'value': 'label'})
    return df_label

# ---------------------- 3. 读取NAS同事因子 ----------------------
def get_nas_factor_df(stock_codes):
    # 直接使用完整UNC路径，不依赖NASConnection的base_path配置
    if not os.path.exists(NAS_FULL_PATH):
        raise RuntimeError(f"无法访问NAS路径: {NAS_FULL_PATH}，请检查网络连接和路径")
    
    # 直接列出目录中的文件
    try:
        files = sorted(os.listdir(NAS_FULL_PATH))
        print(f"在{NAS_FULL_PATH}中找到{len(files)}个文件")
    except Exception as e:
        raise RuntimeError(f"列出NAS目录内容失败: {e}")
    
    date_pattern = re.compile(r'alpha_agru_day1_p10_f30_0123\.(\d{8})\.csv$')
    factor_dfs = []
    for fname in tqdm(files, desc='读取NAS因子'):
        m = date_pattern.search(fname)
        if not m:
            continue
        date_str = m.group(1)
        if not (START_DATE <= date_str <= END_DATE):
            continue
        trade_date = pd.to_datetime(date_str)
        full_file_path = os.path.join(NAS_FULL_PATH, fname)
        try:
            df = pd.read_csv(full_file_path, header=None, names=['stock_code', 'factor_value'], dtype={'stock_code':str}, sep=',')
            df['trade_date'] = trade_date
            df = df[['trade_date', 'stock_code', 'factor_value']]
            factor_dfs.append(df)
        except Exception as e:
            print(f"读取文件 {full_file_path} 失败: {e}")
            continue
    if not factor_dfs:
        raise RuntimeError('未找到NAS因子数据文件或文件格式不符')
    df_nas = pd.concat(factor_dfs, ignore_index=True)
    df_nas['trade_date'] = pd.to_datetime(df_nas['trade_date'])
    return df_nas

# ---------------------- 4. 对齐并计算IC ----------------------
def compute_ic(df_label, df_pred, df_nas):
    # 合并
    df = df_label.merge(df_pred, on=['trade_date', 'stock_code'], how='inner')
    df = df.merge(df_nas, on=['trade_date', 'stock_code'], how='inner')
    df['year'] = pd.to_datetime(df['trade_date']).dt.year

    # 按日期分组计算IC
    ic_model_daily = df.groupby('trade_date').apply(lambda x: spearmanr(x['label'], x['model_pred'])[0])
    ic_nas_daily = df.groupby('trade_date').apply(lambda x: spearmanr(x['label'], x['factor_value'])[0])
    pearson_model_daily = df.groupby('trade_date').apply(lambda x: pearsonr(x['label'], x['model_pred'])[0])
    pearson_nas_daily = df.groupby('trade_date').apply(lambda x: pearsonr(x['label'], x['factor_value'])[0])
    # 年度IC
    ic_model_year = df.groupby('year').apply(lambda x: spearmanr(x['label'], x['model_pred'])[0])
    ic_nas_year = df.groupby('year').apply(lambda x: spearmanr(x['label'], x['factor_value'])[0])
    pearson_model_year = df.groupby('year').apply(lambda x: pearsonr(x['label'], x['model_pred'])[0])
    pearson_nas_year = df.groupby('year').apply(lambda x: pearsonr(x['label'], x['factor_value'])[0])
    # 年度两因子相关性
    spearman_corr_year = df.groupby('year').apply(lambda x: spearmanr(x['model_pred'], x['factor_value'])[0])
    pearson_corr_year = df.groupby('year').apply(lambda x: pearsonr(x['model_pred'], x['factor_value'])[0])
    # 输出年度表
    result = pd.DataFrame({
        'Spearman_IC_model': ic_model_year,
        'Spearman_IC_nas': ic_nas_year,
        'Pearson_IC_model': pearson_model_year,
        'Pearson_IC_nas': pearson_nas_year,
        'Spearman_corr_model_nas': spearman_corr_year,
        'Pearson_corr_model_nas': pearson_corr_year,
    })
    print("\n年度IC与相关性:")
    print(result.round(4))
    print("\n年度均值:")
    print(result.mean().round(4))
    # 全局均值
    print(f"\n模型因子: Spearman IC={ic_model_daily.mean():.4f}, Pearson IC={pearson_model_daily.mean():.4f}")
    print(f"NAS因子:  Spearman IC={ic_nas_daily.mean():.4f}, Pearson IC={pearson_nas_daily.mean():.4f}")
    print(f"两因子相关性: Spearman={spearman_corr_year.mean():.4f}, Pearson={pearson_corr_year.mean():.4f}")
    return df

# ---------------------- 主流程 ----------------------
def save_model_factors_by_year(df_pred):
    save_dir = os.path.join(os.path.dirname(__file__), 'saved', 'result')
    os.makedirs(save_dir, exist_ok=True)
    df_pred['year'] = pd.to_datetime(df_pred['trade_date']).dt.year
    for year, df_year in df_pred.groupby('year'):
        if 2021 <= year <= 2024:
            out_path = os.path.join(save_dir, f'model_factor_{year}.csv')
            df_year[['trade_date', 'stock_code', 'model_pred']].to_csv(out_path, index=False)
            print(f"保存模型因子: {out_path}, shape={df_year.shape}")

def main():
    print("==== 1. 推理获得模型预测 ====")
    df_pred = get_model_predictions()
    print(f"模型预测shape: {df_pred.shape}")
    print(df_pred.head())
    # 保存模型因子（21-24年，分年存）
    save_model_factors_by_year(df_pred)
    # 获取股票池
    stock_codes = df_pred['stock_code'].unique().tolist()
    print("==== 2. 读取label ====")
    df_label = get_label_df(stock_codes)
    print(f"label shape: {df_label.shape}")
    print(df_label.head())
    print("==== 3. 读取NAS同事因子 ====")
    df_nas = get_nas_factor_df(stock_codes)
    print(f"NAS因子 shape: {df_nas.shape}")
    print(df_nas.head())
    print("==== 4. 对齐并计算IC ====")
    compute_ic(df_label, df_pred, df_nas)

if __name__ == "__main__":
    main()
