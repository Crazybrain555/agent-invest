from typing import Dict, List, Optional
import pandas as pd
from backtest.expression.expression_parser import expression_parser


class AlphaExpression:
    """Alpha因子表达式类 - 支持WorldQuant风格的表达式"""

    def __init__(self, name: str, expression: str, decay: float = 0.5):
        self.name = name
        self.expression = expression
        self.decay = decay  # 因子衰减系数
        self.neutralization = []  # 中性化设置
        self.parser = expression_parser  # 使用新的解析器



    def calculate_factor(self, data_dict: Dict[str, pd.DataFrame]) -> pd.DataFrame:
        """计算因子值

        Args:
            data_dict: 数据字典，包含所有基础因子数据
            格式: {factor_name: DataFrame}，DataFrame的index为日期，columns为股票代码

        Returns:
            pd.DataFrame: 计算后的因子值
        """
        try:
            # 验证表达式
            factor_names = list(data_dict['inds'].keys())
            if not self.parser.validate_expression(self.expression, factor_names):
                raise ValueError(f"表达式验证失败: {self.expression}")

            # 解析并计算表达式
            result = self.parser.parse_expression(self.expression, data_dict)

            # 确保结果的格式正确
            if not isinstance(result, pd.DataFrame):
                raise ValueError("表达式计算结果必须是DataFrame格式")

            return result

        except Exception as e:
            raise ValueError(f"因子计算失败 [{self.name}]: {str(e)}")

    def get_required_factors(self) -> List[str]:
        """获取表达式中需要的因子名称"""
        return self.parser._extract_factor_names(self.expression)

    def validate(self, available_factors: List[str]) -> bool:
        """验证表达式是否可以执行"""
        return self.parser.validate_expression(self.expression, available_factors)

    def preview_calculation(self, data_dict: Dict[str, pd.DataFrame],
                            start_date: str = None, end_date: str = None,
                            max_rows: int = 10) -> pd.DataFrame:
        """预览因子计算结果

        Args:
            data_dict: 数据字典
            start_date: 开始日期
            end_date: 结束日期
            max_rows: 最大显示行数

        Returns:
            pd.DataFrame: 预览结果
        """
        try:
            # 计算因子
            result = self.calculate_factor(data_dict)

            # 筛选日期范围
            if start_date:
                start_date = pd.to_datetime(start_date)
                result = result[result.index >= start_date]

            if end_date:
                end_date = pd.to_datetime(end_date)
                result = result[result.index <= end_date]

            # 限制显示行数
            if len(result) > max_rows:
                result = result.tail(max_rows)

            return result

        except Exception as e:
            print(f"因子预览失败: {str(e)}")
            return pd.DataFrame()

    def __str__(self):
        return f"Alpha({self.name}): {self.expression}"

    def __repr__(self):
        return f"AlphaExpression(name='{self.name}', expression='{self.expression}', neutralization={self.neutralization})"


