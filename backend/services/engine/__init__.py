"""KataGo 引擎封装（窗口1）。

- ``KataGoEngine``：``katago analysis`` 协议进程封装（生命周期/查询/心跳）；
- ``analyze_sgf``：整局批量分析（kata-analyze 子命令，v1.18+ 自动回退
  analysis 引擎协议）；
- ``verify_position``：局面验证接口（窗口3 验题复用，签名见
  docs/architecture.md §4.3 附注）。
"""
from .analyze_sgf import AnalysisResult, TurnInfo, analyze_sgf
from .engine import EngineError, KataGoEngine
from .verify import VerifyResult, verify_position

__all__ = [
    "AnalysisResult",
    "EngineError",
    "KataGoEngine",
    "TurnInfo",
    "VerifyResult",
    "analyze_sgf",
    "verify_position",
]
