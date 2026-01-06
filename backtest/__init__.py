"""
引擎模块 - 包含回测引擎和向量化计算引擎
"""

from __future__ import annotations

# NOTE:
# - 这里不要做“重型模块”的顶层导入（例如会触发数据库初始化的逻辑）。
# - 之前的 `from backtest.alpha_backtest import alpha_backtest` 会在任何
#   `import backtest...`（包括 pipeline 的 types / report）时执行，进而
#   触发 db_connection 初始化，导致缺少 cx_Oracle 的环境直接报错。
#
# 为了保持向后兼容，保留同名函数入口，但改为惰性导入。


def alpha_backtest(*args, **kwargs):
    """Lazy wrapper to avoid import side-effects on package import."""
    from backtest.alpha_backtest import alpha_backtest as _alpha_backtest

    return _alpha_backtest(*args, **kwargs)

__all__ = ['alpha_backtest']