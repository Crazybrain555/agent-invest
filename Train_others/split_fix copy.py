import pyarrow.parquet as pq
import pandas as pd
from pathlib import Path
import pyarrow as pa
import os
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

meta_dir = Path('data/Dataset/pv_v1/meta')
parquet_path = meta_dir / 'splits.parquet'
parquet_bak = meta_dir / 'splits.parquet.bak'

# 检查splits.parquet是否存在
if not parquet_path.exists():
    logging.info(f"{parquet_path} 不存在，需要创建")
    
    # 检查是否有备份文件存在
    if parquet_bak.exists():
        logging.info(f"发现备份文件 {parquet_bak}，尝试从备份恢复并修复")
        splits = pq.read_table(parquet_bak).to_pandas()
        logging.info(f"备份文件包含 {len(splits)} 行记录")
    else:
        logging.warning(f"没有找到备份文件，将创建新的splits索引")
        # 创建基本的splits索引(简化示例，实际需要根据实际数据生成)
        splits = pd.DataFrame({
            'trade_date': [],
            'stock_code': [],
            'split': []
        })
        
        # 在实际情况中，应该从shards目录中读取数据，并按照规则分配split
        # 这里只是一个简单的框架
        
        if len(splits) == 0:
            logging.error("无法创建有效的splits索引，请检查数据源")
            exit(1)

    # 确保trade_date是字符串格式的'YYYYMMDD'
    splits['trade_date'] = pd.to_datetime(splits['trade_date'], errors='coerce').dt.strftime('%Y%m%d')
    
    # 确保所有split类型(train, valid, test)都存在
    unique_splits = splits['split'].unique()
    logging.info(f"当前split类型: {unique_splits}")
    
    for required_split in ['train', 'valid', 'test']:
        if required_split not in unique_splits:
            logging.warning(f"缺少 {required_split} 分割，请确保数据完整性")
    
    # 写入修正后的splits.parquet
    os.makedirs(meta_dir, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(splits), parquet_path)
    logging.info(f"已创建并写入 {parquet_path}")
    
else:
    # 存在则读取现有文件并检查
    splits = pq.read_table(parquet_path).to_pandas()
    logging.info(f"现有splits.parquet包含 {len(splits)} 行记录")
    
    # 创建备份
    if not parquet_bak.exists():
        pq.write_table(pa.Table.from_pandas(splits), parquet_bak)
        logging.info(f"已创建备份 {parquet_bak}")
    
    # 检查trade_date格式
    date_samples = splits['trade_date'].head(5).tolist()
    logging.info(f"trade_date样例: {date_samples}")
    
    # 检查数据类型
    logging.info(f"数据类型: \n{splits.dtypes}")
    
    # 检查不同split的分布
    split_counts = splits['split'].value_counts()
    logging.info(f"Split分布: \n{split_counts}")
    
    # 检查所有必需的split类型是否存在
    unique_splits = splits['split'].unique()
    for required_split in ['train', 'valid', 'test']:
        if required_split not in unique_splits:
            logging.warning(f"缺少 {required_split} 分割，这可能导致某些数据加载器出错")
    
    # 如果需要修复格式问题
    needs_fix = False
    
    # 例如：确保trade_date是字符串格式
    if not pd.api.types.is_string_dtype(splits['trade_date']):
        logging.info("修正 trade_date 为字符串格式")
        splits['trade_date'] = pd.to_datetime(splits['trade_date'], errors='coerce').dt.strftime('%Y%m%d')
        needs_fix = True
    
    # 写回修正后的文件
    if needs_fix:
        pq.write_table(pa.Table.from_pandas(splits), parquet_path)
        logging.info(f"已修正并写回 {parquet_path}")
    else:
        logging.info("无需修正")

# 最终报告
final_splits = pq.read_table(parquet_path).to_pandas()
unique_splits = final_splits['split'].unique()
logging.info(f"最终splits.parquet文件包含以下分割: {unique_splits}")
for split_name in unique_splits:
    count = len(final_splits[final_splits['split'] == split_name])
    logging.info(f"  {split_name}: {count}条记录")
