#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
master_scheduler.py
===================
主调度器：在同一进程里定时执行数据管道任务
  1) task_piplines/train_data_update/run_nas_data_pipeline.py --latest
  2) task_piplines/train_data_update/run_daily_data_pipeline.py (所有步骤)
  3) task_piplines/train_data_update/run_stk_pool_pipline.py --latest
  4) task_piplines/train_data_update/factors_share_iq_pipline.py (因子导出)

每天 00:30 本地时间触发。
保持脚本常驻即可；如需停止，按 Ctrl+C。
"""
import subprocess
import time
import sys
import logging
import os
from pathlib import Path
from datetime import datetime

# ---------- 基础配置 ----------
BASE_DIR = Path(__file__).resolve().parent       # 脚本所在的目录
TASK_PIPELINES_DIR = BASE_DIR / "task_piplines" / "train_data_update"
SCRIPTS_CONFIG = [
    {
        "script": TASK_PIPELINES_DIR / "run_nas_data_pipeline.py",
        "args": ["--latest"],  # 获取最新数据
        "description": "NAS数据管道 - 最新数据"
    },
    {
        "script": TASK_PIPELINES_DIR / "run_daily_data_pipeline.py",
        "args": ["--step", "all"],  # 执行所有步骤
        "description": "每日数据管道 - 所有步骤"
    },
    {
        "script": TASK_PIPELINES_DIR / "run_stk_pool_pipline.py",
        "args": ["--latest"],
        "description": "股票池数据管道 - 最新数据"
    },
    {
        "script": TASK_PIPELINES_DIR / "factors_share_iq_pipline.py",
        # 说明：不传 --dataset_path / --model_path 时，脚本将从 experiment_config 自动解析；
        #       使用断点续跑逻辑，仅对新增交易日增量导出。
        "args": [
            "--start_date", "20210101",
            "--factor_name", "TSVIT_PVHF_10d_v1"
        ],
        "description": "导出TSViT量价高频因子到NAS（增量）"
    }
]

LOG_FILE = BASE_DIR / "master_scheduler.log"     # 持久日志

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),       # 控制台
        logging.FileHandler(LOG_FILE, encoding="utf-8")
    ],
)
logger = logging.getLogger(__name__)

# ---------- 虚拟环境检查 ----------
def check_virtual_env():
    """检查是否在虚拟环境中运行"""
    if hasattr(sys, 'real_prefix') or (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix):
        logger.info(f"✅ 检测到虚拟环境: {sys.prefix}")
        return True
    else:
        logger.warning("⚠️ 未检测到虚拟环境，请确保已激活.venv环境")
        return False

# ---------- 任务函数 ----------
def run_script(script_path: Path, args: list = None, description: str = "") -> bool:
    """
    使用当前 Python 解释器调用脚本。
    返回 True 表示运行成功。
    """
    args = args or []
    cmd = [sys.executable, str(script_path)] + args
    
    logger.info(f"开始执行: {description}")
    logger.info(f"命令: {' '.join(cmd)}")
    
    try:
        env = os.environ.copy()
        # 兼容脚本挪到子目录后依然能 `import src/...` / `import configs/...`
        # 也便于未来在 task_piplines 下继续增加更多任务脚本。
        base_dir_str = str(BASE_DIR)
        existing_pythonpath = env.get("PYTHONPATH", "")
        if existing_pythonpath:
            env["PYTHONPATH"] = base_dir_str + os.pathsep + existing_pythonpath
        else:
            env["PYTHONPATH"] = base_dir_str

        # 强制子进程 UTF-8，避免 Windows / WSL 终端编码差异导致日志乱码
        env.setdefault("PYTHONIOENCODING", "UTF-8")
        env.setdefault("PYTHONUTF8", "1")

        # 使用当前工作目录，继承环境变量
        result = subprocess.run(
            cmd, 
            check=True,
            cwd=BASE_DIR,
            env=env,
            capture_output=False,  # 让输出直接显示到控制台
            text=True
        )
        logger.info(f"✅ {description} 运行完成")
        return True
        
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ {description} 运行失败 (退出码: {e.returncode})")
        return False
        
    except FileNotFoundError:
        logger.error(f"❌ 找不到脚本文件: {script_path}")
        return False
        
    except Exception as e:
        logger.exception(f"❌ {description} 出现未知错误: {e}")
        return False


def daily_job():
    """00:30 执行的总任务"""
    start_time = datetime.now()
    logger.info("=" * 60)
    logger.info(f"🚀 定时任务开始 - {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)
    
    # 检查虚拟环境
    check_virtual_env()
    
    success_count = 0
    total_count = len(SCRIPTS_CONFIG)
    
    for i, config in enumerate(SCRIPTS_CONFIG, 1):
        script = config["script"]
        args = config["args"]
        description = config["description"]
        
        logger.info("-" * 40)
        logger.info(f"📋 任务 {i}/{total_count}: {description}")
        
        # 检查脚本文件是否存在
        if not script.exists():
            logger.error(f"❌ 找不到脚本文件: {script}")
            continue
            
        # 执行脚本
        success = run_script(script, args, description)
        if success:
            success_count += 1
        else:
            logger.error(f"❌ 任务 {i} 失败，继续执行后续任务...")
        
        logger.info(f"任务 {i} 完成状态: {'成功' if success else '失败'}")
    
    # 总结
    end_time = datetime.now()
    duration = end_time - start_time
    
    logger.info("-" * 60)
    if success_count == total_count:
        logger.info(f"🎉 所有任务执行成功! ({success_count}/{total_count})")
    else:
        logger.warning(f"⚠️ 部分任务失败! ({success_count}/{total_count} 成功)")
    
    logger.info(f"⏱️ 总耗时: {duration}")
    logger.info(f"🏁 定时任务结束 - {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)
    logger.info("")  # 空行分隔


def manual_run():
    """手动运行一次任务（用于测试）"""
    logger.info("🧪 手动执行任务（测试模式）")
    daily_job()


# ---------- 主入口 ----------
def main():
    """主函数"""
    logger.info("🚀 Master Scheduler 启动中...")
    logger.info(f"📂 工作目录: {BASE_DIR}")
    logger.info(f"🐍 Python 解释器: {sys.executable}")
    
    # 检查依赖
    try:
        import schedule
        logger.info("✅ schedule 库已安装")
    except ImportError:
        logger.error("❌ 缺少 schedule 库，请运行: pip install schedule")
        return 1
    
    # 检查脚本文件
    missing_scripts = []
    for config in SCRIPTS_CONFIG:
        if not config["script"].exists():
            missing_scripts.append(str(config["script"]))
    
    if missing_scripts:
        logger.error("❌ 以下脚本文件不存在:")
        for script in missing_scripts:
            logger.error(f"   - {script}")
        return 1
    
    logger.info("✅ 所有脚本文件检查通过")
    
    # 检查虚拟环境
    if not check_virtual_env():
        logger.warning("建议在虚拟环境中运行此脚本")
        user_input = input("是否继续? (y/N): ").strip().lower()
        if user_input not in ['y', 'yes']:
            logger.info("用户取消执行")
            return 0
    
    # 询问是否立即测试运行一次
    try:
        user_input = input("是否立即测试运行一次? (y/N): ").strip().lower()
        if user_input in ['y', 'yes']:
            manual_run()
            print()  # 空行分隔
    except KeyboardInterrupt:
        logger.info("用户中断")
        return 0
    
    # 设置定时任务
    schedule.every().day.at("00:30").do(daily_job)
    
    logger.info("⏰ 调度器已启动，将在每天 00:30 触发任务")
    logger.info("📝 日志文件: {}".format(LOG_FILE))
    logger.info("🛑 按 Ctrl+C 退出")
    logger.info("")
    
    try:
        while True:
            schedule.run_pending()
            time.sleep(60)  # 每分钟检查一次
    except KeyboardInterrupt:
        logger.info("\n🛑 收到终止指令，调度器正在关闭...")
        logger.info("👋 Master Scheduler 已停止")
        return 0


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
