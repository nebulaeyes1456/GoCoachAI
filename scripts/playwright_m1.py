"""M1 复盘页真实数据实装 · Playwright 自检脚本（窗口4 验收）。

流程：
  1. 启动后端（.venv uvicorn，8765，cwd=项目根）；
  2. 清 review 缓存（保证本次为全新分析，能拍到进度条，脚本可重复执行）；
  3. 打开 http://127.0.0.1:8765/ → 数据源切到「真实后端」→ 载入 9 路示例；
  4. 截图：分析中进度条 → done 后真实曲线 → 关键手定位+棋盘一选标记 → 讲解面板；
  5. 讲解按钮走拦截（explain 返回 502），验证降级提示（¥0 成本，不烧 DeepSeek）；
  6. 设置面板：真实 T0 数据（版本/schema/健康）+ clearCache toast；
  7. 切回 Mock 做 M0 回归：mock 曲线/关键手/讲解文本；
  8. 截图保存到 docs/tasks/evidence/，末尾关闭后端。

用法（项目根）：.venv\\Scripts\\python.exe scripts\\playwright_m1.py
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "docs" / "tasks" / "evidence"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}/"

ANALYZE_TIMEOUT_MS = 900_000  # 首次 OpenCL 启动可能较慢（内核缓存未热时）


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
    return subprocess.Popen(
        [
            str(python), "-m", "uvicorn", "backend.main:app",
            "--host", "127.0.0.1", "--port", str(PORT),
        ],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def clear_review_cache() -> None:
    """清 review 缓存：done 复盘回到 pending，同 SGF 重载会重新分析。"""
    req = urllib.request.Request(
        BASE + "api/v1/system/cache/clear",
        data=json.dumps({"kind": "review"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        print(f"[cache] review 缓存已清: {body}")


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    server = start_server()
    try:
        if not wait_server():
            raise RuntimeError(f"后端未在 {PORT} 端口就绪")
        clear_review_cache()

        from playwright.sync_api import sync_playwright

        shots = []
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception:
                print("[warn] playwright chromium 启动失败，回退系统 Edge")
                browser = pw.chromium.launch(channel="msedge")

            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.on("pageerror", lambda e: print("[pageerror]", e))

            page.goto(BASE, wait_until="networkidle")
            page.wait_for_selector("#board-host canvas", timeout=15000)
            page.wait_for_function("document.body.dataset.ready === '1'", timeout=15000)
            page.wait_for_timeout(400)

            def shot(name: str) -> None:
                path = EVIDENCE / name
                page.screenshot(path=str(path))
                shots.append(path)
                print(f"[shot] {path}")

            # ---- 1) 数据源 → 真实后端，载入 9 路示例，拍分析进度 ----
            page.select_option("#source-select", "mock")  # 复位（防 localStorage 残留）
            page.wait_for_timeout(500)
            page.select_option("#source-select", "real")
            page.wait_for_selector("#review-progress", timeout=15000)
            page.wait_for_timeout(600)
            shot("m1-1-analyzing.png")
            print(f"[check] 分析中进度: {page.text_content('#review-progress-text').strip()}")

            # ---- 2) 等待 done → 真实曲线 ----
            page.wait_for_function(
                "document.body.dataset.reviewStatus === 'done'",
                timeout=ANALYZE_TIMEOUT_MS,
            )
            page.wait_for_selector("#review-status-done", timeout=15000)
            page.wait_for_timeout(800)
            key_count = page.locator(".keymove-item").count()
            chips = [el.text_content().strip() for el in page.query_selector_all(".stat-chip")]
            print(f"[check] 分析完成: {page.text_content('#review-status-done').strip()}")
            print(f"[check] 关键手条数={key_count}, 统计徽章={chips}")
            assert key_count > 0, "真实模式关键手列表为空！"
            shot("m1-2-real-curve.png")

            # ---- 3) 点击关键手 → 一选/落点标记 + 讲解面板 ----
            page.locator(".keymove-item").first.click()
            page.wait_for_selector("#explain-panel", timeout=10000)
            page.wait_for_function(
                "(document.body.dataset.markers || '0') !== '0'", timeout=8000
            )
            page.wait_for_timeout(500)
            print(f"[check] 棋盘标记数={page.evaluate('document.body.dataset.markers')}")
            meta = page.text_content("#explain-panel .explain-meta").strip().replace("\n", " ")
            print(f"[check] 讲解面板元信息: {meta[:120]}")
            shot("m1-3-keymove-markers.png")

            # ---- 4) AI 讲解（拦截 explain → 502，验证降级提示，¥0 不烧 DeepSeek） ----
            page.route(
                "**/api/v1/coach/explain",
                lambda route: route.fulfill(
                    status=502,
                    content_type="application/json",
                    body='{"detail":"讲解服务异常: LLM 未配置（M2 接入）"}',
                ),
            )
            page.click("#btn-explain")
            page.wait_for_selector("#explain-fallback", timeout=15000)
            fallback = page.text_content("#explain-fallback").strip()
            print(f"[check] 讲解降级提示: {fallback}")
            assert "讲解暂不可用" in fallback, "降级提示文案不正确！"
            shot("m1-4-explain-fallback.png")
            page.unroute("**/api/v1/coach/explain")

            # ---- 5) 设置面板：真实 T0 数据 + clearCache toast ----
            page.click("#btn-settings")
            page.wait_for_selector("#sys-version", timeout=10000)
            page.wait_for_timeout(800)
            ver = page.text_content("#sys-version").strip()
            schema = page.text_content("#sys-schema").strip()
            health = page.text_content("#health-status").strip()
            print(f"[check] 设置面板: 版本={ver}, schema={schema}, 健康={health}")
            assert "占位" not in ver, "设置面板版本仍为占位文本！"
            page.click("#btn-cache-coach")  # 清讲解缓存（无害，验证 clearCache + toast）
            page.wait_for_selector(".goc-toast", timeout=10000)
            page.wait_for_timeout(300)
            shot("m1-5-settings.png")
            page.click(".goc-modal-close")
            page.wait_for_timeout(300)

            # ---- 6) Mock 回归：M0 行为不变 ----
            page.select_option("#source-select", "mock")
            page.wait_for_timeout(600)
            page.click("#btn-sample")
            page.wait_for_function("document.body.dataset.ready === '1'", timeout=15000)
            page.wait_for_timeout(600)
            page.click("#btn-last")
            page.wait_for_timeout(500)
            indicator = page.text_content("#move-indicator").strip()
            mock_chips = [el.text_content().strip() for el in page.query_selector_all(".stat-chip")]
            mock_key_count = page.locator(".keymove-item").count()
            print(f"[check] Mock 回归: {indicator}, 徽章={mock_chips}, 关键手={mock_key_count}")
            assert "20/20" in indicator.replace(" ", ""), "Mock 定位结尾失败！"
            assert mock_key_count == 3, "Mock 关键手数量异常（期望 3）！"
            shot("m1-6-mock-regression.png")

            # ---- 7) Mock 讲解文本 ----
            page.locator(".keymove-item").first.click()
            page.wait_for_selector("#explain-panel", timeout=10000)
            page.click("#btn-explain")
            page.wait_for_selector("#explain-panel .takeaway-box", timeout=10000)
            page.wait_for_timeout(400)
            print("[check] Mock 讲解文本已渲染")
            shot("m1-7-mock-explain.png")

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
