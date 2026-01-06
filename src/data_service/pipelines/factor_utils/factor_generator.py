#!/usr/bin/env python3
"""
因子生成器 - 负责模型推理生成因子数据
用于将训练好的模型生成回测所需的因子DataFrame
"""
import os
from pathlib import Path
import pandas as pd
import numpy as np
import torch
from typing import Optional, List
from tqdm import tqdm

PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _normalize_path(path_str: Optional[str]) -> Optional[Path]:
    """Normalize paths from Windows/relative forms to usable Posix paths in WSL."""
    if path_str is None:
        return None

    raw = str(path_str)

    # Translate Windows drive (e.g., F:\foo) to /mnt/f/foo when running on Linux/WSL
    if os.name != "nt" and len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        drive = raw[0].lower()
        remainder = raw[2:].lstrip("\\/")
        raw = f"/mnt/{drive}/{remainder}"

    # Unify separators so "outputs\TSViT_MODEL" works
    raw = raw.replace("\\", "/")

    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path

    return path.expanduser()

from src.models.rnn.gru.dfzq_gru.get_configs import DFZQGRUConfig
from src.models.rnn.gru.dfzq_gru.dfzq_gru import DFZQGRU
from src.models.transformer.tsvit.tsvit import TSViT
from src.dataloader.DataLoader import get_train_valid_test_loaders, get_dataloader
from src.dataloader.DuckwideDataloader import get_duckwide_dataloader
from src.utils.experiment_utils import load_experiment_config
from .factor_saver import save_factor_multi_format
from .factor_converter import convert
from .align import align_wide_to_schema
from .config_utils import (
    resolve_experiment_and_schema,
    detect_dataset_last_date,
    build_fetch_cfg,
    align_df_to_factor_order,
)
from src.data_service.pipelines.Dataset_builder.maintenance import DatasetMaintenance


class DirectLagDataset:
    """
    直接处理 lag 特征的 Dataset 类（Windows 多进程兼容版本）。
    严格按照 ParquetPVDataset._apply_feature_selection 的逻辑确定特征顺序。
    """
    def __init__(self, df, seq_len, selected_factors, dataset_feature_order=None):
        import torch
        self.df = df.copy()
        self.seq_len = seq_len
        
        # 关键修复：完全按照原始 dataset 的逻辑确定特征顺序
        self.factor_names = self._get_dataset_compatible_feature_order(df, selected_factors, dataset_feature_order)
        
        # 构建lag映射
        lag_cols = [col for col in df.columns if '_lag_' in col]
        self.factor_lag_map = {}
        
        for col in lag_cols:
            parts = col.split('_lag_')
            if len(parts) == 2:
                factor_name = parts[0]
                lag_num = int(parts[1])
                
                if factor_name not in self.factor_lag_map:
                    self.factor_lag_map[factor_name] = {}
                self.factor_lag_map[factor_name][lag_num] = col
        
        print(f"   DirectLag Dataset: {len(self.factor_names)} 个特征，{len(df)} 个样本")
        print(f"   特征顺序: {self.factor_names[:5]}{'...' if len(self.factor_names) > 5 else ''}")
        
        # 调试信息
        if len(self.factor_names) == 0:
            print(f"   调试信息：")
            print(f"      - 总lag列数: {len(lag_cols)}")
            print(f"      - lag列示例: {lag_cols[:5] if lag_cols else '无'}")
            print(f"      - selected_factors: {selected_factors}")
            print(f"      - factor_lag_map keys: {list(self.factor_lag_map.keys())[:5]}")
            
            # 分析不匹配原因
            if lag_cols and selected_factors:
                available_factors = set()
                for col in lag_cols:
                    parts = col.split('_lag_')
                    if len(parts) == 2:
                        available_factors.add(parts[0])
                
                print(f"      DB补齐可用因子: {sorted(list(available_factors))[:10]}")
                print(f"      Selected因子: {selected_factors[:10]}")
                
                # 检查overlap
                overlap = set(selected_factors) & available_factors
                print(f"      重叠因子: {len(overlap)} / {len(selected_factors)}")
        
        # 🎯 最终一致性验证：与dataset段对比
        if dataset_feature_order and len(self.factor_names) > 0:
            print(f"   一致性验证：DB补齐 vs Dataset段")
            
            if self.factor_names == dataset_feature_order[:len(self.factor_names)]:
                print(f"   特征顺序完全一致")
            else:
                print(f"   特征顺序不一致:")
                print(f"      Dataset段: {dataset_feature_order[:5]}")
                print(f"      DB补齐段: {self.factor_names[:5]}")
                print(f"   这可能导致模型推理错误，请检查特征匹配逻辑")
    
    def _get_dataset_compatible_feature_order(self, df, selected_factors, dataset_feature_order=None):
        """
        获取与 dataset 完全一致的特征顺序。
        优先使用 dataset 段的实际顺序作为标准。
        """
        # 🎯 如果提供了dataset段的实际特征顺序，直接使用它（最可靠）
        if dataset_feature_order:
            print("   使用 dataset 段的标准特征顺序（不筛掉缺失因子，缺失用 0 填充）")
            # 仅日志可观测
            avail = {c.split('_lag_')[0] for c in df.columns if '_lag_' in c}
            miss = [f for f in dataset_feature_order if f not in avail]
            if miss:
                print(f"      DB补齐缺少 {len(miss)} 个因子，将以 0 填充：{miss[:5]}{'...' if len(miss)>5 else ''}")
            return list(dataset_feature_order)
        
        # 🔧 备用方案：模拟原始dataset的特征选择逻辑
        print(f"   备用方案：模拟 ParquetPVDataset._apply_feature_selection 逻辑")
        
        # 获取所有可能的特征列（模拟all_feature_cols）
        all_available_cols = [col for col in df.columns if '_lag_' in col]
        all_available_cols.sort()  # 按名称排序，模拟原始dataset的固定顺序
        
        if not selected_factors:
            # 没有特征选择时，返回所有特征（按字母顺序）
            unique_factors = set()
            for col in all_available_cols:
                parts = col.split('_lag_')
                if len(parts) == 2:
                    unique_factors.add(parts[0])
            return sorted(list(unique_factors))
        
        # 🎯 严格按照ParquetPVDataset._apply_feature_selection的逻辑
        selected_feature_cols = []
        
        for factor_name in selected_factors:  # 严格按selected_factors顺序！
            # 完全模拟原始逻辑：col.startswith(f"{factor_name}_")
            factor_cols = [col for col in all_available_cols if col.startswith(f"{factor_name}_")]
            if not factor_cols:
                # 尝试不同的匹配模式（模拟原始逻辑）
                factor_cols = [col for col in all_available_cols if factor_name in col]
            
            if factor_cols:
                selected_feature_cols.extend(factor_cols)
                print(f"      {factor_name}: 找到 {len(factor_cols)} 列")
            else:
                print(f"      {factor_name}: 未找到匹配的列")
        
        # 从选中的列中提取唯一的因子名，保持顺序
        unique_factors = []
        seen = set()
        for col in selected_feature_cols:
            parts = col.split('_lag_')
            if len(parts) == 2:
                factor_name = parts[0]
                if factor_name not in seen:
                    unique_factors.append(factor_name)
                    seen.add(factor_name)
        
        print(f"   模拟 dataset 特征选择：{len(selected_factors)} 个输入 → {len(unique_factors)} 个输出")
        return unique_factors
    
    def __len__(self):
        return len(self.df)
    
    def __getitem__(self, idx):
        import torch
        row = self.df.iloc[idx]
        
        # 重组时间序列：[seq_len, n_features]
        sequences = []
        for t in range(self.seq_len):
            lag_num = self.seq_len - 1 - t  # lag_29(t=0) -> lag_0(t=29)
            time_step_features = []
            
            for factor_name in self.factor_names:
                if factor_name in self.factor_lag_map and lag_num in self.factor_lag_map[factor_name]:
                    col_name = self.factor_lag_map[factor_name][lag_num]
                    time_step_features.append(row[col_name])
                else:
                    time_step_features.append(0.0)  # 缺失值填充
            
            sequences.append(time_step_features)
        
        features = torch.tensor(sequences, dtype=torch.float32)  # [seq_len, n_features]
        label = torch.tensor(0.0, dtype=torch.float32)  # dummy label
        date = str(row['trade_date'])
        code = row['stock_code']
        
        return features, label, date, code


class FactorGenerator:
    """
    因子生成器 - 负责：
    1) 自动探测 dataset_path  
    2) 加载 GRU 模型  
    3) 按时间窗口跑推理 → 得到 df_pred  
    4) 把 df_pred 转成回测所需 df_factor
    """
    
    def __init__(self, cfg):
        self.cfg = cfg
        self.model = None
        self.df_factor: Optional[pd.DataFrame] = None
        self.cfg.model_path = str(_normalize_path(self.cfg.model_path))
        
        # 设置结果保存路径
        if self.cfg.backtest_result_path is None:
            self.cfg.backtest_result_path = Path(self.cfg.model_path) / "bt_results"
        else:
            self.cfg.backtest_result_path = _normalize_path(self.cfg.backtest_result_path)

        self.cfg.backtest_result_path = str(self.cfg.backtest_result_path)
        
        # 确保结果目录存在
        os.makedirs(self.cfg.backtest_result_path, exist_ok=True)
    
    def run(self) -> pd.DataFrame:
        """主入口：运行因子生成流程"""
        print("开始因子生成流程...")
        
        # 1. 自动填充数据集路径
        self._auto_fill_dataset_path()
        
        # 2. 加载模型
        self._load_model()
        
        # 3. 推理并转换格式
        self.df_factor = self._infer_and_convert()
        
        print(f"✅ 因子生成完成，共 {len(self.df_factor)} 条记录")
        return self.df_factor
    
    def _auto_fill_dataset_path(self):
        """从experiment_config.json中自动读取dataset_path"""
        if self.cfg.dataset_path is not None:
            self.cfg.dataset_path = str(_normalize_path(self.cfg.dataset_path))
            print(f"📋 使用配置中的dataset_path: {self.cfg.dataset_path}")
            if not os.path.exists(self.cfg.dataset_path):
                raise FileNotFoundError(f"指定的dataset_path不存在: {self.cfg.dataset_path}")
            return
            
        print("🔍 从experiment_config.json中自动读取dataset_path...")
        
        try:
            # 加载实验配置
            experiment_config = load_experiment_config(self.cfg.model_path)
            
            if experiment_config is None:
                raise FileNotFoundError(f"在模型路径 {self.cfg.model_path} 中未找到 experiment_config.json 文件")
            
            # 尝试从多个可能的位置读取dataset_path
            dataset_path = None
            
            # 1. 优先从training_config中读取
            if 'training_config' in experiment_config:
                training_config = experiment_config['training_config']
                dataset_path = training_config.get('dataset_path')
                if dataset_path:
                    print(f"   📋 从training_config中找到dataset_path: {dataset_path}")
            
            # 2. Ensure dataset coverage matches backtest window
            if not dataset_path and 'experiment_info' in experiment_config:
                experiment_info = experiment_config['experiment_info']
                dataset_path = experiment_info.get('dataset_path')
                if dataset_path:
                    print(f"   📋 从experiment_info中找到dataset_path: {dataset_path}")
            
            # 3. 如果都没找到，报错
            if not dataset_path:
                available_keys = []
                if 'training_config' in experiment_config:
                    available_keys.extend([f"training_config.{k}" for k in experiment_config['training_config'].keys()])
                if 'experiment_info' in experiment_config:
                    available_keys.extend([f"experiment_info.{k}" for k in experiment_config['experiment_info'].keys()])
                
                raise ValueError(
                    f"在experiment_config.json中未找到dataset_path配置。\n"
                    f"检查的位置：training_config.dataset_path, experiment_info.dataset_path\n"
                    f"可用的配置键：{available_keys}"
                )
            
            # 4. 验证数据集路径是否存在
            dataset_path = _normalize_path(dataset_path)
            if not os.path.exists(dataset_path):
                raise FileNotFoundError(
                    f"从experiment_config.json中读取的dataset_path不存在: {dataset_path}\n"
                    f"请检查路径是否正确或数据集是否已准备好。"
                )
            
            # 5. 设置到配置中
            self.cfg.dataset_path = str(dataset_path)
            print(f"✅ 成功从experiment_config.json中读取dataset_path: {self.cfg.dataset_path}")
            
        except Exception as e:
            print(f"❌ 从experiment_config.json中读取dataset_path失败: {str(e)}")
            raise e
    
    def _detect_input_size_from_data(self):
        """从数据集中自动检测特征维度"""
        print("自动检测数据集特征维度...")
        
        try:
            # 创建临时数据加载器来检测维度
            dl_cfg = {
                "dataset_path": self.cfg.dataset_path,
                "batch_size": 32,  # 小批次用于检测
                "num_workers": 1,
                "shuffle": False,
                "seed": 42,
                "chunk_size": 65536,
                "memory_limit": "8GB",
                "use_fixed_indices": True,
                "prefetch_factor": 2,
                "use_custom_splits": True,
                "date_ranges": {
                    "train": ("20080101", "20181231"),
                    "valid": ("20190101", "20211231"),
                    "test": ("20220101", "20241231"),
                },
                "selected_factors": None,  # 使用全部特征
            }
            
            # 只需要训练集来检测维度
            train_loader, _, _ = get_train_valid_test_loaders(
                dl_cfg, 
                keep_meta_train=False,
                keep_meta_eval=False,
                max_samples_train=1000,  # 限制样本数量，只为检测维度
                max_samples_valid=100,
                max_samples_test=100,
                use_fixed_indices=True
            )
            
            # 从第一个batch检测特征维度
            sample_batch = next(iter(train_loader))
            if len(sample_batch) >= 2:
                sample_feats = sample_batch[0]  # [batch_size, seq_len, feature_dim]
                detected_input_size = sample_feats.shape[-1]  # 获取最后一维的大小
                print(f"自动检测到数据集特征维度: {detected_input_size}")
                print(f"数据形状: {sample_feats.shape}")
                return detected_input_size
            else:
                raise ValueError("数据加载器返回的batch格式不正确")
                
        except Exception as e:
            print(f"自动检测特征维度失败: {str(e)}")
            print("将尝试从checkpoint中获取模型配置...")
            return None

    def _load_model(self):
        """加载训练好的模型"""
        print("正在加载模型...")
        
        try:
            # 1. 🚀 优先尝试从保存的实验配置加载
            experiment_config = load_experiment_config(self.cfg.model_path)
            
            if experiment_config:
                print("找到保存的实验配置文件")
                model_params = experiment_config.get('model_config', {})
                
                # 从保存的配置创建模型配置
                model_config = DFZQGRUConfig()
                
                # 设置所有模型参数（优先使用保存的配置）
                for attr in ['input_size', 'hidden_size', 'num_layers', 'dropout', 'output_size', 
                            'bidirectional', 'attention', 'input_hidden_dim', 'head_hidden_dim']:
                    if attr in model_params:
                        setattr(model_config, attr, model_params[attr])
                        
                print(f"从实验配置加载模型参数:")
                print(f"- input_size: {model_config.input_size}")
                print(f"- hidden_size: {model_config.hidden_size}")
                print(f"- num_layers: {model_config.num_layers}")
                if 'actual_features' in model_params:
                    print(f"- selected_factors: {len(model_params['actual_features'])} 个特征")
                
            else:
                print("未找到实验配置文件，使用传统方法...")
                
                # 2. 备用方案：尝试自动检测数据集特征维度
                detected_input_size = self._detect_input_size_from_data()
                
                # 3. 从checkpoint加载配置
                model_config = DFZQGRUConfig()
                
                ckpt_path = os.path.join(self.cfg.model_path, 'ckpt')
                ckpt_files = [f for f in os.listdir(ckpt_path) if f.endswith('.pth')]
                
                if ckpt_files:
                    latest_ckpt = max(ckpt_files)
                    ckpt_full_path = os.path.join(ckpt_path, latest_ckpt)
                    checkpoint = torch.load(ckpt_full_path, map_location='cpu', weights_only=False)
                    
                    # 从checkpoint中获取训练配置（如果有的话）
                    if 'training_config' in checkpoint:
                        training_cfg = checkpoint['training_config']
                        print(f"从 checkpoint 加载训练配置")
                        
                        # 使用checkpoint中的配置
                        if 'input_size' in training_cfg and training_cfg['input_size'] is not None:
                            model_config.input_size = training_cfg['input_size']
                            print(f"使用 checkpoint 中的 input_size: {model_config.input_size}")
                        elif detected_input_size is not None:
                            model_config.input_size = detected_input_size
                            print(f"使用自动检测的 input_size: {model_config.input_size}")
                        else:
                            print("无法确定 input_size，使用默认值")
                            
                        # 设置其他模型参数
                        for attr in ['hidden_size', 'num_layers', 'dropout', 'output_size', 
                                    'bidirectional', 'attention', 'input_hidden_dim', 'head_hidden_dim']:
                            if attr in training_cfg:
                                setattr(model_config, attr, training_cfg[attr])
                    else:
                        # 没有训练配置，使用检测到的维度
                        if detected_input_size is not None:
                            model_config.input_size = detected_input_size
                            print(f"使用自动检测的 input_size: {model_config.input_size}")
                        else:
                            print("无法确定 input_size，使用默认值")
            
            # 4. 解析权重文件并自动判断模型类型（GRU vs TSViT）
            ckpt_path = os.path.join(self.cfg.model_path, 'ckpt')
            ckpt_files = [f for f in os.listdir(ckpt_path) if f.endswith('.pth')]
            if not ckpt_files:
                raise FileNotFoundError(f"在 {ckpt_path} 中未找到模型权重文件")
            latest_ckpt = max(ckpt_files)
            ckpt_full_path = os.path.join(ckpt_path, latest_ckpt)

            checkpoint = torch.load(ckpt_full_path, map_location='cpu', weights_only=False)
            state_dict = checkpoint.get('model_state_dict', {})

            # 通过权重key判断是否为TSViT
            state_keys = list(state_dict.keys())
            is_tsvit = any(k.startswith('patch_embed') or k.startswith('encoder.') or k.startswith('ln_out') for k in state_keys)

            if is_tsvit:
                # 使用保存的训练配置恢复TSViT参数
                training_cfg = experiment_config.get('training_config', {}) if experiment_config else {}

                def g(name, default=None):
                    return training_cfg.get(name, default)

                tsvit_params = {
                    'T': g('T'),
                    'D': g('D'),
                    'lead': g('lead'),
                    'P': g('P'),
                    'S': g('S'),
                    'patch_mode': g('patch_mode'),
                    'hidden_size': g('hidden_size'),
                    'dt': g('dt'),
                    'share_timeproj': g('share_timeproj'),
                    'ms_branches': g('ms_branches'),
                    'pos_encoding': g('pos_encoding'),
                    'rope_pct': g('rope_pct'),
                    'rope_theta': g('rope_theta'),
                    'rpb_max_dist': g('rpb_max_dist'),
                    'pos_dropout': g('pos_dropout'),
                    'nheads': g('nheads'),
                    'num_layers': g('num_layers'),
                    'ffn_mult': g('ffn_mult'),
                    'dropout': g('dropout'),
                    'attn_dropout': g('attn_dropout'),
                    'norm_first': g('norm_first'),
                    'drop_path_rate': g('drop_path_rate'),
                    'encoder_impl': g('encoder_impl'),
                    'use_cls': g('use_cls'),
                    'head_type': g('head_type'),
                    'token_drop_p': g('token_drop_p'),
                    'fv_bn': g('fv_bn', True),
                }

                # 实例化TSViT
                self.model = TSViT(**tsvit_params)
                self.model.load_state_dict(state_dict, strict=False)
                self.model.eval()
                print(f"成功加载 TSViT 模型: {latest_ckpt}")
                print(f"TSViT 配置: Dh={tsvit_params.get('hidden_size')}, L={tsvit_params.get('num_layers')}, H={tsvit_params.get('nheads')}")
            else:
                # 回退到GRU模型加载
                self.model = DFZQGRU(model_config)
                self.model.load_state_dict(state_dict)
                self.model.eval()
                print(f"成功加载 GRU 模型: {latest_ckpt}")
                print(f"最终模型配置: input_size={model_config.input_size}, hidden_size={model_config.hidden_size}")
            
        except Exception as e:
            print(f"模型加载失败: {str(e)}")
            raise e

    def _infer_and_convert(self) -> pd.DataFrame:
        """使用模型推理生成因子预测，并转换为目标格式"""
        try:
            # 1. 模型推理生成预测
            df_pred = self._generate_model_predictions()
            
            if df_pred is None or len(df_pred) == 0:
                raise RuntimeError("模型推理失败，未生成任何因子数据")
            
            # 2. 转换为目标格式（使用可插拔的转换器）
            target_format = getattr(self.cfg, "factor_target_format", "backtest")
            return convert(
                df_pred,
                target=target_format,
                cfg=self.cfg
            )
                
        except Exception as e:
            print(f"因子推理和转换失败: {str(e)}")
            raise e
    
    def _generate_model_predictions(self):
        """使用模型推理生成因子预测 - 支持dataset段+DB补齐段的混合推理"""
        print("开始模型推理生成因子...")
        
        try:
            # 1. 从实验配置中加载数据集配置
            # 统一从 experiment/schema 解析配置
            resolved = resolve_experiment_and_schema(self.cfg.model_path, fallback_dataset_path=self.cfg.dataset_path)
            self.cfg.dataset_path = resolved.dataset_path
            selected_factors = resolved.selected_factors
            self.cfg.seq_len = resolved.seq_len
            print("解析 experiment/schema 完成，已载入特征与表配置")
            print(f"序列长度(seq_len): {resolved.seq_len}")

            # 从训练时保存的配置中读取与数据加载相关的参数，保持与训练一致
            try:
                exp_cfg = load_experiment_config(self.cfg.model_path)
                training_cfg = exp_cfg.get('training_config', {}) if exp_cfg else {}
            except Exception:
                training_cfg = {}
            
            # 2. Ensure dataset coverage matches backtest window
            backtest_start = self.cfg.start_date  # YYYYMMDD format
            backtest_end = self.cfg.end_date      # YYYYMMDD format
            print(f"[Maintenance] Backtest date range: {backtest_start} - {backtest_end}")

            maintenance = DatasetMaintenance(self.cfg.dataset_path)
            coverage_report = maintenance.ensure_coverage(
                backtest_start,
                backtest_end,
                auto_extend=getattr(self.cfg, "auto_extend_dataset", True),
                threads=getattr(self.cfg, "dataset_extend_threads", None),
            )

            if coverage_report.dataset_end:
                print(f"[Maintenance] Dataset coverage end date: {coverage_report.dataset_end}")
            else:
                print("[Maintenance] Unable to detect dataset coverage end date")

            if coverage_report.extended:
                print("[Maintenance] Dataset gap filled by automatic extension")
            elif not coverage_report.is_satisfied:
                print(f"[Maintenance] Dataset still missing ranges: {coverage_report.missing_ranges}")

            ds_last = coverage_report.dataset_end or detect_dataset_last_date(self.cfg.dataset_path)

            # ȷ�����ݼ���������
            ds_end = ds_last if ds_last and ds_last >= backtest_start else None
            
            # 3. 设置设备和模型
            device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
            self.model.to(device)
            self.model.eval()
            
            # 4. 初始化预测结果列表
            dfs_pred = []
            dataset_feature_order = None  # 用于存储dataset段的实际特征顺序
            
            # ----------------------------------------
            # 4-A  dataset 段推理 (start → ds_end)
            # ----------------------------------------
            if ds_end and ds_end >= backtest_start:
                effective_end = min(ds_end, backtest_end)
                print(f"【阶段A】dataset 推理 {backtest_start} → {effective_end}")
                
                # 创建数据加载器配置（与训练保持一致，避免过大batch导致CUDA kernel配置错误）
                dl_cfg = {
                    "dataset_path": self.cfg.dataset_path,
                    "batch_size": 2048,
                    "num_workers": int(training_cfg.get("num_workers", 1)),
                    "shuffle": False,
                    "seed": int(training_cfg.get("seed", 42)),
                    # 关键：使用训练阶段的 chunk_size（YAML 为 8192）
                    "chunk_size": int(training_cfg.get("chunk_size", 8192)),
                    "memory_limit": training_cfg.get("memory_limit", "8GB"),
                    "use_fixed_indices": bool(training_cfg.get("use_fixed_indices", True)),
                    "prefetch_factor": int(training_cfg.get("prefetch_factor", 2)),
                    "pin_memory": bool(training_cfg.get("pin_memory", True)),
                    "persistent_workers": bool(training_cfg.get("persistent_workers", True)),
                    "selected_factors": selected_factors,
                }
                extra_keys = [
                    "dataset_impl",
                    "seq_len",
                    "days_per_fetch",
                    "part_pad",
                    "duck_threads",
                    "duck_memory",
                    "duck_cache",
                    "duck_max_temp",
                    "duck_temp_dir",
                    "duck_materialize",
                    "duck_persist_conn",
                    "persist_connection",
                    "duck_temp_directory",
                    "batch_by",
                ]
                for key in extra_keys:
                    if key in training_cfg and training_cfg[key] is not None:
                        dl_cfg[key] = training_cfg[key]
                dl_cfg.setdefault("seq_len", int(self.cfg.seq_len))
                
                loader_ds = self._get_backtest_dataloader(dl_cfg, backtest_start, effective_end)
                print(f"   Dataset 加载器创建完成，共 {len(loader_ds)} 个 batch")
                
                # 🎯 关键：从dataset段获取实际的特征顺序作为标准
                dataset_feature_order = self._extract_dataset_feature_order(loader_ds, selected_factors)
                
                df_pred_ds = self._infer_loader(loader_ds, device)
                # 添加来源标记
                df_pred_ds['source'] = 'dataset'
                dfs_pred.append(df_pred_ds)
                print(f"   Dataset 段推理完成: {len(df_pred_ds)} 条记录")
            
            # ----------------------------------------
            # 4-B  DB 补齐段推理 (ds_end+1 → backtest_end)
            # ----------------------------------------
            need_db_fill = (not ds_end) or (ds_end < backtest_end)
            
            if need_db_fill:
                # 计算补齐开始时间
                if ds_end:
                    fill_start = (pd.Timestamp(ds_end) + pd.Timedelta(days=1)).strftime("%Y%m%d")
                else:
                    fill_start = backtest_start
                
                print(f"【阶段B】DB 补齐推理 {fill_start} → {backtest_end}")
                
                # 从数据库获取补齐数据
                from src.data_service.pipelines.factor_utils.db_fetcher import fetch_wide_lag, LagChunkStream
                
                # 准备DB补齐配置 (从cfg中读取，如果cfg没有则使用默认值)
                fetch_cfg = build_fetch_cfg(resolved, align_to_dataset=True)
                if hasattr(self.cfg, 'fetch') and getattr(self.cfg, 'fetch') is not None:
                    override = getattr(self.cfg, 'fetch')
                    for k in [
                        'restricted_table',
                        'features_tables',
                        'stats_table',
                        'clip_std',
                        'factor_based_nan_handling',
                        'consecutive_nan_threshold',
                        'code_prefix_blacklist',
                        'code_blacklist',
                        'seq_len',
                        'max_factors_per_batch',
                    ]:
                        if hasattr(override, k):
                            v = getattr(override, k)
                            if v is not None:
                                setattr(fetch_cfg, k, v)
                print("   使用解析后的 DB 补齐参数（与 dataset 对齐）")
                
                try:
                    # 获取宽格式+滞后数据
                    chunk_size = getattr(self.cfg, 'db_chunk_stock_size', 25)
                    wide_lag = fetch_wide_lag(
                        fill_start,
                        backtest_end,
                        selected_factors=selected_factors,
                        windows=None,
                        cfg=fetch_cfg,
                        align_to_schema=self.cfg.dataset_path,
                        dataset_path_for_order=self.cfg.dataset_path,
                        dataset_feature_order=dataset_feature_order,
                        chunk_size=chunk_size,
                        code_prefix_blacklist=getattr(fetch_cfg, 'code_prefix_blacklist', ['9']),
                        code_blacklist=getattr(fetch_cfg, 'code_blacklist', []),
                    )

                    if isinstance(wide_lag, LagChunkStream):
                        print(f"   DB 补齐分块流模式，股票分块大小: {wide_lag.chunk_size}")
                        df_pred_db = self._infer_chunk_stream(
                            wide_lag,
                            device,
                            selected_factors=selected_factors,
                            dataset_feature_order=dataset_feature_order,
                        )
                        if df_pred_db is not None and not df_pred_db.empty:
                            # 添加来源标记
                            df_pred_db['source'] = 'db'
                            dfs_pred.append(df_pred_db)
                            print(f"   DB 补齐分块流累计记录数: {len(df_pred_db)}")
                        else:
                            print("   DB 补齐分块流未产生有效结果")
                    else:
                        if isinstance(wide_lag, pd.DataFrame) and wide_lag.empty:
                            print("   DB 补齐返回空 DataFrame")
                        else:
                            if isinstance(wide_lag, pd.DataFrame):
                                print(f"   DB 补齐宽表形状: {wide_lag.shape}")

                                db_dataset = self._create_direct_lag_dataset(
                                    wide_lag,
                                    seq_len=fetch_cfg.seq_len,
                                    selected_factors=selected_factors,
                                    dataset_feature_order=dataset_feature_order,
                                )

                                if db_dataset is None:
                                    print("   创建 DirectLag Dataset 失败，跳过 DB 补齐段")
                                else:
                                    import torch.utils.data as data_utils

                                    db_loader = data_utils.DataLoader(
                                        db_dataset,
                                        batch_size=2048,
                                        shuffle=False,
                                        num_workers=0,
                                        pin_memory=False,
                                    )

                                    print(f"   DB 补齐 DataLoader 批次数: {len(db_loader)}")

                                    df_pred_db = self._infer_loader(db_loader, device)
                                    # 添加来源标记
                                    df_pred_db['source'] = 'db'
                                    dfs_pred.append(df_pred_db)
                                    print(f"   DB 补齐段推理完成: {len(df_pred_db)} 条记录")
                except Exception as e:
                    print(f"   DB 补齐推理失败: {str(e)}")
                    print("   将只使用 dataset 段的结果")
                    import traceback
                    traceback.print_exc()
            
            # 5. 合并所有推理结果
            if not dfs_pred:
                raise RuntimeError("没有任何推理结果，请检查数据范围设置")
            
            df_pred = pd.concat(dfs_pred, ignore_index=True)
            
            # 5.5. DB 段 NaN 填充（只对 DB 补齐段进行前向填充）
            if 'source' in df_pred.columns:
                df_pred = df_pred.sort_values(['stock_code', 'trade_date'])
                db_mask = df_pred['source'] == 'db'
                nan_mask = df_pred['model_pred'].isna()
                fill_mask = db_mask & nan_mask
                
                if fill_mask.any():
                    filled_series = df_pred.groupby('stock_code')['model_pred'].ffill()
                    df_pred.loc[fill_mask, 'model_pred'] = filled_series[fill_mask]
                    # 剩余 NaN 填充为 0
                    remaining_nan = df_pred['model_pred'].isna()
                    if remaining_nan.any():
                        df_pred.loc[remaining_nan, 'model_pred'] = 0.0
                    print(f"   DB 段 NaN 填充: {fill_mask.sum()} 个位置被前向填充")
                
                # 去掉来源标记列
                df_pred = df_pred.drop(columns=['source'])
            
            # 6. 按时间排序并去重
            df_pred = df_pred.sort_values(['trade_date', 'stock_code']).drop_duplicates(
                subset=['trade_date', 'stock_code'], keep='last'
            ).reset_index(drop=True)
            
            print(f"混合推理完成")
            print(f"- 总记录数: {len(df_pred)}")
            print(f"- 时间范围: {df_pred['trade_date'].min()} 至 {df_pred['trade_date'].max()}")
            print(f"- 股票数量: {df_pred['stock_code'].nunique()}")
            print(f"- 预测值范围: {df_pred['model_pred'].min():.4f} 至 {df_pred['model_pred'].max():.4f}")
            
            # 7. 🚀 保存因子文件（使用独立的保存器，支持多格式）
            if hasattr(self.cfg, 'enable_factor_save') and self.cfg.enable_factor_save:
                save_result = save_factor_multi_format(
                    df_pred,
                    self.cfg,
                    self.cfg.backtest_result_path,
                    formats=getattr(self.cfg, 'factor_save_formats', ['parquet']),
                )
                pairs = [f"{k}: {v.get('status', 'unknown')}" for k, v in save_result.items()]
                print("   因子保存结果:", pairs)
            else:
                print("   跳过因子保存（enable_factor_save=False 或未配置）")
            
            return df_pred
            
        except Exception as e:
            print(f"模型推理失败: {str(e)}")
            import traceback
            traceback.print_exc()
            raise e

    def _extract_dataset_feature_order(self, loader, selected_factors):
        """
        🎯 从dataset段的实际DataLoader中提取正确的特征顺序
        这是确保一致性的黄金标准
        """
        try:
            print(f"   提取 dataset 段的实际特征顺序...")
            
            # 从dataset对象获取feature信息
            if hasattr(loader.dataset, 'dataset'):  # BatchDataset的情况
                dataset = loader.dataset.dataset
            else:
                dataset = loader.dataset
            
            if hasattr(dataset, 'get_selected_factors'):
                actual_factors = dataset.get_selected_factors()
                print(f"   从 dataset 获取实际特征顺序: {len(actual_factors)} 个特征")
                print(f"   前5个特征: {actual_factors[:5]}")
                return actual_factors
            else:
                print(f"   dataset 对象没有 get_selected_factors 方法，使用 selected_factors")
                return selected_factors
                
        except Exception as e:
            print(f"   提取 dataset 特征顺序失败: {str(e)}")
            print(f"   回退到使用 selected_factors: {selected_factors}")
            return selected_factors

    def _create_direct_lag_dataset(self, wide_lag: pd.DataFrame, seq_len: int, selected_factors: List[str] = None, dataset_feature_order: List[str] = None):
        """
        直接从lag特征创建Dataset，避免使用WideInferDataset的二次序列构建
        
        Args:
            wide_lag: 包含lag特征的宽表，每行已包含完整的时间序列信息
            seq_len: 序列长度
            selected_factors: 选择的原始因子列表
            dataset_feature_order: dataset段的实际特征顺序（黄金标准）
            
        Returns:
            DirectLagDataset: 直接处理lag特征的Dataset
        """
        try:
            print(f"   创建直接 lag dataset：{wide_lag.shape}")
            
            # 🎯 使用dataset段的特征顺序作为标准（如果提供）
            reference_order = dataset_feature_order if dataset_feature_order else selected_factors
            
            if dataset_feature_order:
                print(f"   使用 dataset 段的标准特征顺序: {len(dataset_feature_order)} 个特征")
            else:
                print(f"   使用 selected_factors 顺序: {len(selected_factors) if selected_factors else 0} 个特征")
            
            # 先按 dataset schema 对齐列集合与顺序
            try:
                wide_lag = align_wide_to_schema(
                    wide_lag, 
                    self.cfg.dataset_path,
                    fallback_order=reference_order,
                    seq_len=seq_len
                )
                print("   已按 dataset schema 对齐列集合与顺序")
            except Exception as ex:
                print(f"   schema 对齐失败，继续使用原列集合：{ex}")

            # 使用模块级别的DirectLagDataset类，支持Windows多进程pickle
            return DirectLagDataset(wide_lag, seq_len, reference_order, dataset_feature_order)
            
        except Exception as e:
            print(f"   创建 DirectLag Dataset 失败: {str(e)}")
            import traceback
            traceback.print_exc()
            return None

    def _detect_dataset_last_date(self) -> str | None:
        """
        检测dataset的最后日期
        读取 meta/splits.parquet 或 shards 文件名，返回 YYYYMMDD 字符串
        """
        try:
            import pyarrow.parquet as pq
            from pathlib import Path
            
            dataset_path = Path(self.cfg.dataset_path)
            
            # 方法1: 从 meta/splits.parquet 读取
            splits_file = dataset_path / "meta" / "splits.parquet"
            if splits_file.exists():
                try:
                    df_splits = pq.read_table(splits_file).to_pandas()
                    if 'trade_date' in df_splits.columns:
                        last_date = df_splits['trade_date'].max()
                        if pd.notna(last_date):
                            # 转换为YYYYMMDD格式
                            if isinstance(last_date, str):
                                return last_date.replace('-', '')
                            else:
                                return pd.to_datetime(last_date).strftime('%Y%m%d')
                except Exception as e:
                    print(f"   读取 splits.parquet 失败: {e}")
            
            # 方法2: 从 shards 文件名推断（备用方案）
            shards_dir = dataset_path / "shards"
            if shards_dir.exists():
                try:
                    parquet_files = list(shards_dir.rglob("*.parquet"))
                    if parquet_files:
                        # 从第一个文件读取最大日期
                        sample_file = parquet_files[0]
                        df_sample = pq.read_table(sample_file).to_pandas()
                        if 'trade_date' in df_sample.columns:
                            last_date = df_sample['trade_date'].max()
                            if pd.notna(last_date):
                                if isinstance(last_date, str):
                                    return last_date.replace('-', '')
                                else:
                                    return pd.to_datetime(last_date).strftime('%Y%m%d')
                except Exception as e:
                    print(f"   从 shards 文件推断日期失败: {e}")
            
            print("   无法检测到 dataset 的最后日期")
            return None
            
        except Exception as e:
            print(f"   检测 dataset 最后日期失败: {e}")
            return None
    
    def _infer_loader(self, loader, device):
        """共用的推理循环，返回 df_pred"""
        print(f"   开始推理，共 {len(loader)} 个 batch...")
        
        all_dates = []
        all_codes = []
        all_preds = []
        
        with torch.no_grad():
            for batch_idx, batch_data in enumerate(tqdm(loader, desc='推理进度')):
                # 期望4个元素：feats, labels, dates, codes
                if isinstance(batch_data, (tuple, list)) and len(batch_data) >= 4:
                    feats, labels, dates, codes = batch_data[:4]
                else:
                    print(f"⚠️ Batch {batch_idx} 数据格式不正确，跳过")
                    continue
                
                # 数据预处理和推理
                if len(feats.shape) == 3:
                    feats = feats.to(device).float()
                    if batch_idx == 0:
                        print(f"   数据形状: {feats.shape}")
                else:
                    print(f"意外的 feats 形状: {feats.shape}")
                    continue
                
                # 为避免单批过大触发 CUDA kernel 配置错误，这里做安全微批推理
                max_infer_bs = 16384 if device.type == 'cuda' else feats.size(0)
                if feats.size(0) > max_infer_bs:
                    batch_preds = []
                    for start in range(0, feats.size(0), max_infer_bs):
                        end = min(feats.size(0), start + max_infer_bs)
                        out = self.model(feats[start:end])
                        if isinstance(out, (tuple, list)) and len(out) >= 1:
                            preds_tensor = out[0]
                        else:
                            preds_tensor = out
                        preds_part = preds_tensor.squeeze(-1).cpu().numpy()
                        batch_preds.append(preds_part)
                        # 元数据分片对齐
                        all_dates.extend(pd.to_datetime(dates[start:end]))
                        all_codes.extend(codes[start:end])
                    preds = np.concatenate(batch_preds, axis=0)
                    all_preds.extend(preds)
                else:
                    out = self.model(feats)
                    if isinstance(out, (tuple, list)) and len(out) >= 1:
                        preds_tensor = out[0]
                    else:
                        preds_tensor = out
                    preds = preds_tensor.squeeze(-1).cpu().numpy()
                    
                    # 收集预测结果和元数据
                    all_dates.extend(pd.to_datetime(dates))
                    all_codes.extend(codes)
                    all_preds.extend(preds)
                
                # 显示进度
                if (batch_idx + 1) % 20 == 0:
                    print(f"   已处理 {batch_idx + 1}/{len(loader)} 个 batch，共 {len(all_preds)} 个样本")
        
        # 构建结果DataFrame
        df_result = pd.DataFrame({
            'trade_date': all_dates,
            'stock_code': all_codes,
            'model_pred': all_preds
        })
        
        print(f"   推理完成: {len(df_result)} 条记录")
        return df_result

    def _infer_chunk_stream(self, stream, device, selected_factors=None, dataset_feature_order=None):
        """Iterate over chunked DB supplements to avoid large memory spikes.

        Args:
            stream: LagChunkStream providing chunk DataFrames.
            device: torch device.
            selected_factors: desired factor order.
            dataset_feature_order: dataset feature order for alignment.
        """
        import torch.utils.data as data_utils
        import gc

        reference_order = dataset_feature_order or selected_factors
        if reference_order is None and hasattr(stream, 'reference_order'):
            reference_order = stream.reference_order

        dfs = []
        total_rows = 0
        chunk_idx = 0
        for chunk_df in stream.iter_chunks():
            if chunk_df is None or chunk_df.empty:
                continue
            chunk_idx += 1
            print(f"   [chunk {chunk_idx}] DB 补齐分块行数: {len(chunk_df)}")

            chunk_dataset = self._create_direct_lag_dataset(
                chunk_df,
                seq_len=stream.seq_len,
                selected_factors=reference_order,
                dataset_feature_order=dataset_feature_order,
            )

            if chunk_dataset is None or len(chunk_dataset) == 0:
                print(f"     分块 {chunk_idx} 为空或创建 Dataset 失败，跳过")
                continue

            loader = data_utils.DataLoader(
                chunk_dataset,
                batch_size=2048,
                shuffle=False,
                num_workers=0,
                pin_memory=False,
            )

            df_pred_chunk = self._infer_loader(loader, device)
            if df_pred_chunk is not None and not df_pred_chunk.empty:
                dfs.append(df_pred_chunk)
                total_rows += len(df_pred_chunk)
            gc.collect()

        if dfs:
            print(f"   DB 补齐分块流总记录数: {total_rows}")
            result_df = pd.concat(dfs, ignore_index=True)
            # 添加来源标记（在分块流中统一标记为 db）
            result_df['source'] = 'db'
            return result_df
        return pd.DataFrame(columns=['trade_date', 'stock_code', 'model_pred'])

    def _get_backtest_dataloader(self, dl_cfg, start_date, end_date):
        """创建指定日期范围的数据加载器"""
        try:
            # 设置自定义日期范围
            dl_cfg_custom = dl_cfg.copy()
            dl_cfg_custom["date_from"] = start_date
            dl_cfg_custom["date_to"] = end_date

            requested_split = getattr(self.cfg, "data_split", None)

            if requested_split:
                dl_cfg_custom["use_custom_splits"] = True
                dl_cfg_custom["date_ranges"] = {requested_split: (start_date, end_date)}
                print(
                    f"🔧 指定 split='{requested_split}'，使用 split + 日期过滤：{start_date} - {end_date}"
                )
            else:
                dl_cfg_custom["use_custom_splits"] = False
                dl_cfg_custom.pop("date_ranges", None)
                print(
                    f"🔧 未指定 split，使用全量索引按日期过滤：{start_date} - {end_date}"
                )

            dataset_path = dl_cfg_custom.get("dataset_path")
            use_duckwide = bool(
                dataset_path
                and os.path.exists(os.path.join(dataset_path, "shards", "wide_daily"))
            )

            if use_duckwide:
                dl_cfg_custom.setdefault("seq_len", int(self.cfg.seq_len))
                dl_cfg_custom.setdefault(
                    "dataset_impl", dl_cfg_custom.get("dataset_impl") or "ring"
                )
                dl_cfg_custom.setdefault(
                    "duck_threads", int(dl_cfg_custom.get("duck_threads", 8))
                )
                dl_cfg_custom.setdefault(
                    "duck_memory", dl_cfg_custom.get("duck_memory") or "8GB"
                )
                dl_cfg_custom.setdefault(
                    "duck_temp_dir",
                    dl_cfg_custom.get("duck_temp_dir")
                    or os.path.join(dataset_path, "duck_tmp"),
                )
                dl_cfg_custom.setdefault(
                    "persist_connection",
                    bool(dl_cfg_custom.get("persist_connection", True)),
                )
                try:
                    loader = get_duckwide_dataloader(
                        split=requested_split,
                        config=dl_cfg_custom,
                        keep_meta=True,
                    )
                except Exception as duck_err:
                    print(f"⚠️ Duckwide DataLoader 创建失败，回退到旧版加载器: {duck_err}")
                    use_duckwide = False

            if not use_duckwide:
                loader = get_dataloader(
                    split=requested_split or "custom",
                    config=dl_cfg_custom,
                    keep_meta=True,
                    use_fixed_indices=True,
                )

            return loader

        except Exception as e:
            print(f"❌ 创建回测数据加载器失败: {str(e)}")
            import traceback
            traceback.print_exc()
            raise e
