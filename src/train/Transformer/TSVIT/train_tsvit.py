# -*- coding: utf-8 -*-
"""
TSViT 主训练脚本 - 简洁的训练入口
"""

import logging
import torch
import numpy as np
import random
from pathlib import Path
from datetime import datetime
from src.utils.experiment_utils import (
    get_experiment_summary,
    create_experiment_dirs,
    save_experiment_config,
)

from .config import TSViTConfig
from .trainer import TSViTTrainer
from src.models.transformer.tsvit.tsvit import TSViT


def setup_logging():
    """设置日志"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )


def set_seed(seed: int):
    """设置随机种子"""
    torch.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)


def create_output_dirs_with_utils(config: TSViTConfig) -> tuple[str, dict]:
    """使用实验工具创建标准化输出目录结构并返回目录信息"""
    info = get_experiment_summary(config)
    output_dir = info['output_dir']
    dirs = create_experiment_dirs(output_dir)
    return output_dir, dirs


def detect_input_dim(train_loader) -> int:
    """自动检测输入维度"""
    sample_batch = next(iter(train_loader))
    if len(sample_batch) >= 2:
        sample_feats = sample_batch[0]  # [B, T, D]
        return sample_feats.shape[-1]
    else:
        raise ValueError("无法检测输入维度")


def detect_seq_len(train_loader) -> int:
    """自动检测序列长度T"""
    sample_batch = next(iter(train_loader))
    if len(sample_batch) >= 2:
        sample_feats = sample_batch[0]  # [B, T, D]
        return sample_feats.shape[1]
    else:
        raise ValueError("无法检测序列长度")


def run_training(config: TSViTConfig, train_loader, valid_loader, test_loader=None) -> str:
    """运行完整训练流程"""
    setup_logging()
    logger = logging.getLogger("run_tsvit")
    
    # 设置设备和种子
    device = torch.device("cuda" if torch.cuda.is_available() and not config.force_cpu else "cpu")
    set_seed(config.seed)
    logger.info(f"使用设备: {device}, 种子: {config.seed}")
    
    # 检查训练集是否为空
    if len(train_loader) == 0:
        raise RuntimeError(
            "❌ 训练集为空！请检查以下配置：\n"
            "  1. date_ranges 的日期范围是否正确\n"
            "  2. selected_factors 因子列表是否有效\n"
            "  3. indices 过滤条件（ok_factors + has_label）是否过严\n"
            "  4. 运行 'python initiate_pip_pv_dataset.py' 检查数据集状态"
        )
    
    # 自动检测输入维度与序列长度（单次取样，避免重复的首批重活）
    if (config.D is None) or (config.T is None):
        sample_batch = next(iter(train_loader))
        sample_feats = sample_batch[0]
        if config.D is None:
            config.D = sample_feats.shape[-1]
            logger.info(f"自动检测输入维度: {config.D}")
        if config.T is None:
            config.T = sample_feats.shape[1]
            logger.info(f"自动检测序列长度T: {config.T}")
    
    # 创建标准化输出目录（含 ckpt/logs/bt_results）
    output_dir, dirs = create_output_dirs_with_utils(config)
    logger.info(f"输出目录: {output_dir}")
    
    # 创建模型
    model_params = config.get_model_params()
    model = TSViT(**model_params).to(device)
    
    # 模型信息
    total_params = model.count_parameters()
    logger.info(f"模型参数量: {total_params:,} ({total_params/1e6:.2f}M)")
    
    # 创建训练器
    trainer = TSViTTrainer(model, config, device, output_dir)

    # 保存实验配置（与GRU一致的 experiment_config.json）
    try:
        cfg_path = save_experiment_config(output_dir, config)
        logger.info(f"配置已保存: {cfg_path}")
    except Exception as e:
        logger.warning(f"保存实验配置失败: {e}")
    
    # 开始训练
    logger.info("开始训练...")
    try:
        result_dir = trainer.train(train_loader, valid_loader, test_loader)
        logger.info("训练完成！")
        return result_dir
    except KeyboardInterrupt:
        logger.info("训练被用户中断")
        return output_dir
    except Exception as e:
        logger.error(f"训练失败: {e}")
        raise


if __name__ == "__main__":
    # 基础测试
    config = TSViTConfig.from_yaml("configs/models/transformer/tsvit.yaml")
    print(f"配置加载成功: {config.T}x{config.D} -> {config.Dh}")
