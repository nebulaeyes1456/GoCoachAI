"""题目系统服务（窗口3 实现）。

- ``store``：problems/attempts 数据层；
- ``generator``：错题生成器（review_id → 验证通过的题目）；
- ``checker``：判题（答案对比 + 错误反馈）；
- ``utils``：棋盘/棋串/裁剪/主题/难度等纯函数。
"""
from . import checker, generator, store, utils  # noqa: F401

__all__ = ["checker", "generator", "store", "utils"]
