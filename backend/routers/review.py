"""复盘接口 /api/v1/review/* 与 WS /ws/review/{id}（窗口1）。

契约见 ``docs/architecture.md`` §4.1：
- POST /api/v1/review/analyze
- GET  /api/v1/review/{review_id}/status
- WS   /ws/review/{review_id}
- GET  /api/v1/review/{review_id}
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import threading
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from ..common import db
from ..common.models import (
    MoveInfo,
    ReviewAnalyzeRequest,
    ReviewAnalyzeResponse,
    ReviewDetailResponse,
    ReviewStats,
    ReviewStatusResponse,
)
from ..services.review import service

router = APIRouter(prefix="/api/v1/review", tags=["review"])
ws_router = APIRouter(tags=["review-ws"])


# ---------------------------------------------------------------------------
# WS 连接管理（工作线程 → 主事件循环线程安全广播）
# ---------------------------------------------------------------------------


class WSManager:
    def __init__(self) -> None:
        self._conns: dict[str, set[WebSocket]] = {}
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, review_id: str, ws: WebSocket) -> None:
        await ws.accept()
        with self._lock:
            self._conns.setdefault(review_id, set()).add(ws)

    def disconnect(self, review_id: str, ws: WebSocket) -> None:
        with self._lock:
            conns = self._conns.get(review_id)
            if conns:
                conns.discard(ws)
                if not conns:
                    self._conns.pop(review_id, None)

    def broadcast(self, review_id: str, message: dict) -> None:
        """线程安全广播：由 review 工作线程调用。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with self._lock:
            targets = list(self._conns.get(review_id, ()))
        if not targets:
            return
        for ws in targets:
            asyncio.run_coroutine_threadsafe(self._safe_send(ws, message), loop)

    @staticmethod
    async def _safe_send(ws: WebSocket, message: dict) -> None:
        try:
            await ws.send_json(message)
        except Exception:
            pass


ws_manager = WSManager()
service.set_progress_broadcast(ws_manager.broadcast)


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


def _decode_sgf_text(req: ReviewAnalyzeRequest) -> str:
    sgf = (req.sgf_text or "").strip()
    if not sgf and req.sgf_base64:
        try:
            sgf = base64.b64decode(req.sgf_base64, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="sgf_base64 解码失败") from exc
    if not sgf.strip():
        raise HTTPException(status_code=400, detail="缺少 sgf_text 或 sgf_base64")
    return sgf


@router.post("/analyze", response_model=ReviewAnalyzeResponse)
def analyze(req: ReviewAnalyzeRequest) -> ReviewAnalyzeResponse:
    sgf = _decode_sgf_text(req)
    profile = req.profile if req.profile in ("fast", "standard", "fine") else "fast"
    review_id = service.get_service().submit(sgf, profile)
    return ReviewAnalyzeResponse(review_id=review_id)


@router.get("/{review_id}/status", response_model=ReviewStatusResponse)
def status(review_id: str) -> ReviewStatusResponse:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT status, progress, error FROM reviews WHERE id=?", (review_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="复盘记录不存在")
    return ReviewStatusResponse(
        status=row["status"],
        progress=float(row["progress"] or 0.0),
        error=row["error"],
    )


@router.get("/{review_id}", response_model=ReviewDetailResponse)
def detail(review_id: str) -> ReviewDetailResponse:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM reviews WHERE id=?", (review_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="复盘记录不存在")
        moves = conn.execute(
            "SELECT * FROM moves WHERE review_id=? ORDER BY move_number",
            (review_id,),
        ).fetchall()
    finally:
        conn.close()

    curve: list[MoveInfo] = []
    key_moves: list[MoveInfo] = []
    stats = ReviewStats()
    for m in moves:
        pv: list[str] = []
        if m["pv"]:
            try:
                loaded = json.loads(m["pv"])
                if isinstance(loaded, list):
                    pv = [str(x) for x in loaded]
            except json.JSONDecodeError:
                pv = []
        candidates: list[dict] = []
        if m["candidates"]:
            try:
                loaded = json.loads(m["candidates"])
                if isinstance(loaded, list):
                    candidates = [dict(c) for c in loaded]
            except json.JSONDecodeError:
                candidates = []
        try:
            ownership = json.loads(m["ownership"] or "[]")
        except (json.JSONDecodeError, TypeError):
            ownership = []
        info = MoveInfo(
            move=int(m["move_number"]),
            color=m["color"] or "B",
            coord=m["coord"] or "",
            winrate=m["winrate"],
            score_lead=m["score_lead"],
            category=m["category"] or "normal",
            delta=m["delta"],
            best_coord=m["best_coord"],
            pv=pv,
            visits=m["visits"],
            candidates=candidates,
            score_stdev=m["score_stdev"],
            ownership=ownership,
        )
        curve.append(info)
        if info.category != "normal":
            key_moves.append(info)
            if info.category == "blunder":
                stats.blunders += 1
            elif info.category == "question":
                stats.questions += 1
            elif info.category == "good":
                stats.good += 1

    final_ownership: list[float] = []
    if row["final_ownership"]:
        try:
            loaded = json.loads(row["final_ownership"])
            if isinstance(loaded, list):
                final_ownership = [float(v) for v in loaded]
        except json.JSONDecodeError:
            final_ownership = []

    return ReviewDetailResponse(
        id=row["id"],
        board_size=int(row["board_size"] or 19),
        black=row["black"],
        white=row["white"],
        profile=row["profile"] or "fast",
        status=row["status"] or "pending",
        winrate_curve=curve,
        key_moves=key_moves,
        stats=stats,
        final_ownership=final_ownership,
    )


# ---------------------------------------------------------------------------
# WS
# ---------------------------------------------------------------------------


@ws_router.websocket("/ws/review/{review_id}")
async def review_ws(websocket: WebSocket, review_id: str) -> None:
    await ws_manager.connect(review_id, websocket)
    try:
        # 连接时先推送当前状态
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT status, progress, error FROM reviews WHERE id=?",
                (review_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            await websocket.send_json({"type": "failed", "error": "复盘记录不存在"})
        elif row["status"] == "done":
            await websocket.send_json({"type": "done"})
        elif row["status"] == "failed":
            await websocket.send_json({"type": "failed", "error": row["error"] or ""})
        else:
            await websocket.send_json(
                {"type": "progress", "progress": float(row["progress"] or 0.0)}
            )
        while True:
            await websocket.receive_text()  # 保活
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(review_id, websocket)
