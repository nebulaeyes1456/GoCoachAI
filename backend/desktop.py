"""桌面启动器（窗口4，T7 商业化发布版）。

职责（商业化发布形态的第一层）：
1. 解析用户数据目录（Windows: %APPDATA%\\GoCoachAI，其余 ~/.local/share/gocoachai），
   创建目录并设置环境变量 ``GOCOACH_DATA_DIR``——此后 DB/配置/日志/题库全部
   落在用户目录，**升级安装程序不丢数据**（settings.py 配合）；
2. 单实例锁（127.0.0.1:8766）：第二个实例直接退出并提示；
3. 启动后端：检测 8765 端口，已占用则复用现有服务（浏览器/命令行启动的实例）；
   未占用则在线程内启动 uvicorn（复用 ``backend.main:app``）；
4. 首启自检：等待服务就绪后请求 /system/health 与 /system/info，把
   引擎后端（opencl/eigenavx2）、模型、DeepSeek 状态写入启动日志；
5. 打开内置 WebView2 窗口指向 ``http://127.0.0.1:8765``；
6. 异常兜底：--windowed 下无 stdout，错误写入数据目录 logs/launcher.log。

打包入口：PyInstaller 打本文件（见 ``scripts/build_desktop.py``）。
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

# PyInstaller --windowed（runw.exe）下 sys.stdout/sys.stderr 为 None，
# print() 会抛 AttributeError 导致启动中断；这里兜底为 devnull。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

# 项目根目录：开发模式（python backend/desktop.py）与 PyInstaller 冻结后
# （__file__ 指向 _MEIPASS/backend/desktop.pyc）都能正确解析 backend 包。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HOST = "127.0.0.1"
PORT = 8765
LOCK_PORT = 8766          # 单实例锁端口
URL = f"http://{HOST}:{PORT}/"


# ---------------------------------------------------------------------------
# 用户数据目录
# ---------------------------------------------------------------------------

def user_data_dir() -> Path:
    """用户数据目录：Windows %APPDATA%\\GoCoachAI，其他 ~/.local/share/gocoachai。"""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        d = Path(base) / "GoCoachAI"
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        d = Path(base) / "gocoachai"
    for sub in ("", "library", "backups", "sgfs", "logs"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    # 在 import backend 之前设置：settings.DATA_DIR 解析自此
    os.environ.setdefault("GOCOACH_DATA_DIR", str(d))
    return d


_DATA_DIR = user_data_dir()
_LAUNCHER_LOG = _DATA_DIR / "logs" / "launcher.log"


def migrate_seed_library() -> None:
    """首启迁移内置题库：程序目录 data/library → 用户数据目录 library（幂等）。"""
    seed = ROOT / "data" / "library"
    if not seed.exists() or not any(seed.iterdir()):
        return
    target = _DATA_DIR / "library"
    try:
        target.mkdir(parents=True, exist_ok=True)
        copied = 0
        for src in seed.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(seed)
            dst = target / rel
            if dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            import shutil as _shutil
            _shutil.copy2(src, dst)
            copied += 1
        if copied:
            launcher_log(f"题库迁移：从程序目录复制 {copied} 个文件到 {target}")
    except OSError as exc:
        launcher_log(f"题库迁移失败（忽略）: {exc}")


def launcher_log(text: str) -> None:
    print(f"[launcher] {text}")
    try:
        with _LAUNCHER_LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 单实例锁
# ---------------------------------------------------------------------------

def acquire_single_instance() -> socket.socket | None:
    """绑定 127.0.0.1:8766 作为实例锁；失败返回 None（已有实例在运行）。

    注意：Windows 下 SO_REUSEADDR 会允许第二个 socket 绑定同一地址，
    因此不设置该选项，依赖系统默认"端口占用即失败"语义。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((HOST, LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def start_backend() -> bool:
    """确保后端服务在 8765 端口可用。返回是否为本进程新建的服务。"""
    if port_in_use(HOST, PORT):
        launcher_log(f"端口 {PORT} 已有服务，直接复用：{URL}")
        return False

    try:
        import uvicorn

        from backend.main import app
    except Exception as exc:
        launcher_log(f"backend 导入失败: {type(exc).__name__}: {exc}")
        raise

    def run() -> None:
        try:
            uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
        except Exception as exc:
            launcher_log(f"uvicorn 启动失败: {type(exc).__name__}: {exc}")
            raise

    threading.Thread(target=run, name="goc-backend", daemon=True).start()

    # 等待服务就绪（最多 30 秒；首次可能包含建表）
    for _ in range(300):
        if port_in_use(HOST, PORT):
            launcher_log(f"内置服务已就绪：{URL}")
            return True
        time.sleep(0.1)
    raise RuntimeError(
        "内置服务启动超时（30 秒）。请确认依赖安装完整："
        "pip install -r backend/requirements.txt"
    )


def startup_selfcheck() -> None:
    """首启自检：请求 /system/info 与 /system/health，写入启动日志。"""
    try:
        with urllib.request.urlopen(f"{URL}api/v1/system/info", timeout=10) as r:
            info = __import__("json").loads(r.read().decode("utf-8"))
        with urllib.request.urlopen(f"{URL}api/v1/system/health", timeout=10) as r:
            health = __import__("json").loads(r.read().decode("utf-8"))
    except Exception as exc:
        launcher_log(f"自检请求失败（可忽略，页面会再提示）: {exc}")
        return
    backend_name = info.get("engine_backend") or "未探测"
    model_ready = "已配置" if info.get("model_ready") else "未配置"
    checks = health.get("checks") or {}
    parts = [f"{k}={ 'ok' if v.get('ok') else 'failed' }"
             for k, v in checks.items()]
    launcher_log(
        f"自检: 版本 v{info.get('version')} | 引擎后端={backend_name} | "
        f"模型/API={model_ready} | {', '.join(parts)}"
    )


def main() -> None:
    launcher_log(f"启动器开始（数据目录 {_DATA_DIR}）")

    lock = acquire_single_instance()
    if lock is None:
        launcher_log("已有实例在运行，本实例退出")
        return

    try:
        migrate_seed_library()
        start_backend()
        startup_selfcheck()

        try:
            import webview
        except ImportError:
            launcher_log("未安装 pywebview（pip install -r requirements-desktop.txt）")
            launcher_log(f"后端服务已在运行，请用浏览器打开 {URL}")
            sys.exit(1)

        webview.create_window(
            "弈友 · GoCoachAI 围棋教练",
            URL + "?prod=1",
            width=1280,
            height=840,
            min_size=(1000, 680),
        )
        webview.start()
    finally:
        try:
            lock.close()
        except OSError:
            pass
        launcher_log("启动器退出")


if __name__ == "__main__":
    main()
