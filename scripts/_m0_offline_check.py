"""M0 无后端 + 多路数验证（临时脚本，验收后删除）。

页面为纯静态 ESM（任务书要求），Chrome 禁止 file:// 下的模块加载；
「无后端」的正确形态是任意静态 HTTP 服务（本脚本用 python -m http.server，非 FastAPI）。
验证：
1. 无后端静态服务下 mock 模式可跑（不依赖 fetch）；
2. 粘贴 19 路 SGF → 载入 → 棋盘尺寸自适应；
3. 13 路同理。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
PORT = 8766
BASE = f"http://127.0.0.1:{PORT}/"

SGF19 = (
    "(;GM[1]FF[4]CA[UTF-8]SZ[19]KM[7.5]PB[黑]PW[白]"
    ";B[pd];W[dp];B[pp];W[dd];B[fq];W[cn];B[qn];W[oq])"
)
SGF13 = "(;GM[1]FF[4]CA[UTF-8]SZ[13]KM[6.5];B[gg];W[fd];B[cc];W[kk])"


def wait_server(timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(BASE, timeout=1).close()
            return True
        except Exception:
            time.sleep(0.3)
    return False


def start_static_server() -> subprocess.Popen:
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = pathlib.Path(sys.executable)
    return subprocess.Popen(
        [str(python), "-m", "http.server", str(PORT), "--directory", str(FRONTEND)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )


def main() -> int:
    server = start_static_server()
    try:
        if not wait_server():
            raise RuntimeError("静态服务器未就绪")

        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception:
                browser = pw.chromium.launch(channel="msedge")
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))

            page.goto(BASE, wait_until="networkidle")
            page.wait_for_selector("#board-host canvas", timeout=15000)
            page.wait_for_function("document.body.dataset.ready === '1'", timeout=15000)
            label9 = page.text_content("#board-size-label").strip()
            ind9 = page.text_content("#move-indicator").strip()
            print(f"[check] 无后端静态服务（mock 模式）: label={label9}, {ind9}")

            page.fill("#sgf-input", SGF19)
            page.click("#btn-load")
            page.wait_for_timeout(600)
            label19 = page.text_content("#board-size-label").strip()
            ind19 = page.text_content("#move-indicator").strip()
            print(f"[check] 19 路 SGF: label={label19}, {ind19}")

            page.fill("#sgf-input", SGF13)
            page.click("#btn-load")
            page.wait_for_timeout(600)
            label13 = page.text_content("#board-size-label").strip()
            ind13 = page.text_content("#move-indicator").strip()
            print(f"[check] 13 路 SGF: label={label13}, {ind13}")

            print(f"[check] JS 错误: {errors if errors else '无'}")
            browser.close()

        ok = (
            "9 路" in label9 and "20 手" in ind9
            and "19 路" in label19 and "8 手" in ind19
            and "13 路" in label13 and "4 手" in ind13
            and not errors
        )
        print("RESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    sys.exit(main())
