"""系统接口 /api/v1/system/*（窗口0 实现，T0 扩展运维/诊断端点）。

契约见 ``docs/architecture.md`` §4.4：
- GET  /api/v1/system/info
- PUT  /api/v1/system/settings
- GET  /api/v1/system/health          （T0 新增）
- GET  /api/v1/system/engine/status   （T0 新增）
- POST /api/v1/system/engine/restart  （T0 新增）
- GET  /api/v1/system/logs            （T0 新增）
- POST /api/v1/system/cache/clear     （T0 新增）
- POST /api/v1/system/db/backup       （T0 新增）
- GET  /api/v1/system/version         （T0 新增）

只增不改：原有 /info、/settings 的既有字段一律不动（仅 /info 增加
engine_backend/schema_version 两个新字段）。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from ..common import cost, db, serverlog, settings
from ..common.models import (
    CacheClearRequest,
    CacheClearResponse,
    DbBackupResponse,
    EngineRestartRequest,
    EngineRestartResponse,
    EngineStatusResponse,
    HealthCheckItem,
    HealthResponse,
    LogsResponse,
    SystemInfoResponse,
    SystemSettingsUpdate,
    VersionResponse,
)
from ..services.engine import backend as engine_backend

router = APIRouter(prefix="/api/v1/system", tags=["system"])

# 版本号（契约 §4.4）
VERSION = "1.7.2"

BACKUP_DIR = settings.BASE_DIR / "data" / "backups"

# 健康检查总体 ok 的判定：数据库、模型文件、DeepSeek key 三项；
# 引擎进程按需启动，未运行不算故障（engine_process 仅报告真实状态）。
_CRITICAL_CHECKS = ("database", "model_file", "deepseek_key")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _current_backend():
    """当前所选引擎后端（进程级缓存；探测/回退失败返回 None）。"""
    try:
        return engine_backend.choose_backend(persist=False)
    except Exception as exc:
        serverlog.error(f"[system] 获取引擎后端失败: {exc}")
        return None


def _info() -> SystemInfoResponse:
    s = settings.get_settings()
    choice = _current_backend()
    backend_name = choice.name if choice else ""
    engine_ready = engine_backend.engine_alive() if choice else False
    try:
        schema_ver = db.schema_version()
    except Exception:
        schema_ver = 0
    return SystemInfoResponse(
        version=VERSION,
        engine_ready=engine_ready,           # T1：真实引擎存活状态
        engine_backend=backend_name,          # T1：当前所选后端
        schema_version=schema_ver,            # T0：schema 版本
        model_ready=bool((s.get("coach") or {}).get("api_key")),  # 窗口2：DeepSeek key 已配置
        profile=s.get("review", {}).get("profile", "fast"),
        token_usage_month=round(cost.month_cost(), 4),  # 窗口2：coach_calls 月度聚合
        token_limit_month=float(s.get("budget", {}).get("token_limit_month", 30)),
    )


# ---------------------------------------------------------------------------
# 既有端点（契约 §4.4，不改字段）
# ---------------------------------------------------------------------------

@router.get("/info", response_model=SystemInfoResponse)
def system_info() -> SystemInfoResponse:
    """返回版本、引擎/模型就绪状态与 token 统计。"""
    return _info()


@router.put("/settings", response_model=SystemInfoResponse)
def update_settings(payload: SystemSettingsUpdate) -> SystemInfoResponse:
    """部分更新配置并写回 config.yaml，返回更新后的系统信息。"""
    updates: dict = payload.model_dump(exclude_none=True)
    # profile 对应 config 的 review.profile（§5）
    if "profile" in updates:
        updates.setdefault("review", {})["profile"] = updates.pop("profile")
    # blunder/question/good 阈值归入 review 段
    for key in ("blunder_threshold", "question_threshold", "good_threshold"):
        if key in updates:
            updates.setdefault("review", {})[key] = updates.pop(key)
    if updates:
        settings.save_settings(updates)
    return _info()


# ---------------------------------------------------------------------------
# T0 新增：运维/诊断端点
# ---------------------------------------------------------------------------

@router.get("/health", response_model=HealthResponse)
def system_health() -> HealthResponse:
    """四状态健康检查：数据库连通、引擎进程存活、模型文件存在、DeepSeek key 配置。"""
    checks: dict[str, HealthCheckItem] = {}

    # 1) 数据库连通
    try:
        conn = db.connect()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        checks["database"] = HealthCheckItem(
            ok=True, detail=f"connected（schema v{db.schema_version()}）"
        )
    except Exception as exc:
        checks["database"] = HealthCheckItem(ok=False, detail=str(exc))

    # 2) 引擎进程存活（真实状态；按需启动，未运行不算故障）
    alive = engine_backend.engine_alive()
    if alive:
        detail = f"进程存活（{engine_backend.alive_engine_count()} 个）"
    else:
        detail = "引擎进程未运行（首次分析时自动启动）"
    checks["engine_process"] = HealthCheckItem(ok=alive, detail=detail)

    # 3) 模型文件存在（按当前所选后端解析）
    choice = _current_backend()
    if choice is None:
        checks["model_file"] = HealthCheckItem(ok=False, detail="后端选择失败")
    elif Path(choice.model).exists():
        checks["model_file"] = HealthCheckItem(ok=True, detail=str(choice.model))
    else:
        checks["model_file"] = HealthCheckItem(
            ok=False, detail=f"模型文件不存在: {choice.model}"
        )

    # 4) DeepSeek key 配置（provider=ollama 时无需 key）
    s = settings.get_settings()
    coach_cfg = s.get("coach") or {}
    provider = str(coach_cfg.get("provider") or "deepseek")
    if provider == "ollama":
        checks["deepseek_key"] = HealthCheckItem(
            ok=True, detail="provider=ollama（本地推理，无需 key）"
        )
    elif coach_cfg.get("api_key"):
        checks["deepseek_key"] = HealthCheckItem(ok=True, detail="configured")
    else:
        checks["deepseek_key"] = HealthCheckItem(
            ok=False, detail="未配置 coach.api_key"
        )

    ok = all(checks.get(k).ok for k in _CRITICAL_CHECKS)
    return HealthResponse(status="ok" if ok else "degraded", checks=checks)


@router.get("/engine/status", response_model=EngineStatusResponse)
def engine_status() -> EngineStatusResponse:
    """引擎当前后端、进程状态与最近错误。"""
    choice = _current_backend()
    return EngineStatusResponse(
        backend=choice.name if choice else "",
        mode=choice.mode if choice else "auto",
        running=engine_backend.engine_alive(),
        process_count=engine_backend.alive_engine_count(),
        last_error=engine_backend.last_engine_error(),
    )


@router.post("/engine/restart", response_model=EngineRestartResponse)
def engine_restart(
    payload: EngineRestartRequest | None = None,
) -> EngineRestartResponse:
    """重启引擎；body 可选 {"backend": "cpu"} 临时切换后端（不写 config）。

    不带 body 时清除临时切换，回到 config 的 katago.backend 配置值。
    注意：分析引擎按需启动，重启实际效果是终止在册进程 + 重置后端选择。
    """
    try:
        requested = payload.backend if payload else None
        result = engine_backend.restart_engine(requested)
        serverlog.info(
            f"[system] 引擎重启完成: backend={result['backend']}"
            f"（mode={result['mode']}）"
        )
        return EngineRestartResponse(
            ok=True,
            backend=result["backend"],
            mode=result["mode"],
            message=result["reason"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        serverlog.error(f"[system] 引擎重启失败: {exc}")
        raise HTTPException(status_code=500, detail=f"引擎重启失败: {exc}") from exc


@router.get("/logs", response_model=LogsResponse)
def system_logs(lines: int = Query(default=200, ge=1, le=5000)) -> LogsResponse:
    """最近 N 行服务日志（server.log_file，默认 data/app.log）。"""
    log_path = serverlog.log_file()
    try:
        rel = str(log_path.relative_to(settings.BASE_DIR))
    except ValueError:
        rel = str(log_path)
    entries = serverlog.read_tail(lines)
    return LogsResponse(path=rel, lines=len(entries), log=entries)


@router.post("/cache/clear", response_model=CacheClearResponse)
def cache_clear(payload: CacheClearRequest) -> CacheClearResponse:
    """清讲解缓存（prompt 更新后强制刷新）。

    - kind=coach：清 explanations（讲解）+ coach_asks（答疑旁表）；
    - kind=review：清 moves（分析结果），并把 done 的复盘回到 pending
      （同一 SGF 再次提交会重新分析）；
    - kind=all：以上全部。
    """
    kind = (payload.kind or "all").lower()
    if kind not in ("coach", "review", "all"):
        raise HTTPException(
            status_code=400, detail="kind 必须是 coach | review | all"
        )
    cleared: dict[str, int] = {}
    conn = db.connect()
    try:
        if kind in ("coach", "all"):
            cleared["explanations"] = conn.execute(
                "DELETE FROM explanations"
            ).rowcount
            try:
                cleared["coach_asks"] = conn.execute(
                    "DELETE FROM coach_asks"
                ).rowcount
            except sqlite3.OperationalError:
                pass
        if kind in ("review", "all"):
            try:
                cleared["moves"] = conn.execute("DELETE FROM moves").rowcount
                cleared["reviews_reset"] = conn.execute(
                    "UPDATE reviews SET status='pending', progress=0.0,"
                    " error=NULL WHERE status='done'"
                ).rowcount
            except sqlite3.OperationalError:
                pass
        conn.commit()
    finally:
        conn.close()
    serverlog.info(f"[system] 缓存清理完成: kind={kind}, cleared={cleared}")
    return CacheClearResponse(kind=kind, cleared=cleared)


@router.post("/db/backup", response_model=DbBackupResponse)
def db_backup() -> DbBackupResponse:
    """SQLite 备份：VACUUM INTO data/backups/goapp-YYYYMMDD-HHMMSS.db。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stem = datetime.now().strftime("goapp-%Y%m%d-%H%M%S")
    path = BACKUP_DIR / f"{stem}.db"
    # 同秒冲突时追加序号
    suffix = 1
    while path.exists():
        path = BACKUP_DIR / f"{stem}-{suffix}.db"
        suffix += 1
    conn = db.connect()
    try:
        conn.execute("VACUUM INTO ?", (str(path),))
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=500, detail=f"备份失败: {exc}") from exc
    finally:
        conn.close()
    size = path.stat().st_size if path.exists() else 0
    try:
        rel = str(path.relative_to(settings.BASE_DIR))
    except ValueError:
        rel = str(path)
    serverlog.info(f"[system] 数据库备份完成: {rel}（{size} bytes）")
    return DbBackupResponse(path=rel, filename=path.name, size_bytes=size)


@router.get("/version", response_model=VersionResponse)
def system_version() -> VersionResponse:
    """版本 + 引擎后端 + schema 版本（前端"检查更新"用）。"""
    choice = _current_backend()
    try:
        schema_ver = db.schema_version()
    except Exception:
        schema_ver = 0
    return VersionResponse(
        version=VERSION,
        engine_backend=choice.name if choice else "",
        schema_version=schema_ver,
    )

