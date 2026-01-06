# -*- coding: utf-8 -*-
"""
日期分组模式使用示例
演示如何启用batch_by='date'实现单日batch功能
"""

from src.dataloader.DataLoader import get_train_valid_test_loaders

def example_date_grouping():
    """日期分组模式使用示例"""
    
    # 配置：启用日期分组模式
    config = {
        "dataset_path": "data/Dataset/pv_v5_pv_v5_pvhflow_solid30",
        "batch_size": 256,
        "chunk_size": 32768,
        "num_workers": 2,
        "shuffle": True,
        "seed": 42,
        
        # 🚀 关键：启用日期分组模式
        "batch_by": "date",  # 每个batch包含同一交易日的所有股票
        
        # 建议启用keep_meta获取日期信息
        "keep_meta": True,
        
        # 其他配置
        "use_fixed_indices": True,
        "duck_threads": 8,
        "duck_memory": "16GB",
        "prefetch_factor": 2,
    }
    
    print("🗓️ 创建日期分组模式的数据加载器...")
    
    # 获取数据加载器
    train_loader, valid_loader, test_loader = get_train_valid_test_loaders(
        config=config,
        keep_meta_train=True,  # 训练时保留日期元数据
        keep_meta_eval=False,
    )
    
    print("✅ 数据加载器创建成功！")
    
    # 验证日期分组功能（仅演示，实际使用时不需要）
    print("\n🔍 验证前几个batch...")
    batch_count = 0
    for batch_data in train_loader:
        if len(batch_data) == 4:  # (feats, labels, dates, codes)
            feats, labels, dates, codes = batch_data
            unique_dates = set(dates)
            
            print(f"Batch {batch_count + 1}: {feats.shape[0]} 只股票, "
                  f"日期: {list(unique_dates)}")
            
            if len(unique_dates) == 1:
                print("  ✅ 日期分组正常")
            else:
                print("  ❌ 发现跨日batch")
            
        batch_count += 1
        if batch_count >= 3:  # 只验证前3个batch
            break
    
    return train_loader, valid_loader, test_loader

def demo_training_loop(train_loader):
    """演示训练循环中的日期分组使用"""
    print("\n🚀 演示训练循环...")
    
    for batch_data in train_loader:
        if len(batch_data) == 4:  # (feats, labels, dates, codes)
            feats, labels, dates, codes = batch_data
            trade_date = dates[0]  # 单日batch，所有样本同一天
            
            print(f"  处理 {trade_date}: {feats.shape[0]} 只股票")
            
            # 在这里可以直接计算横截面指标：
            # - 横截面相关性 (IC)  
            # - 横截面Huber损失
            # - 横截面排序
            # 无需再按日期分组！
            
            break  # 只演示一个batch

if __name__ == "__main__":
    try:
        train_loader, _, _ = example_date_grouping()
        demo_training_loop(train_loader)
        
        print("\n📝 使用要点:")
        print("1. 设置 batch_by='date' 启用日期分组")
        print("2. 每个batch自动包含同一交易日的所有股票")
        print("3. 可直接计算横截面损失，与逐日IC目标一致")
        print("4. 命令行: python run_tsvit.py --batch-by date")
        
    except Exception as e:
        print(f"❌ 示例运行失败: {e}")
        print("请确保数据集路径正确且已生成索引文件")