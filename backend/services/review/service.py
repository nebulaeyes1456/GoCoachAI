"""复盘服务（窗口1）。

- 任务队列：**单工作线程**顺序分析（避免 CPU 争抢），支持排队与取消；
- ``analyze_review(review_id, sgf_text, profile)``：
  存 SGF → 调 kata-analyze → 解析 → 计算每手当前方胜率与 delta →
  按 §4.1 阈值打标（blunder/question/good）→ 写 moves 表 →
  更新 reviews.status/progress（经 WS 推送）；
- 缓存：同 SGF 同 profile 已有 done 记录则直接返回，不重跑；
- 坐标转换复用 common/sgf_io.py，确保与前端一致。
"""
from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from ...common import db as db_mod
from ...common.settings import BASE_DIR, get_settings
from ..engine.analyze_sgf import analyze_sgf, complement_final_turn
from .classify import Thresholds, classify_move, is_handicap_game

SGF_DIR = BASE_DIR / "data" / "sgfs"

# 进度广播回调：routers/review.py 注册（广播 {"type":"progress",...} 等）
_progress_broadcast: Callable[[str, dict], None] = lambda review_id, msg: None


def set_progress_broadcast(fn: Callable[[str, dict], None]) -> None:
    """由 router 注册 WS 广播函数（避免循环导入）。"""
    global _progress_broadcast
    _progress_broadcast = fn


@dataclass
class ReviewTask:
    review_id: str
    sgf_text: str
    profile: str


class ReviewService:
    """单工作线程复盘任务队列。"""

    def __init__(self) -> None:
        self._queue: "queue.Queue[ReviewTask]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._cancel_pending: set[str] = set()
        self._cancel_running: set[str] = set()
        self._current: Optional[str] = None
        self._running = False

    # ------------------------------------------------------------- 生命周期

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._worker = threading.Thread(
                target=self._run, name="review-worker", daemon=True
            )
            self._worker.start()

    def stop(self) -> None:
        with self._lock:
            self._running = False
        # 放入哨兵唤醒 worker 退出
        self._queue.put(None)  # type: ignore[arg-type]

    # ------------------------------------------------------------- 提交/取消

    def submit(self, sgf_text: str, profile: str) -> str:
        """提交分析任务；缓存命中（同 SGF 同 profile 已 done）直接复用。

        返回 review_id（SGF sha256 前 16 位，契约 §3）。
        """
        sgf = sgf_text or ""
        review_id = hashlib.sha256(sgf.encode("utf-8")).hexdigest()[:16]

        from ...common import sgf_io

        parsed = sgf_io.parse_sgf(sgf)
        board_size = parsed.board_size
        black, white = parsed.black, parsed.white

        conn = db_mod.connect()
        try:
            row = conn.execute(
                "SELECT id, profile, status FROM reviews WHERE id = ?",
                (review_id,),
            ).fetchone()
            if row and row["profile"] == profile and row["status"] == "done":
                # 缓存命中：不重跑
                return review_id
            SGF_DIR.mkdir(parents=True, exist_ok=True)
            sgf_path = SGF_DIR / f"{review_id}.sgf"
            sgf_path.write_text(sgf, encoding="utf-8")
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            conn.execute(
                """
                INSERT INTO reviews
                    (id, sgf_path, board_size, black, white, created_at,
                     status, profile, progress, error)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, 0.0, NULL)
                ON CONFLICT(id) DO UPDATE SET
                    sgf_path=excluded.sgf_path,
                    board_size=excluded.board_size,
                    black=excluded.black,
                    white=excluded.white,
                    created_at=excluded.created_at,
                    status='pending',
                    profile=excluded.profile,
                    progress=0.0,
                    error=NULL
                """,
                (review_id, str(sgf_path), board_size, black, white, now, profile),
            )
            # 同 SGF 换 profile 重跑时清掉旧 moves
            conn.execute("DELETE FROM moves WHERE review_id = ?", (review_id,))
            conn.commit()
        finally:
            conn.close()

        with self._lock:
            self._cancel_pending.discard(review_id)
            self._cancel_running.discard(review_id)
        self._queue.put(ReviewTask(review_id, sgf, profile))
        return review_id

    def cancel(self, review_id: str) -> bool:
        """取消任务。

        排队中的任务会直接移除不执行；已开始分析的任务无法中断
        （kata-analyze 为批量子进程），结果照常落库。返回是否接受取消请求。
        """
        with self._lock:
            self._cancel_pending.add(review_id)
        return True

    # ------------------------------------------------------------- 工作线程

    def _run(self) -> None:
        while True:
            task: Optional[ReviewTask] = self._queue.get()
            try:
                if task is None:  # 停止哨兵
                    break
                with self._lock:
                    if task.review_id in self._cancel_pending:
                        self._cancel_pending.discard(task.review_id)
                        continue
                self._current = task.review_id
                self._process(task)
            except Exception as exc:  # 单任务异常不杀死工作线程
                if task is not None:
                    self._set_status(task.review_id, "failed", error=str(exc))
            finally:
                self._current = None

    def _process(self, task: ReviewTask) -> None:
        review_id, sgf_text, profile = task.review_id, task.sgf_text, task.profile
        self._set_status(review_id, "analyzing", progress=0.01)

        def progress_cb(frac: float) -> None:
            # 0.02 ~ 0.90 区间映射分析进度（剩余 10% 留给写库）
            p = 0.02 + 0.88 * max(0.0, min(1.0, frac))
            self._set_status(review_id, "analyzing", progress=round(p, 4))

        t0 = time.time()
        result = analyze_sgf(sgf_text, profile=profile, progress_cb=progress_cb)
        elapsed = time.time() - t0

        # 最后一手之后的局面（补 after 值）：优先用 analyze_sgf 已返回的
        # final_turn（winrate 为对手视角）；旧版 kata-analyze 路径缺失时补查
        final_turn: Optional[dict] = None
        if result.final_turn is not None:
            ft = result.final_turn
            if ft.winrate is not None:
                final_turn = {
                    "winrate": ft.winrate,
                    "score_lead": ft.score_lead,
                    "ownership": [
                        float(v) for v in (ft.ownership or [])
                    ],
                }
        elif result.turns:
            try:
                final_turn = complement_final_turn(sgf_text, profile=profile)
            except Exception:
                final_turn = None

        rows = build_move_rows(sgf_text, result.turns, final_turn, profile)
        if not rows:
            self._set_status(review_id, "done", progress=1.0)
            return

        # 终局目数热图：取最后一手的 ownership（[-1,1] 数组）
        final_ownership: list[float] = []
        if result.turns:
            final_ownership = [
                float(v) for v in (result.turns[-1].ownership or [])
            ]

        conn = db_mod.connect()
        try:
            total = len(rows)
            for i, row in enumerate(rows, start=1):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO moves
                        (review_id, move_number, color, coord, winrate,
                         score_lead, visits, category, delta, best_coord, pv,
                         candidates, score_stdev, ownership)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (review_id, *row),
                )
                if i % 5 == 0 or i == total:
                    conn.commit()
                    self._set_status(
                        review_id, "analyzing",
                        progress=round(0.90 + 0.10 * i / total, 4),
                    )
            conn.commit()
            # 终局目数热图写库（v4 迁移列；供 detail 接口返回前端叠加层）
            conn.execute(
                "UPDATE reviews SET final_ownership=? WHERE id=?",
                (json.dumps(final_ownership), review_id),
            )
            conn.commit()
        finally:
            conn.close()
        self._set_status(review_id, "done", progress=1.0)
        print(
            f"[review] {review_id} done: {len(rows)} 手, 分析耗时 {elapsed:.1f}s, "
            f"profile={profile}"
        )

    def _set_status(self, review_id: str, status: str, progress: Optional[float] = None,
                    error: Optional[str] = None) -> None:
        conn = db_mod.connect()
        try:
            if progress is None:
                progress = _current_progress(conn, review_id)
            conn.execute(
                "UPDATE reviews SET status=?, progress=?, error=? WHERE id=?",
                (status, progress, error, review_id),
            )
            conn.commit()
        finally:
            conn.close()
        if status == "done":
            _progress_broadcast(review_id, {"type": "done"})
        elif status == "failed":
            _progress_broadcast(
                review_id, {"type": "failed", "error": error or ""}
            )
        else:
            _progress_broadcast(
                review_id, {"type": "progress", "progress": progress}
            )


def _current_progress(conn, review_id: str) -> float:
    row = conn.execute(
        "SELECT progress FROM reviews WHERE id=?", (review_id,)
    ).fetchone()
    return float(row["progress"]) if row else 0.0


# ---------------------------------------------------------------------------
# 每手数据组装（纯函数，便于测试）
# ---------------------------------------------------------------------------


def build_move_rows(
    sgf_text: str,
    turns: list[Any],
    final_turn: Optional[dict],
    profile: str = "fast",
) -> list[tuple]:
    """由 TurnInfo 列表构造 moves 表行。

    返回 ``[(move_number, color, coord, winrate, score_lead, visits,
    category, delta, best_coord, pv_json), ...]``。

    换算规则（SIDETOMOVE 视角）：
    - 第 i 手（0 基 turn=i-1）的 before = turns[i-1].winrate（当前方=落子方）；
    - after = 1 - turns[i].winrate（下一局面当前方为对手）；
    - 最后手的 after 来自 final_turn（若可得）；否则 delta=None；
    - 存库 winrate = after（该手落下后当前方胜率），与 delta 同口径。
    """
    from ...common import sgf_io

    parsed = sgf_io.parse_sgf(sgf_text)
    n = len(parsed.moves)
    if n == 0 or len(turns) < n:
        return []
    cfg = get_settings()
    review_cfg = cfg.get("review", {})
    thresholds = Thresholds(
        blunder=float(review_cfg.get("blunder_threshold", 0.12)),
        question=float(review_cfg.get("question_threshold", 0.06)),
        good=float(review_cfg.get("good_threshold", 0.06)),
        relax=(
            float(review_cfg.get("handicap_relax", 1.5))
            if is_handicap_game(sgf_text)
            else 1.0
        ),
    )
    rows: list[tuple] = []
    for i, (color, coord) in enumerate(parsed.moves):
        before = turns[i].winrate
        # 该手落下后的局面 = turns[i+1]（下一个 turn 的局面）；最后一手用 final_turn
        if i + 1 < len(turns):
            ownership = turns[i + 1].ownership
        else:
            ownership = (final_turn or {}).get("ownership") or []
        best_coord = turns[i].best_coord
        pv = turns[i].pv
        visits = turns[i].visits
        score_lead = turns[i].score_lead
        score_stdev = turns[i].score_stdev
        # 选点推荐：取该局面 KataGo 前 5 个候选点（精简字段）
        candidates: list[dict] = []
        for mi in (turns[i].move_infos or [])[:5]:
            mv = str(mi.get("move", ""))
            candidates.append(
                {
                    "order": int(mi.get("order", 0)),
                    "move": "" if mv.lower() == "pass" else mv,
                    "winrate": mi.get("winrate"),
                    "score_lead": mi.get("scoreLead"),
                    "visits": mi.get("visits"),
                    "pv": [str(x) for x in (mi.get("pv") or [])][:8],
                }
            )
        if i + 1 < n and turns[i + 1].winrate is not None:
            after = 1.0 - turns[i + 1].winrate
        elif final_turn and final_turn.get("winrate") is not None:
            after = 1.0 - float(final_turn["winrate"])
        else:
            after = None
        delta = (after - before) if (after is not None and before is not None) else None
        category = classify_move(delta, best_coord, coord, thresholds)
        rows.append(
            (
                i + 1,
                color,
                coord,
                after,
                score_lead,
                visits,
                category,
                delta,
                best_coord,
                json.dumps(pv, ensure_ascii=False),
                json.dumps(candidates, ensure_ascii=False),
                score_stdev,
                json.dumps([float(v) for v in (ownership or [])],
                           ensure_ascii=False),
            )
        )
    return rows


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------

_service: Optional[ReviewService] = None
_service_lock = threading.Lock()


def get_service() -> ReviewService:
    global _service
    with _service_lock:
        if _service is None:
            _service = ReviewService()
        return _service
