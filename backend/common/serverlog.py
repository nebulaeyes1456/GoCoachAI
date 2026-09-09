"""服务端文件日志（T0 可维护性基座）。

- 配置：``server.log_file``（默认 ``data/app.log``，相对路径按项目根解析）；
- 轮转：写入前若文件已超 5MB，先归档为 ``<name>.1``（覆盖旧备份）再新建；
- 关键事件：服务启动/停止、引擎探测结果、LLM 调用摘要、未处理错误；
- ``GET /api/v1/system/logs`` 读取最近 N 行（见 routers/system.py）。

无第三方依赖；写失败静默忽略（日志子系统不得拖垮业务）。
"""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from .settings import BASE_DIR, get_settings

DEFAULT_LOG_FILE = "data/app.log"
MAX_BYTES = 5 * 1024 * 1024      # 超过 5MB 轮转
BACKUP_SUFFIX = ".1"

_lock = threading.Lock()
_log_path: Path | None = None


def log_file() -> Path:
    """当前日志文件路径（进程级缓存；测试可调用 set_log_path 覆盖）。"""
    global _log_path
    with _lock:
        if _log_path is not None:
            return _log_path
    rel = str(
        (get_settings().get("server") or {}).get("log_file") or DEFAULT_LOG_FILE
    )
    p = Path(rel)
    if not p.is_absolute():
        p = BASE_DIR / p
    with _lock:
        _log_path = p
    return p


def set_log_path(path: str | Path | None) -> None:
    """强制指定日志路径（测试用）；传 None 重置为按配置解析。"""
    global _log_path
    with _lock:
        _log_path = None if path is None else Path(path)


def _rotate(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > MAX_BYTES:
            backup = path.with_name(path.name + BACKUP_SUFFIX)
            backup.unlink(missing_ok=True)
            path.replace(backup)
    except OSError:
        pass


def write(level: str, message: str) -> None:
    path = log_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            _rotate(path)
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with path.open("a", encoding="utf-8") as f:
                f.write(f"{ts} [{level}] {message}\n")
    except OSError:
        pass


def info(message: str) -> None:
    write("INFO", message)


def warning(message: str) -> None:
    write("WARN", message)


def error(message: str) -> None:
    write("ERROR", message)


def read_tail(lines: int = 200) -> list[str]:
    """读取最近 N 行日志（不存在则返回空列表）。"""
    path = log_file()
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return f.readlines()[-lines:]
    except OSError:
        return []
