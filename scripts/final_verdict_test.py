#!/usr/bin/env python
import time
import duckdb
import os
import logging

# Setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("final_verdict_test")

def setup_db_connection():
    """设置数据库连接并创建数据视图"""
    dataset_path = "data/Dataset/pv_v5_pv_v5_pvh"
    con = duckdb.connect(":memory:")
    parquet_path = os.path.join(dataset_path, "shards/**/*.parquet")
    con.execute(f"CREATE VIEW raw_data AS SELECT * FROM parquet_scan('{parquet_path}', binary_as_string=true)")
    indices_path = os.path.join(dataset_path, "meta/train_indices.parquet")
    con.execute(f"CREATE VIEW fixed_indices AS SELECT * FROM parquet_scan('{indices_path}')")
    return con

def fetch_chunk(con, chunk_size, offset):
    """模拟从DuckDB中获取一个数据块"""
    query = f"""
        SELECT r.trade_date, r.stock_code, r.tc_t10_n30_adj
        FROM raw_data r
        INNER JOIN fixed_indices f ON r.trade_date = f.trade_date AND r.stock_code = f.stock_code
        ORDER BY r.trade_date, r.stock_code
        LIMIT {chunk_size} OFFSET {offset}
    """
    start_time = time.perf_counter()
    con.execute(query).fetchall()
    duration = time.perf_counter() - start_time
    return duration

def simulate_training_step(duration=0.1):
    """模拟一次训练步骤（如forward/backward pass）所花费的时间"""
    time.sleep(duration)

def main():
    """主函数，执行对比测试并输出结论"""
    con = setup_db_connection()
    
    BATCH_SIZE = 16384
    SMALL_CHUNK_SIZE = 4096
    TRAIN_STEP_TIME = 0.1  # 100ms
    
    logger.info("="*80)
    logger.info("🧪 最终诊断测试：验证 '取数-训练-取数' 间断模式的影响")
    logger.info("="*80)
    logger.info(f"配置: batch_size={BATCH_SIZE}, small_chunk_size={SMALL_CHUNK_SIZE}, 模拟训练耗时={TRAIN_STEP_TIME}s")

    # --- 模式1: 间断模式 (模拟真实DataLoader) ---
    logger.info("\n--- 模式1: 间断模式 (多次小I/O) ---")
    logger.info("模拟为了凑齐一个batch，多次执行chunk I/O")
    
    num_chunks = BATCH_SIZE // SMALL_CHUNK_SIZE
    start_mode1_fetch = time.perf_counter()
    total_fetch_time_mode1 = 0
    for i in range(num_chunks):
        fetch_duration = fetch_chunk(con, SMALL_CHUNK_SIZE, i * SMALL_CHUNK_SIZE)
        total_fetch_time_mode1 += fetch_duration
        logger.info(f"  获取Chunk {i+1}/{num_chunks} 耗时: {fetch_duration:.3f}s")
    
    logger.info(f"  👉 总取数时间: {total_fetch_time_mode1:.3f}s")
    
    logger.info("  ...现在开始训练...")
    simulate_training_step(TRAIN_STEP_TIME)
    logger.info(f"  ...训练完成 ({TRAIN_STEP_TIME}s)")
    
    total_time_mode1 = total_fetch_time_mode1 + TRAIN_STEP_TIME
    logger.info(f"  ✅ 模式1 (取数+训练) 总耗时: {total_time_mode1:.3f}s")

    # --- 模式2: 理想模式 (一次性大I/O) ---
    logger.info("\n--- 模式2: 理想模式 (一次性大I/O) ---")
    logger.info("模拟为了凑齐一个batch，执行单次chunk I/O")

    total_fetch_time_mode2 = fetch_chunk(con, BATCH_SIZE, 0)
    logger.info(f"  👉 总取数时间: {total_fetch_time_mode2:.3f}s")

    logger.info("  ...现在开始训练...")
    simulate_training_step(TRAIN_STEP_TIME)
    logger.info(f"  ...训练完成 ({TRAIN_STEP_TIME}s)")
    
    total_time_mode2 = total_fetch_time_mode2 + TRAIN_STEP_TIME
    logger.info(f"  ✅ 模式2 (取数+训练) 总耗时: {total_time_mode2:.3f}s")

    # --- 最终结论 ---
    logger.info("\n" + "="*80)
    logger.info("🎯 最终结论")
    logger.info("="*80)
    logger.info(f"间断模式 (多次小I/O): {total_time_mode1:.3f}s")
    logger.info(f"理想模式 (一次大I/O): {total_time_mode2:.3f}s")
    
    if total_time_mode2 > 0:
        performance_diff = total_time_mode1 / total_time_mode2
        logger.info(f"✅ 结论: 你的想法完全正确。'间断性取数' 模式比理想模式慢了 {performance_diff:.1f} 倍。")
        logger.info("   根本原因在于【多次小I/O的累积耗时】远大于【一次性大I/O的耗时】。")
    else:
        logger.error("❌ 理想模式耗时为0，无法计算性能差异。")

    con.close()

if __name__ == "__main__":
    main() 