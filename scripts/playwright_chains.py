"""题链 UI 验收（v1.7.0）· Playwright 脚本。

流程（Mock + 真实双模式）：
  1. 打开练习 tab → 点「🔗 题链」→ 链列表卡片截图（Mock：2 条演示链）；
  2. 点「托退定式」→ 竖向时间线（步序/目标/难度）截图；
  3. 点「▶ 连续练习」→ 进入做题页 → 断言来源徽标「托退定式 · 第 1 变」→ 截图；
  4. 答对第一变（Mock）→「下一题」→ 断言自动进入第 2 变（连续练习）→ 截图；
  5. 数据源切「真实后端」→ 题链列表（10 条定式种子）→ 截图；
  6. 打开一条真实链 → 时间线（题数为 0 时显示生长提示）→ 截图。

用法（项目根）：.venv\\Scripts\\python.exe scripts\\playwright_chains.py [--port 8766]
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "docs" / "tasks" / "evidence"


def wait_server(base: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base, timeout=1).close()
            return True
        except Exception:
            time.sleep(0.3)
    return False


def start_server(port: int) -> subprocess.Popen:
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = pathlib.Path(sys.executable)
    return subprocess.Popen(
        [str(python), "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8766)
    args = ap.parse_args()
    base = f"http://127.0.0.1:{args.port}/"
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    server = None
    if not wait_server(base, timeout=3):
        server = start_server(args.port)
        if not wait_server(base):
            raise RuntimeError(f"后端未在 {args.port} 端口就绪")

    from playwright.sync_api import sync_playwright

    failures: list[str] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        print(f"[{'OK' if cond else 'X'}] {label} {detail}")
        if not cond:
            failures.append(label)

    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception:
                print("[warn] chromium 启动失败，回退系统 Edge")
                browser = pw.chromium.launch(channel="msedge")
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            def _on_err(e):
                print("[pageerror]", e)
                print(getattr(e, "stack", ""))
            page.on("pageerror", _on_err)
            page.goto(base, wait_until="networkidle")
            page.wait_for_function(
                "document.body.dataset.ready === '1'", timeout=20000)

            def shot(name: str) -> None:
                path = EVIDENCE / name
                page.screenshot(path=str(path))
                print(f"[shot] {path}")

            # 0) Mock 数据源
            page.select_option("#source-select", "mock")
            page.wait_for_timeout(400)

            # 1) 练习 → 题链列表
            page.click("#tab-practice")
            page.wait_for_selector("#view-practice .problem-item", timeout=15000)
            page.click("#btn-open-chains")
            page.wait_for_selector(".chain-card", timeout=20000)
            cards = page.locator(".chain-card")
            check("Mock 题链卡片", cards.count() >= 2, f"count={cards.count()}")
            names = page.eval_on_selector_all(
                ".chain-card-name", "els => els.map(e => e.textContent.trim())")
            check("链名含托退定式", any("托退" in n for n in names), str(names))
            shot("v17-1-chains-mock.png")

            # 2) 链详情时间线
            cards.first.click()
            page.wait_for_selector(".chain-step", timeout=15000)
            steps = page.locator(".chain-step")
            check("链上步序数", steps.count() >= 2, f"count={steps.count()}")
            check("时间线文案", "第 1 变" in page.inner_text(".chain-timeline"))
            shot("v17-2-chain-timeline-mock.png")

            # 3) 连续练习 → 做题页 + 来源徽标
            page.click("#btn-chain-run")
            page.wait_for_selector("#practice-chain-badge", timeout=15000)
            badge = page.inner_text("#practice-chain-badge").strip()
            check("来源徽标", "第 1 变" in badge, badge)
            check("链内返回按钮文案",
                  "题链" in page.inner_text("#btn-practice-back"))
            shot("v17-3-chain-solve-badge-mock.png")

            # 4) 答对第一变 → 下一题 → 自动进入第 2 变
            page.wait_for_selector("#practice-board-host canvas", timeout=15000)
            for coord in ("F5",):  # mock-p1 正解
                box = page.evaluate(
                    "(() => { const c = document.querySelector('#practice-board-host canvas');"
                    " const r = c.getBoundingClientRect();"
                    " return {x: r.x, y: r.y, w: r.width, h: r.height}; })()")
                size = page.evaluate("Number(document.body.dataset.practiceSize)")
                col, row = coord[0], int(coord[1:])
                cols = "ABCDEFGHJKLMNOPQRST"
                x = box["x"] + box["w"] * (cols.index(col) + 0.5) / size
                y = box["y"] + box["h"] * (size - row + 0.5) / size
                page.mouse.click(x, y)
            page.wait_for_timeout(300)
            page.click("#btn-practice-judge")
            page.wait_for_function(
                "document.body.dataset.attemptCorrect === '1'", timeout=15000)
            page.click("#btn-practice-next")
            page.wait_for_function(
                "(() => { const b = document.querySelector('#practice-chain-badge');"
                " return b && b.textContent.includes('第 2 变'); })()", timeout=15000)
            check("连续练习自动进入下一变", True,
                  page.inner_text("#practice-chain-badge").strip())
            shot("v17-4-chain-continuous-mock.png")

            # 5) 真实后端：10 条定式种子链
            page.click("#tab-practice")
            page.select_option("#source-select", "real")
            page.wait_for_timeout(600)
            page.wait_for_selector("#btn-open-chains", timeout=15000)
            page.click("#btn-open-chains")
            page.wait_for_selector(".chain-card", timeout=20000)
            real_cards = page.locator(".chain-card").count()
            check("真实题链数 ≥ 10", real_cards >= 10, f"count={real_cards}")
            shot("v17-5-chains-real.png")

            # 6) 真实链详情
            page.locator(".chain-card").first.click()
            page.wait_for_timeout(800)
            shot("v17-6-chain-detail-real.png")
            check("真实链详情已渲染",
                  page.locator(".panel-title").first.inner_text() != "")

            browser.close()
    finally:
        if server is not None:
            server.terminate()

    print(f"\n== 验收结果：{'全部通过' if not failures else '失败 ' + str(failures)} ==")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
