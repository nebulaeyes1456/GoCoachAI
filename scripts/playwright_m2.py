"""M2「三合一 SPA + 练习试下 + 答疑 + 档位选择」· Playwright 验收脚本（窗口4）。

流程（全部在同一个 index.html 内完成，不新建 html）：
  1. 启动后端（.venv uvicorn，8765，cwd=项目根）；
  2. 打开 http://127.0.0.1:8765/ → 顶栏切「练习」tab → 截图（Mock 题库列表）；
  3. 数据源切「真实后端」→ 题库刷新为真实题库（含官子谱/生成题）→ 截图；
  4. 选中一题 → WGo 题面棋盘 + 轮到谁 → 截图；
  5. 切回 Mock → 选中 mock 9 路题 → 点击棋盘落子试下 → 截图；
  6. 「判定」→ 错误反馈截图（mock 判定无需引擎，秒回）；
  7. 连错 3 次后「显示答案」→ 正解标记截图；
  8. 切「答疑」tab（真实源）→ 拦截 coach/ask 返回 502 → 降级提示截图
     （route 拦截，¥0 成本，不烧 DeepSeek）；
  9. 打开设置 → 档位下拉（fast/standard/fine 含耗时标注）→ 切 fine → 截图；
 10. 切回「复盘」tab（Mock）→ 载入 9 路示例 → 截图（复盘功能回归正常）；
 11. 复盘「试摆」：进入试摆模式点击棋盘摆子 → 截图 → 退出恢复；
 12. 脚本末尾恢复档位 fast 并关闭后端。

用法（项目根）：.venv\\Scripts\\python.exe scripts\\playwright_m2.py
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


def wait_server(timeout: float = 40.0) -> bool:
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


def put_settings(profile: str) -> None:
    """直接走 PUT /api/v1/system/settings 恢复档位（与前端一致）。"""
    req = urllib.request.Request(
        BASE + "api/v1/system/settings",
        data=json.dumps({"profile": profile}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            print(f"[settings] 档位已恢复 {body.get('profile')}")
    except Exception as exc:
        print(f"[warn] 恢复档位失败: {exc}")


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    server = start_server()
    try:
        if not wait_server():
            raise RuntimeError(f"后端未在 {PORT} 端口就绪")

        from playwright.sync_api import sync_playwright

        shots: list[pathlib.Path] = []
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
            page.wait_for_timeout(300)

            def shot(name: str) -> None:
                path = EVIDENCE / name
                page.screenshot(path=str(path))
                shots.append(path)
                print(f"[shot] {path}")

            # ---- 0) 复位数据源为 Mock（防 localStorage 残留） ----
            page.select_option("#source-select", "mock")
            page.wait_for_timeout(600)

            # ---- 1) 顶栏切「练习」tab：Mock 题库列表 ----
            page.click("#tab-practice")
            page.wait_for_selector("#view-practice .problem-item", timeout=15000)
            page.wait_for_function(
                "document.body.dataset.practiceCount !== undefined", timeout=5000
            )
            mock_count = page.evaluate("document.body.dataset.practiceCount")
            print(f"[check] Mock 题库 {mock_count} 题（顶栏练习 tab 已切换）")
            shot("m2-1-practice-list-mock.png")

            # ---- 2) 数据源切「真实后端」：真实题库（官子谱/生成题数据） ----
            page.select_option("#source-select", "real")
            page.wait_for_function(
                "(() => { const el = document.querySelector('#view-practice .problem-item');"
                " return el && el.dataset.problemId !== 'mock-p1'; })()",
                timeout=30000,
            )
            page.wait_for_timeout(500)
            real_count = page.evaluate("document.body.dataset.practiceCount")
            first_id = page.evaluate(
                "document.querySelector('#view-practice .problem-item').dataset.problemId"
            )
            print(f"[check] 真实题库 {real_count} 题，第一题 id={first_id}")
            shot("m2-2-practice-list-real.png")

            # ---- 3) 选中一题：题面棋盘 + 轮到谁 ----
            page.locator("#view-practice .problem-item").first.click()
            page.wait_for_selector("#practice-board-host canvas", timeout=15000)
            page.wait_for_function(
                "document.body.dataset.practiceSize !== undefined", timeout=8000
            )
            solver = page.evaluate("document.body.dataset.practiceSolver")
            size = page.evaluate("document.body.dataset.practiceSize")
            print(f"[check] 题面 {size} 路，轮到 {'白' if solver == 'W' else '黑'} 方")
            page.wait_for_timeout(500)
            shot("m2-3-problem-real.png")

            # ---- 4) 切回 Mock：选 9 路 mock 题 + 点击棋盘试下 ----
            page.select_option("#source-select", "mock")
            page.wait_for_function(
                "(() => { const el = document.querySelector('#view-practice .problem-item');"
                " return el && el.dataset.problemId === 'mock-p1'; })()",
                timeout=15000,
            )
            page.locator("#view-practice .problem-item").first.click()
            page.wait_for_selector("#practice-board-host canvas", timeout=10000)
            page.wait_for_function(
                "document.body.dataset.practiceSize === '9'", timeout=8000
            )
            box = page.locator("#practice-board-host canvas").last.bounding_box()
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.wait_for_function(
                "document.body.dataset.practiceTrial === '1'", timeout=5000
            )
            print("[check] 已点击棋盘落子试下（WGo 官方 click 交互）")
            shot("m2-4-practice-trial.png")

            # ---- 5) 「判定」→ 错误反馈 ----
            page.click("#btn-practice-judge")
            page.wait_for_selector("#practice-feedback", timeout=15000)
            fb = page.text_content("#practice-feedback").strip()
            print(f"[check] 判定反馈: {fb}")
            assert "不对" in fb, "Mock 判定应为错误反馈！"
            shot("m2-5-judge-wrong.png")

            # ---- 6) 再错两次 → 「显示答案」解锁 → 正解标记 ----
            for i in range(2):
                page.wait_for_function(
                    "!document.querySelector('#btn-practice-judge').disabled",
                    timeout=15000,
                )
                page.click("#btn-practice-judge")
                page.wait_for_selector("#practice-feedback", timeout=15000)
            page.wait_for_function(
                "!document.querySelector('#btn-practice-answer').disabled",
                timeout=10000,
            )
            page.click("#btn-practice-answer")
            page.wait_for_selector("#practice-answer-text", timeout=8000)
            page.wait_for_timeout(400)
            ans = page.text_content("#practice-answer-text").strip()
            print(f"[check] 显示答案: {ans}")
            shot("m2-6-show-answer.png")

            # ---- 7) 「答疑」tab（真实源 + route 拦截 502，¥0 不烧 DeepSeek） ----
            page.select_option("#source-select", "real")
            page.click("#tab-ask")
            page.wait_for_selector("#ask-question", timeout=8000)
            page.route(
                "**/api/v1/coach/ask",
                lambda route: route.fulfill(
                    status=502,
                    content_type="application/json",
                    body='{"detail":"讲解服务异常: DeepSeek API key 未配置"}',
                ),
            )
            page.fill("#ask-question", "这一步为什么不好？接下来应该往哪里下？")
            page.fill("#ask-sgf", "")
            page.click("#btn-ask-submit")
            page.wait_for_selector("#ask-fallback", timeout=15000)
            fallback = page.text_content("#ask-fallback").strip()
            print(f"[check] 答疑降级提示: {fallback}")
            assert "答疑暂不可用" in fallback, "降级提示文案不正确！"
            shot("m2-7-ask-fallback.png")
            page.unroute("**/api/v1/coach/ask")

            # ---- 8) 设置面板：档位下拉 + 切 fine ----
            page.click("#btn-settings")
            page.wait_for_function(
                "(() => { const el = document.querySelector('#sys-version');"
                " return el && el.textContent.trim() !== ''; })()",
                timeout=15000,
            )
            page.wait_for_timeout(600)
            cur = page.text_content("#cur-profile").strip()
            print(f"[check] 设置面板当前档位: {cur}")
            shot("m2-8-settings-profile.png")
            page.select_option("#sel-profile", "fine")
            page.wait_for_function(
                "document.querySelector('#cur-profile').textContent.trim() === 'fine'",
                timeout=10000,
            )
            page.wait_for_timeout(400)
            print("[check] 档位已切换为 fine（PUT /api/v1/system/settings）")
            shot("m2-9-settings-fine.png")
            page.click(".goc-modal-close")
            page.wait_for_timeout(300)

            # ---- 9) 复盘回归（Mock）：载入 9 路示例 ----
            page.select_option("#source-select", "mock")
            page.click("#tab-review")
            page.wait_for_timeout(500)
            page.click("#btn-sample")
            page.wait_for_function("document.body.dataset.ready === '1'", timeout=15000)
            page.wait_for_timeout(500)
            indicator = page.text_content("#move-indicator").strip()
            print(f"[check] 复盘回归: {indicator}")
            assert "20" in indicator, "复盘手数定位异常！"
            shot("m2-10-review-mock.png")

            # ---- 10) 复盘试摆：进入 → 摆子 → 截图 → 退出恢复 ----
            page.click("#btn-trial")
            page.wait_for_function("document.body.dataset.trial === '1'", timeout=5000)
            box = page.locator("#board-host canvas").last.bounding_box()
            placed = False
            for fx, fy in [
                (0.5, 0.5), (0.35, 0.25), (0.2, 0.2), (0.8, 0.8),
                (0.15, 0.85), (0.85, 0.15), (0.3, 0.7), (0.7, 0.3),
            ]:
                page.mouse.click(
                    box["x"] + box["width"] * fx, box["y"] + box["height"] * fy
                )
                try:
                    page.wait_for_function(
                        "parseInt(document.body.dataset.trialStones || '0') > 0",
                        timeout=700,
                    )
                    placed = True
                    break
                except Exception:
                    continue
            assert placed, "试摆模式未能摆上任何棋子！"
            print("[check] 复盘试摆已摆子")
            shot("m2-11-review-trial.png")
            page.click("#btn-trial")  # 退出试摆，恢复 KifuReader 定位
            page.wait_for_function("document.body.dataset.trial === '0'", timeout=5000)
            print("[check] 已退出试摆恢复棋谱定位")

            browser.close()

        print(f"OK: 共 {len(shots)} 张截图 -> {EVIDENCE}")
        return 0
    finally:
        put_settings("fast")  # 恢复档位，避免污染用户配置
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
        print("[done] 后端进程已关闭")


if __name__ == "__main__":
    sys.exit(main())
