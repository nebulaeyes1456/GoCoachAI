"""题目系统接口 /api/v1/problems/*（窗口3 实现）。

契约见 ``docs/architecture.md`` §4.3：
- POST /api/v1/problems/generate
- GET  /api/v1/problems/library   （注意：须声明在 /{problem_id} 之前）
- GET  /api/v1/problems/{problem_id}
- POST /api/v1/problems/{problem_id}/attempt

说明：
- generate 为同步接口（契约要求直接返回题目列表）；单题验证约 10~30 秒，
  生成 max_problems 道可能耗时数分钟，用 def 端点让 FastAPI 放进线程池，
  避免阻塞事件循环；前端在窗口4 可加 loading 提示。
- 判题接口对错误答案可能触发一次引擎补查（约数秒），同样走线程池。
"""

from __future__ import annotations

import threading
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ..common.models import (
    ChainBrief,
    ChainDetailResponse,
    ChainGrowRequest,
    ChainGrowResponse,
    ChainListResponse,
    ChainProblemBrief,
    ExtractFromReviewRequest,
    ExtractFromReviewResponse,
    GeneratedProblemBrief,
    LibraryProblemBrief,
    ProblemAttemptRequest,
    ProblemAttemptResponse,
    ProblemDetailResponse,
    ProblemExplainResponse,
    ProblemExtractRequest,
    ProblemExtractResponse,
    ProblemGenerateRequest,
    ProblemGenerateResponse,
    ProblemLibraryResponse,
)
from ..services.problems import (
    chains,
    checker,
    explainer,
    extractor,
    generator,
    store,
)

router = APIRouter(prefix="/api/v1/problems", tags=["problems"])

# 提取任务互斥锁：引擎单进程，同一时间只跑一个提取任务（占用时返回 409）
_extract_lock = threading.Lock()

# 生长任务互斥锁：引擎单进程，同一条链/不同链的 grow 不并发
_grow_lock = threading.Lock()


@router.post("/generate", response_model=ProblemGenerateResponse)
def generate(req: ProblemGenerateRequest) -> ProblemGenerateResponse:
    """从复盘失误生成题目（同步；耗时取决于题目数与验证深度）。"""
    try:
        result = generator.generate(
            review_id=req.review_id,
            themes=req.themes or None,
            max_problems=req.max_problems,
            target_rank=req.target_rank,
        )
    except generator.ReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except generator.SgfFileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # 引擎启动失败等
        raise HTTPException(status_code=502, detail=f"题目生成失败: {exc}") from exc
    return ProblemGenerateResponse(
        problems=[GeneratedProblemBrief(**p) for p in result["problems"]],
        failed=int(result["failed"]),
    )


@router.post("/extract", response_model=ProblemExtractResponse)
def extract(req: ProblemExtractRequest) -> ProblemExtractResponse:
    """从任意棋局扫描未定型区域成题（M3，同步；引擎单进程，互斥锁防并发）。"""
    if not (req.sgf_text or "").strip() and not (req.review_id or "").strip():
        raise HTTPException(
            status_code=400, detail="sgf_text 与 review_id 至少提供一个"
        )
    if not _extract_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409, detail="已有提取任务在运行，请稍后再试"
        )
    try:
        result = extractor.extract(
            sgf_text=(req.sgf_text or "").strip() or None,
            review_id=(req.review_id or "").strip() or None,
            max_problems=req.max_problems,
            target_rank=req.target_rank,
        )
    except extractor.ReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except extractor.SgfFileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except extractor.ReviewFailedError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except extractor.ReviewTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except Exception as exc:  # 引擎启动失败等
        raise HTTPException(status_code=502, detail=f"局部提取失败: {exc}") from exc
    finally:
        _extract_lock.release()
    return ProblemExtractResponse(
        extracted=[GeneratedProblemBrief(**p) for p in result["extracted"]],
        failed=int(result["failed"]),
        skipped=int(result["skipped"]),
    )


@router.get("/chains", response_model=ChainListResponse)
def list_chains() -> ChainListResponse:
    """定式链列表（v1.7.0）：名称/描述/题数/涉及主题。"""
    return ChainListResponse(
        chains=[ChainBrief(**c) for c in chains.list_chains()]
    )


@router.get("/chains/{chain_id}", response_model=ChainDetailResponse)
def chain_detail(chain_id: str) -> ChainDetailResponse:
    """链详情：按 chain_step 排序的题目列表（即「第 1 变 → 第 n 变」学习顺序）。"""
    chain = chains.get_chain(chain_id)
    if chain is None:
        raise HTTPException(status_code=404, detail="题链不存在")
    items = store.list_chain_problems(chain_id)
    problems = [
        ChainProblemBrief(
            id=p["id"],
            theme=p["theme"],
            goal=p.get("goal"),
            rank_min=p.get("rank_min"),
            rank_max=p.get("rank_max"),
            setup_sgf=p["setup_sgf"],
            hint=p.get("hint"),
            answer=p.get("answer") or "",
            verdict=p.get("verdict"),
            chain_id=chain_id,
            chain_step=p.get("chain_step"),
            solved=store.has_correct_attempt(p["id"]),
        )
        for p in items
    ]
    brief = next(
        (c for c in chains.list_chains() if c["id"] == chain_id), None
    ) or {
        "id": chain_id, "name": chain["name"], "theme": chain.get("theme"),
        "description": chain.get("description"), "status": chain["status"],
        "problems_count": len(problems), "themes": [],
        "created_at": chain.get("created_at"),
    }
    return ChainDetailResponse(
        chain=ChainBrief(**brief),
        root_sgf=chain["root_sgf"],
        problems=problems,
    )


@router.post("/chains/{chain_id}/grow", response_model=ChainGrowResponse)
def grow_chain(chain_id: str, req: ChainGrowRequest) -> ChainGrowResponse:
    """触发生长（同步执行；引擎单进程，占用时返回 409）。

    返回新增题数与丢弃数；同一链重复调用幂等（新增 0 题、步序不变）。
    """
    if not _grow_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有生长任务在运行，请稍后再试")
    try:
        result = chains.grow_chain(
            chain_id,
            max_depth=req.max_depth,
            max_per_level=req.max_per_level,
            profile=req.profile,
        )
    except chains.ChainNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except chains.ChainSgfError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # 引擎启动失败等
        raise HTTPException(status_code=502, detail=f"链生长失败: {exc}") from exc
    finally:
        _grow_lock.release()
    return ChainGrowResponse(
        chain_id=result["chain_id"],
        added=int(result["added"]),
        discarded=int(result["discarded"]),
        steps=int(result["steps"]),
        problems=[ChainProblemBrief(**p) for p in result["problems"]],
    )


@router.get("/library", response_model=ProblemLibraryResponse)
def library(
    theme: Optional[str] = Query(default=None),
    rank: Optional[int] = Query(default=None),
    sort: Optional[str] = Query(default=None, pattern="^(easiest|hardest)$"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ProblemLibraryResponse:
    """题库分页 + 筛选 + 难度排序（sort: easiest/hardest）。"""
    items, total = store.list_problems(
        theme=theme, rank=rank, limit=limit, offset=offset, sort=sort
    )
    return ProblemLibraryResponse(
        problems=[LibraryProblemBrief(**p) for p in items], total=total
    )


@router.get("/{problem_id}", response_model=ProblemDetailResponse)
def detail(problem_id: str) -> ProblemDetailResponse:
    problem = store.get_problem(problem_id)
    if problem is None:
        raise HTTPException(status_code=404, detail="题目不存在")
    if problem.get("chain_id"):
        chain = chains.get_chain(problem["chain_id"])
        if chain:
            problem["chain_name"] = chain["name"]
    return ProblemDetailResponse(**problem)


@router.post("/{problem_id}/attempt", response_model=ProblemAttemptResponse)
def attempt(problem_id: str, req: ProblemAttemptRequest) -> ProblemAttemptResponse:
    try:
        result = checker.attempt(problem_id, req.coord)
    except checker.ProblemNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ProblemAttemptResponse(**result)


@router.post("/{problem_id}/explain", response_model=ProblemExplainResponse)
def explain(problem_id: str) -> ProblemExplainResponse:
    """练习深度讲解（同步讲棋分段：结果类型 + 脱先判断 + 分段变化）。

    KataGo 局部推演 + LLM 翻译成段；按题目缓存，命中不扣费。
    """
    try:
        result = explainer.explain_problem(problem_id)
    except explainer.ProblemNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except explainer.BudgetExceededError as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc
    except Exception as exc:  # 引擎启动失败 / LLM 失败
        raise HTTPException(status_code=502, detail=f"深度讲解生成失败: {exc}") from exc
    return ProblemExplainResponse(**result)


@router.post("/extract_from_review", response_model=ExtractFromReviewResponse)
def extract_from_review(req: ExtractFromReviewRequest) -> ExtractFromReviewResponse:
    """把复盘某一手所在局部截取为题目（讲解面板「收录本题」）。"""
    try:
        result = generator.extract_one(req.review_id, req.move_number)
    except generator.MoveNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except generator.SgfFileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except generator.VerifyFailedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # 引擎启动失败等
        raise HTTPException(status_code=502, detail=f"局部截取失败: {exc}") from exc
    return ExtractFromReviewResponse(**result)
