"""棋手档案 / 棋谱库 / 水平画像接口 /api/v1/progress/*（成长视图）。

- POST   /profiles            创建档案
- GET    /profiles            档案列表
- DELETE /profiles/{id}       删除档案
- GET    /profiles/{id}       档案详情（档案 + 棋谱 + 画像缓存）
- POST   /profiles/{id}/attach      归档已分析的复盘
- POST   /profiles/{id}/import       导入外部 SGF（自动复盘分析并归档）
- POST   /profiles/{id}/insight      计算画像（纯 KataGo 统计，无 LLM 成本）
- POST   /profiles/{id}/advice       AI 提高建议（LLM，按档案缓存）
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..common.models import (
    PlayerProfile,
    ProfileAdviceRequest,
    ProfileAttachRequest,
    ProfileCreateRequest,
    ProfileDetailResponse,
    ProfileGameBrief,
    ProfileImportRequest,
    ProfileImportResponse,
    ProfileInsightResponse,
    ProfileListResponse,
)
from ..services.progress import service

router = APIRouter(prefix="/api/v1/progress", tags=["progress"])


@router.post("/profiles", response_model=PlayerProfile)
def create_profile(req: ProfileCreateRequest) -> PlayerProfile:
    if not (req.name or "").strip():
        raise HTTPException(status_code=400, detail="档案名不能为空")
    return PlayerProfile(**service.create(req.name, req.note))


@router.get("/profiles", response_model=ProfileListResponse)
def list_profiles() -> ProfileListResponse:
    return ProfileListResponse(
        profiles=[PlayerProfile(**p) for p in service.list_all()]
    )


@router.delete("/profiles/{profile_id}")
def delete_profile(profile_id: str) -> dict:
    service.delete(profile_id)
    return {"deleted": True}


@router.get("/profiles/{profile_id}", response_model=ProfileDetailResponse)
def profile_detail(profile_id: str) -> ProfileDetailResponse:
    try:
        detail = service.detail(profile_id)
    except service.ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ProfileDetailResponse(
        profile=PlayerProfile(**detail["profile"]),
        games=[ProfileGameBrief(**g) for g in detail["games"]],
        insight=detail["insight"],
    )


@router.post("/profiles/{profile_id}/attach", response_model=dict)
def attach(profile_id: str, req: ProfileAttachRequest) -> dict:
    try:
        return service.attach(req.profile_id, req.review_id)
    except service.ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.ReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/profiles/{profile_id}/import",
             response_model=ProfileImportResponse)
def import_sgf(profile_id: str, req: ProfileImportRequest) -> ProfileImportResponse:
    if not (req.sgf_text or "").strip():
        raise HTTPException(status_code=400, detail="SGF 内容为空")
    try:
        result = service.import_sgf(
            profile_id, req.sgf_text, req.review_profile)
    except service.ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"SGF 导入失败: {exc}") from exc
    return ProfileImportResponse(**result)


@router.post("/profiles/{profile_id}/insight",
             response_model=ProfileInsightResponse)
def insight(profile_id: str, force: bool = False) -> ProfileInsightResponse:
    try:
        result = service.insight(profile_id, force=force)
    except service.ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.NoGamesError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ProfileInsightResponse(**result)


@router.post("/profiles/{profile_id}/advice",
             response_model=ProfileInsightResponse)
def advice(profile_id: str) -> ProfileInsightResponse:
    try:
        result = service.advice(profile_id)
    except service.ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.NoGamesError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"建议生成失败: {exc}") from exc
    return ProfileInsightResponse(**result)
