# -*- coding: utf-8 -*-
import logging

logger = logging.getLogger(__name__)

from datetime import datetime, timedelta

_TRADING_CALENDAR_CACHE = None


def _get_trading_days_before(start_date: str, periods: int) -> str:
    """
    获取指定日期之前N个交易日的日期（带缓存优化）
    
    Args:
        start_date: 基准日期 (YYYYMMDD格式)
        periods: 向前查找的交易日数量
        
    Returns:
        str: N个交易日之前的日期 (YYYYMMDD格式)
    """
    global _TRADING_CALENDAR_CACHE
    
    try:
        # 如果缓存为空，一次性加载完整交易日历
        if _TRADING_CALENDAR_CACHE is None:
            logger.info("首次加载交易日历缓存...")
            from src.utils.db_connection import db_config
            from sqlalchemy import text
            
            # 加载足够长的历史交易日历（比如2000年至今）
            sql = text("""
            SELECT TRADE_DAYS
            FROM wind_quant.dbo.AShareCalendar
            WHERE S_INFO_EXCHMARKET='SSE'
            AND TRADE_DAYS >= '20000101'
            ORDER BY TRADE_DAYS ASC
            """)
            
            with db_config.get_wind_session() as session:
                result = session.execute(sql)
                _TRADING_CALENDAR_CACHE = [str(row[0]) for row in result]
            
            logger.info(f"交易日历缓存加载完成，包含 {_TRADING_CALENDAR_CACHE and len(_TRADING_CALENDAR_CACHE) or 0} 个交易日")
        
        # 从缓存中查找
        trading_dates = _TRADING_CALENDAR_CACHE
        
        if not trading_dates:
            # 如果无法获取交易日历，使用估算方法
            logger.warning(f"无法获取交易日历，使用估算方法：向前推{int(periods * 1.4)}个自然日")
            end_date = datetime.strptime(start_date, '%Y%m%d')
            estimated_date = end_date - timedelta(days=int(periods * 1.4))
            return estimated_date.strftime('%Y%m%d')
        
        # 找到基准日期在交易日历中的位置
        if start_date in trading_dates:
            target_index = trading_dates.index(start_date)
        else:
            # 如果基准日期不是交易日，找到最近的前一个交易日
            target_index = len([d for d in trading_dates if d < start_date]) - 1
        
        # 计算目标日期的索引
        target_date_index = max(0, target_index - periods)
        
        return trading_dates[target_date_index]
        
    except Exception as e:
        logger.error(f"获取交易日失败: {str(e)}")
        # 降级处理：使用估算方法
        end_date = datetime.strptime(start_date, '%Y%m%d')
        estimated_date = end_date - timedelta(days=int(periods * 1.4))
        return estimated_date.strftime('%Y%m%d')


