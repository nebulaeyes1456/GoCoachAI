"""复盘服务（窗口1）。

- ``get_service()``：全局单例任务队列（单工作线程，支持排队与取消）；
- ``build_move_rows``：胜率换算 + 关键手判定（纯函数，供测试）。
"""
from .classify import Thresholds, classify_move, is_handicap_game
from .service import ReviewService, build_move_rows, get_service

__all__ = [
    "ReviewService",
    "Thresholds",
    "build_move_rows",
    "classify_move",
    "get_service",
    "is_handicap_game",
]
