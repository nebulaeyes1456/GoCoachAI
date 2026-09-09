"""关键手判定（窗口1，纯函数，供单元测试）。

契约：``docs/architecture.md`` §4.1 —— 当前方胜率变化
``delta = winrate_after - winrate_before``：

- ``delta <= -blunder_threshold``          → blunder（坏手）
- ``-blunder < delta <= -question``        → question（疑问手）
- ``delta >= +good_threshold`` 且最佳点≠落点 → good（好手）
- 其余 → normal

阈值放 config.yaml（review.*）；让子棋/早期布局可放宽：
``review.handicap_relax`` 为让子棋（SGF 含 HA>0 或 AB 让子）阈值放宽系数。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Thresholds:
    blunder: float = 0.12
    question: float = 0.06
    good: float = 0.06
    relax: float = 1.0  # 让子棋放宽系数（>1 表示阈值放大）

    @property
    def blunder_relaxed(self) -> float:
        return self.blunder * self.relax

    @property
    def question_relaxed(self) -> float:
        return self.question * self.relax

    @property
    def good_relaxed(self) -> float:
        return self.good * self.relax


def is_handicap_game(sgf_text: str) -> bool:
    """判断 SGF 是否为让子棋（HA>0 或含 AB 摆子）。"""
    import re

    if re.search(r"HA\s*\[\s*([1-9]\d*)", sgf_text or "", re.IGNORECASE):
        return True
    if re.search(r"\bAB\s*\[", sgf_text or "", re.IGNORECASE):
        return True
    return False


def classify_move(
    delta: Optional[float],
    best_coord: Optional[str],
    actual_coord: Optional[str],
    thresholds: Thresholds,
) -> str:
    """按契约阈值判定单手的 category（blunder/question/good/normal）。"""
    if delta is None:
        return "normal"
    best = (best_coord or "").strip().upper()
    actual = (actual_coord or "").strip().upper()
    if delta <= -thresholds.blunder_relaxed:
        return "blunder"
    if delta <= -thresholds.question_relaxed:
        return "question"
    if delta >= thresholds.good_relaxed and best and actual and best != actual:
        return "good"
    return "normal"
