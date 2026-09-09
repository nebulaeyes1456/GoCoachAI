"""对弈陪练路由（新手板块）：POST /api/v1/play/move、/play/tip。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..services.play import service
from ..services.play.service import BudgetExceeded

router = APIRouter(prefix="/api/v1/play", tags=["play"])


@router.post("/move")
def play_move(payload: dict) -> dict:
    """对弈一手：AI 应手 + 用户这手评价（免费，纯 KataGo）。"""
    try:
        size = int(payload.get("size") or 9)
        moves = payload.get("moves") or []
        komi = float(payload.get("komi") or 6.5)
        ai_rank = payload.get("ai_rank") or None
        return service.analyze_move(size, moves, komi, ai_rank)
    except BudgetExceeded as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"对弈分析异常: {exc}") from exc


@router.post("/tip")
def play_tip(payload: dict) -> dict:
    """坏手/疑问手 LLM 讲解（付费，按局面缓存）。"""
    try:
        size = int(payload.get("size") or 9)
        moves = payload.get("moves") or []
        coord = str(payload.get("coord") or "")
        best = str(payload.get("best") or "")
        delta = payload.get("delta")
        level = str(payload.get("level") or "bad")
        return service.explain_tip(size, moves, coord, best, delta, level)
    except BudgetExceeded as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"讲解生成异常: {exc}") from exc
