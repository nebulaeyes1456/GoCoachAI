"""FastAPI 入口（窗口0 搭骨架）。

- 挂载各 router（review/coach/problems 为窗口1/2/3 占位）；
- ``GET /`` 等挂载 ``frontend/`` 静态文件；
- 启动时执行建表；
- 端口固定 8765（契约 §2）。

启动：``uvicorn backend.main:app --port 8765``（项目根目录执行）。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .common import db, serverlog
from .common.settings import FRONTEND_DIR, get_settings
from .routers import coach, play, problems, progress, review, system
from .services.engine import backend as engine_backend
from .services.review import service as review_service


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动时执行 §3 全部建表 SQL（幂等）+ 增量迁移
    db.init_db()
    serverlog.info(
        f"[server] 服务启动 v{system.VERSION}，schema v{db.schema_version()}"
    )
    # T1：启动时探测引擎后端（auto 时写入 katago.detected_backend，进程级缓存）
    try:
        choice = engine_backend.choose_backend(persist=True)
        serverlog.info(f"[server] 引擎后端: {choice.name}（{choice.reason}）")
    except Exception as exc:
        serverlog.error(f"[server] 引擎后端探测失败: {exc}")
    # 复盘任务队列（窗口1）：单工作线程
    review_service.get_service().start()
    review.ws_manager.bind_loop(asyncio.get_running_loop())
    yield
    serverlog.info("[server] 服务停止")
    review_service.get_service().stop()


app = FastAPI(title="GoCoachAI", version="1.7.0", lifespan=lifespan)

# API 路由（§4）
app.include_router(system.router)
app.include_router(review.router)
app.include_router(review.ws_router)
app.include_router(coach.router)
app.include_router(play.router)
app.include_router(problems.router)
app.include_router(progress.router)

# 前端静态资源（§2：后端挂载 frontend/）
app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIR / "assets")), name="assets")
app.mount("/js", StaticFiles(directory=str(FRONTEND_DIR / "js")), name="js")
app.mount("/css", StaticFiles(directory=str(FRONTEND_DIR / "css")), name="css")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/practice.html", include_in_schema=False)
def practice_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "practice.html")


@app.get("/ask.html", include_in_schema=False)
def ask_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "ask.html")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    """未处理异常：记录到服务日志后重新抛出（不改变原有 500 行为）。"""
    serverlog.error(
        f"[server] 未处理异常 {request.method} {request.url.path}: {exc}"
    )
    raise exc


if __name__ == "__main__":
    import uvicorn

    port = int(get_settings().get("server", {}).get("port", 8765))
    uvicorn.run("backend.main:app", host="127.0.0.1", port=port)
