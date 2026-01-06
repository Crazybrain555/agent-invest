"""Patch 实用函数

- num_patches: 根据窗口 P 和步长 S 计算 patch 数 N = (T - P) // S + 1
"""

def num_patches(T: int, P: int, S: int) -> int:
    if T < P:
        raise ValueError(f"T({T}) must be >= P({P}).")
    return (T - P) // S + 1


