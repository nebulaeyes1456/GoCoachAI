"""M0 棋盘命门 · Playwright 自检脚本（窗口4 验收，铁律2：截图验收）。

流程：
  1. 启动后端（.venv 的 uvicorn，端口 8765，cwd=项目根）；
  2. 打开 http://127.0.0.1:8765/，等待 WGo 棋盘渲染完成；
  3. 截图：初始 → 点击第 12 手定位 → 播放 3 秒 → 定位到结尾；
  4. 截图保存到 docs/tasks/evidence/；
  5. 脚本末尾关闭后端进程。

用法（项目根目录）：
  .venv\\Scripts\\python.exe scripts\\playwright_m0.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "docs" / "tasks" / "evidence"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}/"


def wait_server(timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(BASE, timeout=1).close()
            return True
        except Exception:
            time.sleep(0.3)
    return False


def start_server() -> subprocess.Popen:
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = pathlib.Path(sys.executable)
    proc = subprocess.Popen(
        [
            str(python), "-m", "uvicorn", "backend.main:app",
            "--host", "127.0.0.1", "--port", str(PORT),
        ],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return proc


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    server = start_server()
    try:
        if not wait_server():
            raise RuntimeError(f"后端未在 {PORT} 端口就绪（uvicorn 启动失败？）")

        from playwright.sync_api import sync_playwright

        shots = []
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception:
                print("[warn] playwright chromium 启动失败，回退系统 Edge")
                browser = pw.chromium.launch(channel="msedge")

            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.on("console", lambda m: None)
            page.on(
                "pageerror",
                lambda e: print("[pageerror]", e),
            )

            page.goto(BASE, wait_until="networkidle")
            # 等待 WGo 棋盘 canvas 渲染 + 页面自报 ready（app.js 设置 body[data-ready]=1）
            page.wait_for_selector("#board-host canvas", timeout=15000)
            page.wait_for_function(
                "document.body.dataset.ready === '1'", timeout=15000
            )
            page.wait_for_timeout(400)

            def shot(name: str) -> None:
                path = EVIDENCE / name
                page.screenshot(path=str(path))
                shots.append(path)
                print(f"[shot] {path}")

            # 1) 初始（空棋盘 + 完整 UI）
            shot("m0-1-initial.png")

            # 2) 点击第 12 手定位（手数列表 data-move=12）
            page.click("[data-move='12']")
            page.wait_for_timeout(500)
            indicator = page.text_content("#move-indicator")
            print(f"[check] 点击第 12 手后指示器: {indicator}")
            shot("m0-2-move12.png")

            # 3) 播放 3 秒（自动播放推进手数）
            page.click("#btn-play")
            page.wait_for_timeout(3000)
            indicator = page.text_content("#move-indicator")
            print(f"[check] 播放 3 秒后指示器: {indicator}")
            shot("m0-3-playing.png")
            page.click("#btn-play")  # 暂停

            # 4) 定位到结尾（全谱落子状态）
            page.click("#btn-last")
            page.wait_for_timeout(500)
            indicator = page.text_content("#move-indicator")
            print(f"[check] 定位结尾后指示器: {indicator}")
            shot("m0-4-final.png")

            browser.close()

        print(f"OK: 共 {len(shots)} 张截图 -> {EVIDENCE}")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
        print("[done] 后端进程已关闭")


if __name__ == "__main__":
    sys.exit(main())
