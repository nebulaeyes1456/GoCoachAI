"""最小 SGF 工具（窗口0）。

功能（只够后端用，不追求完整 SGF 标准）：
- ``parse_sgf``：提取棋盘大小（SZ）、黑白棋手名（PB/PW）、主线手数序列 ``(color, coord)``。
- ``sgf_to_coord`` / ``coord_to_sgf``：SGF 坐标与界面坐标互转。

坐标约定（契约固定）：
- 界面 ``coord`` 一律用 "A1"~"T19" 风格：列字母 A~T（跳过 I），行数字 1~19，
  采用 GTP/KataGo 约定（A1 在左下角）；pass 用空串 ""。
- SGF 坐标 "aa" 为棋盘左上角，对应界面 "A19"（行号从底部往上数）。
- 例：SGF "pd"（天元）↔ 界面 "Q16"；SGF "dd" ↔ "D16"。
- SGF 的 19 路 pass 记作 "tt"；通用记作 (chr(ord('a')+board_size)) 重复两次。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 界面列字母表：A~T 跳过 I（19 列）
BOARD_COLS = "ABCDEFGHJKLMNOPQRST"


def _sgf_index(letter: str) -> int:
    """SGF 坐标字母（a=0）转 0 基下标。"""
    return ord(letter.lower()) - ord("a")


def _pass_point(board_size: int) -> str:
    """SGF 中表示 pass 的坐标串，如 19 路为 "tt"。"""
    letter = chr(ord("a") + board_size)
    return letter * 2


def sgf_to_coord(sgf_point: str, board_size: int = 19) -> str:
    """SGF 坐标 → 界面坐标。

    - SGF "aa"（左上角）→ 界面 "A19"；
    - pass（""、"tt"、越界坐标）→ 空串 ""。
    """
    point = (sgf_point or "").strip().lower()
    if not point:
        return ""
    if len(point) < 2:
        return ""
    if point == _pass_point(board_size):
        return ""
    col_i, row_i = _sgf_index(point[0]), _sgf_index(point[1])
    if col_i >= board_size or row_i >= board_size:
        # 越界（如 19 路的 "tt"）视为 pass
        return ""
    col = BOARD_COLS[col_i]
    row = board_size - row_i  # SGF 行 'a' 在顶部 → 最大行号
    return f"{col}{row}"


def coord_to_sgf(coord: str, board_size: int = 19) -> str:
    """界面坐标 → SGF 坐标。

    - "A19" → SGF "aa"；"Q16" → "pd"；
    - 空串或 "pass" → SGF pass（19 路为 "tt"）。
    """
    c = (coord or "").strip().upper()
    if c in ("", "PASS"):
        return _pass_point(board_size)
    if len(c) < 2:
        raise ValueError(f"非法坐标: {coord!r}")
    col_letter, row_text = c[0], c[1:]
    if col_letter not in BOARD_COLS:
        raise ValueError(f"非法列字母: {col_letter!r}")
    try:
        row = int(row_text)
    except ValueError as exc:
        raise ValueError(f"非法行号: {coord!r}") from exc
    if not 1 <= row <= board_size:
        raise ValueError(f"行号超出棋盘: {coord!r}")
    col_i = BOARD_COLS.index(col_letter)
    row_i = board_size - row
    return f"{chr(ord('a') + col_i)}{chr(ord('a') + row_i)}"


@dataclass
class ParsedSGF:
    """parse_sgf 的返回值。"""

    board_size: int = 19
    black: str = ""
    white: str = ""
    moves: list[tuple[str, str]] = field(default_factory=list)  # (color, coord)

    def __post_init__(self) -> None:
        if self.board_size <= 0:
            raise ValueError(f"非法棋盘大小: {self.board_size}")


_MOVE_RE = re.compile(r";([BW])\[([a-zA-Z]*)\]")


def parse_sgf(sgf_text: str) -> ParsedSGF:
    """解析 SGF 文本：棋盘大小、黑白棋手名、主线手数序列。

    - 仅提取主线（顶层）着法，跳过 ``( ... )`` 分支内的着法；
    - 坐标经 ``sgf_to_coord`` 换算为界面风格；pass 为 ""。
    """
    text = sgf_text or ""
    board_size = 19
    black = white = ""

    m = re.search(r"SZ\s*\[\s*(\d+)", text, re.IGNORECASE)
    if m:
        board_size = int(m.group(1))
    m = re.search(r"PB\s*\[([^\]]*)\]", text, re.IGNORECASE)
    if m:
        black = m.group(1).strip()
    m = re.search(r"PW\s*\[([^\]]*)\]", text, re.IGNORECASE)
    if m:
        white = m.group(1).strip()

    moves: list[tuple[str, str]] = []
    depth = 0
    main_depth: int | None = None  # 第一个着法节点所在深度即主线深度
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == ";" and (main_depth is None or depth == main_depth):
            m = _MOVE_RE.match(text, i)
            if m:
                if main_depth is None:
                    main_depth = depth  # SGF 根序列最先出现，其深度即主线
                color = m.group(1).upper()
                coord = sgf_to_coord(m.group(2), board_size)
                moves.append((color, coord))
                i += m.end() - i - 1
        i += 1
    return ParsedSGF(board_size=board_size, black=black, white=white, moves=moves)
