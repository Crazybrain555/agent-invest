#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
基于pyparsing的WorldQuant风格表达式解析器
彻底解决corner case和语法解析问题
"""

import re
import numpy as np
import pandas as pd
from typing import Dict, Any, Union, List

from pyparsing import (
    Word, alphas, alphanums, nums,
    Literal, Optional, ZeroOrMore, OneOrMore,
    Forward, Group, Suppress, ParseException,
    infixNotation, opAssoc, Keyword,
    pyparsing_common as ppc
)

PYPARSING_AVAILABLE = True

from backtest.engine.engine import VectorizedEngine


class WorldQuantExpressionParser:
    """WorldQuant风格表达式解析器 - 基于pyparsing"""

    def __init__(self):
        self.engine = VectorizedEngine()

        # 支持的函数映射
        self.functions = {
            # 基础函数
            'rank': self.engine.rank,
            'abs': self.engine.abs,
            'sign': self.engine.sign,
            'log': self.engine.log,
            'sqrt': self.engine.sqrt,
            'scale': self.engine.scale,

            # 时间序列函数
            'delay': self.engine.delay,
            'delta': self.engine.delta,
            'ts_delta': self.engine.ts_delta,
            'ts_rank': self.engine.ts_rank,
            'ts_mean': self.engine.ts_mean,
            'ts_std': self.engine.ts_std,
            'ts_sum': self.engine.ts_sum,
            'ts_prod': self.engine.ts_prod,
            'ts_min': self.engine.ts_min,
            'ts_max': self.engine.ts_max,
            'ts_argmin': self.engine.ts_argmin,
            'ts_argmax': self.engine.ts_argmax,
            'ts_zscore': self.engine.ts_zscore,
            'ts_skew': self.engine.ts_skew,
            'ts_kurt': self.engine.ts_kurt,
            'ts_returns': self.engine.ts_returns,
            'ts_corr': self.engine.ts_corr,
            'ts_cov': self.engine.ts_cov,

            # 衰减函数
            'decay_linear': self.engine.decay_linear,
            'decay_exp': self.engine.decay_exp,
        }

        if PYPARSING_AVAILABLE:
            self.grammar = self._build_grammar()
        else:
            self.grammar = None

    def _build_grammar(self):
        """构建WorldQuant表达式的语法规则"""

        # 基本元素
        identifier = Word(alphas + "_", alphanums + "_")
        number = ppc.number()

        # 前向引用，用于递归语法
        expr = Forward()

        # 函数调用: func_name(arg1, arg2, ...)
        func_args = Optional(expr + ZeroOrMore(Suppress(",") + expr))
        function_call = identifier + Suppress("(") + Group(Optional(func_args)) + Suppress(")")

        # 基本操作数
        operand = (
                number |
                function_call |
                identifier |
                Suppress("(") + expr + Suppress(")")
        )

        # 三元运算符: condition ? value_true : value_false
        ternary = Group(expr + Suppress("?") + expr + Suppress(":") + expr)

        # 表达式定义
        expr <<= (ternary | operand)

        return expr

    def parse_expression(self, expression: str, data_dict) -> pd.DataFrame:
        """解析并执行表达式"""
        try:
            # 使用简化的回退解析器
            python_code = self._fallback_parse(expression)

            # 创建执行环境
            env = self._create_environment(data_dict['inds'])

            # 执行表达式
            result = self._evaluate_expression(python_code, env)

            return result

        except Exception as e:
            raise ValueError(f"表达式解析失败: {expression}, 错误: {str(e)}")

    def _fallback_parse(self, expression: str) -> str:
        """改进的回退解析器 - 正确处理括号内的三元运算符"""

        # 递归处理多个三元运算符
        max_iterations = 5
        for iteration in range(max_iterations):
            original = expression
            expression = self._process_one_ternary(expression)

            # 如果没有变化，说明没有更多三元运算符了
            if expression == original:
                break

        return expression

    def _process_one_ternary(self, expression: str) -> str:
        """处理一个三元运算符"""

        # 查找第一个 ?
        question_pos = expression.find('?')
        if question_pos == -1:
            return expression

        # 查找对应的 :
        colon_pos = self._find_matching_colon_improved(expression, question_pos)
        if colon_pos == -1:
            return expression

        # 向前查找条件的开始位置
        condition_start = self._find_condition_start_improved(expression, question_pos)

        # 向后查找value_false的结束位置
        value_end = self._find_value_end_improved(expression, colon_pos)

        # 提取各部分
        condition = expression[condition_start:question_pos].strip()
        value_true = expression[question_pos + 1:colon_pos].strip()
        value_false = expression[colon_pos + 1:value_end].strip()

        # 调试信息
        print(f"DEBUG: 处理表达式段: {expression[condition_start:value_end]}")
        print(f"DEBUG: 条件='{condition}', 真值='{value_true}', 假值='{value_false}'")

        # 检查提取是否成功
        if condition and value_true and value_false:
            replacement = f"condition({condition}, {value_true}, {value_false})"
            result = (expression[:condition_start] +
                      replacement +
                      expression[value_end:])
            print(f"DEBUG: 替换成功: {result}")
            return result
        else:
            print("DEBUG: 提取失败，保持原样")
            return expression

    def _find_matching_colon_improved(self, expression: str, question_pos: int) -> int:
        """改进的冒号查找 - 考虑括号和嵌套"""
        paren_count = 0
        question_count = 0

        for i in range(question_pos + 1, len(expression)):
            char = expression[i]

            if char == '(':
                paren_count += 1
            elif char == ')':
                paren_count -= 1
            elif char == '?' and paren_count == 0:
                question_count += 1
            elif char == ':':
                if paren_count == 0 and question_count == 0:
                    return i
                elif question_count > 0:
                    question_count -= 1

        return -1

    def _find_condition_start_improved(self, expression: str, question_pos: int) -> int:
        """改进的条件开始位置查找"""
        paren_count = 0

        # 从问号位置向前查找
        for i in range(question_pos - 1, -1, -1):
            char = expression[i]

            if char == ')':
                paren_count += 1
            elif char == '(':
                paren_count -= 1
                # 如果遇到左括号且括号已平衡，这可能是条件的开始
                if paren_count < 0:
                    return i + 1
            elif paren_count == 0:
                # 在括号外层，遇到运算符就是边界
                if char in '(,+*-/':
                    return i + 1

        return 0

    def _find_value_end_improved(self, expression: str, colon_pos: int) -> int:
        """改进的值结束位置查找"""
        paren_count = 0

        # 从冒号位置向后查找
        for i in range(colon_pos + 1, len(expression)):
            char = expression[i]

            if char == '(':
                paren_count += 1
            elif char == ')':
                if paren_count == 0:
                    # 遇到右括号且括号已平衡，这是值的结束
                    return i
                paren_count -= 1
            elif paren_count == 0:
                # 在括号外层，遇到运算符就是边界
                if char in '+*-/,)':
                    return i

        return len(expression)

    def _create_environment(self, data_dict: Dict[str, pd.DataFrame]) -> Dict[str, Any]:
        """创建执行环境"""
        env = {}
        env.update(data_dict)
        env.update(self.functions)
        env['condition'] = self._condition_wrapper
        env['np'] = np
        env['pd'] = pd
        return env

    def _condition_wrapper(self, condition, value_if_true, value_if_false):
        """条件函数包装器"""
        return self.engine.condition(condition, value_if_true, value_if_false)

    def _evaluate_expression(self, expression: str, env: Dict[str, Any]) -> pd.DataFrame:
        """执行表达式"""
        try:
            result = eval(expression, {"__builtins__": {}}, env)


            return result

        except Exception as e:
            raise ValueError(f"表达式执行失败: {str(e)}")

    def validate_expression(self, expression: str, factor_names: List[str]) -> bool:
        """验证表达式是否有效"""
        try:
            used_factors = self._extract_factor_names(expression)
            undefined_factors = set(used_factors) - set(factor_names)

            if undefined_factors:
                print(f"警告: 表达式中包含未定义的因子: {undefined_factors}")
                return False

            return True

        except Exception as e:
            print(f"表达式验证失败: {str(e)}")
            return False

    def _extract_factor_names(self, expression: str) -> List[str]:
        """从表达式中提取因子名称"""
        factor_pattern = r'\b[a-zA-Z_][a-zA-Z0-9_]*\b'
        matches = re.findall(factor_pattern, expression)

        factor_names = []
        for match in matches:
            if match not in self.functions and match not in ['np', 'pd', 'condition']:
                factor_names.append(match)

        return list(set(factor_names))


# 创建新的解析器实例
expression_parser= WorldQuantExpressionParser()