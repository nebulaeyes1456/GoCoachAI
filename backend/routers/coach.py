"""教练讲解接口 /api/v1/coach/*（窗口2 实现）。

契约见 ``docs/architecture.md`` §4.2：
- POST /api/v1/coach/explain
- POST /api/v1/coach/summary
- POST /api/v1/coach/ask
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from ..common.models import (CoachAskRequest, CoachAskResponse,
                             PenaltyRequest, PenaltyResponse, TtsRequest,
                             CoachDeepRequest, CoachDeepResponse,
                             CoachExplainRequest, CoachExplainResponse,
                             CoachSummaryRequest, CoachSummaryResponse)
from ..services.coach import service

router = APIRouter(prefix="/api/v1/coach", tags=["coach"])


@router.post("/explain", response_model=CoachExplainResponse)
def coach_explain(payload: CoachExplainRequest) -> CoachExplainResponse:
    """单手讲解：从复盘数据取该手，调 LLM，结果缓存于 explanations 表。"""
    try:
        return service.explain(payload.review_id, payload.move_number)
    except service.BudgetExceededError as exc:
        raise HTTPException(status_code=402, detail="本月预算已用尽") from exc
    except (service.ReviewNotFoundError, service.MoveNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # LLMError 等
        raise HTTPException(status_code=502, detail=f"讲解服务异常: {exc}") from exc


@router.post("/summary", response_model=CoachSummaryResponse)
def coach_summary(payload: CoachSummaryRequest) -> CoachSummaryResponse:
    """全局总结：基于胜率曲线摘要与关键手，结果缓存于 explanations 表。"""
    try:
        return service.summary(payload.review_id)
    except service.BudgetExceededError as exc:
        raise HTTPException(status_code=402, detail="本月预算已用尽") from exc
    except service.ReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"讲解服务异常: {exc}") from exc


@router.post("/tts")
def coach_tts(payload: TtsRequest) -> Response:
    """语音合成：讲解/报告文本 → mp3（edge-tts 免费在线服务）。"""
    from ..services.coach import tts

    try:
        audio = tts.synthesize(payload.text, payload.persona)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"语音合成异常: {exc}") from exc
    return Response(
        content=audio, media_type="audio/mpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.post("/penalty", response_model=PenaltyResponse)
def coach_penalty(payload: PenaltyRequest) -> PenaltyResponse:
    """惩罚变化：坏手后对手的最强应对 PV（纯 KataGo 分析，免费且缓存）。"""
    try:
        return service.penalty(payload.review_id, payload.move_number)
    except (service.ReviewNotFoundError, service.MoveNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"惩罚分析异常: {exc}") from exc


@router.post("/deep", response_model=CoachDeepResponse)
def coach_deep(payload: CoachDeepRequest) -> CoachDeepResponse:
    """整盘深度分析报告：一次性生成整篇、上下文关联。"""
    try:
        return service.deep_analysis(payload.review_id)
    except service.BudgetExceededError as exc:
        raise HTTPException(status_code=402, detail="本月预算已用尽") from exc
    except service.ReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"讲解服务异常: {exc}") from exc


@router.post("/ask", response_model=CoachAskResponse)
def coach_ask(payload: CoachAskRequest) -> CoachAskResponse:
    """答疑：局面 SGF + 问题直接问 LLM，落 coach_asks 旁表。"""
    try:
        return service.ask(payload.sgf_text, payload.question, payload.level)
    except service.BudgetExceededError as exc:
        raise HTTPException(status_code=402, detail="本月预算已用尽") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"讲解服务异常: {exc}") from exc
