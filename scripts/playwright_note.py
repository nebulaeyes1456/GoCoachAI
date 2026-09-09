"""记谱视图（对弈记录 + 胜率 + 试下研究）· Playwright 自检脚本。

流程（Mock，不烧 API）：
  1. 切「记谱」tab → 空盘 9 路；
  2. 落 7 手（第 7 手提白 E4）→ 计数/提子/胜率自动分析；
  3. 悔棋 → 6 手、白 E4 回来、胜率同步；
  4. 浏览历史（第 4 手）；
  5. 试下研究：落子 → 退出恢复主线；
  6. 保存 SGF（下载+剪贴板兜底 toast）→ 载入 SGF 继续记录；
  7. 复盘回归：切回复盘 tab，关键手/试摆仍正常（复盘功能不动）。

用法（项目根，需后端已运行 8765）：
  .venv\\Scripts\\python.exe scripts\\playwright_note.py
"""
from __future__ import annotations

import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "docs" / "tasks" / "evidence"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}/"


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    try:
        urllib.request.urlopen(BASE, timeout=3).close()
    except Exception:
        print(f"[FAIL] 后端未运行：{BASE}")
        return 1

    from playwright.sync_api import sync_playwright

    results: list[tuple[str, bool, str]] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        results.append((name, cond, detail))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(800)

        # 1) 切记谱（默认 19 路）
        page.locator("#tab-note").click()
        page.wait_for_timeout(700)
        check(
            "记谱-默认19路初始化",
            page.locator("#note-board-host canvas").count() > 0
            and "第 0 / 0 手" in page.locator("#note-move-indicator").inner_text()
            and page.locator("#note-size-label").inner_text().strip() == "19 路",
            page.locator("#note-size-label").inner_text(),
        )

        # 2) 落 7 手（第 7 手提白 E4）——19 路棋盘几何
        host = page.locator("#note-board-host")
        box = host.bounding_box()
        for x, y in [(4, 4), (4, 5), (3, 5), (1, 4), (5, 5), (1, 5), (4, 6)]:
            click_point(page, "#note-board-host", x, y, size=19)
        page.wait_for_timeout(700)
        check(
            "记谱-落子计数",
            "第 7 / 7 手" in page.locator("#note-move-indicator").inner_text(),
            "7 手记录（19 路）",
        )
        check(
            "记谱-提子",
            grid_point(page, "#note-board-host", 4, 5, size=19) == ".",
            "白 E4 被提",
        )
        check(
            "记谱-胜率自动分析",
            "胜率" in page.locator("#note-winrate-text").inner_text()
            and "黑方胜率" in page.locator("#note-curve-label").inner_text(),
            page.locator("#note-winrate-text").inner_text(),
        )
        page.screenshot(path=str(EVIDENCE / "note-01-play.png"))

        # 3) 悔棋
        page.locator("#view-note button", has_text="悔棋").click()
        page.wait_for_timeout(500)
        check(
            "记谱-悔棋",
            "第 6 / 6 手" in page.locator("#note-move-indicator").inner_text()
            and grid_point(page, "#note-board-host", 4, 5, size=19) == "O",
            "白 E4 恢复、胜率同步",
        )
        page.screenshot(path=str(EVIDENCE / "note-02-undo.png"))

        # 4) 浏览历史（第 4 手）
        page.locator("#note-move-strip .move-btn").nth(3).click()
        page.wait_for_timeout(400)
        check(
            "记谱-浏览历史",
            "第 4 / 6 手" in page.locator("#note-move-indicator").inner_text(),
            page.locator("#note-winrate-text").inner_text(),
        )

        # 5) 试下研究
        page.locator("#view-note button", has_text="试下").click()
        page.wait_for_timeout(300)
        click_point(page, "#note-board-host", 8, 4, size=19)
        page.wait_for_timeout(300)
        check(
            "记谱-试下研究",
            page.evaluate("() => document.body.dataset.noteTrial") == "1",
            "研究子已摆",
        )
        page.screenshot(path=str(EVIDENCE / "note-03-trial.png"))
        page.locator("#view-note button", has_text="退出试下").click()
        page.wait_for_timeout(400)
        check(
            "记谱-退出试下恢复",
            "第 4 / 6 手" in page.locator("#note-move-indicator").inner_text(),
            "主线局面恢复",
        )

        # 6) 保存 + 载入
        page.locator('#view-note button[title="到结尾"]').click()
        page.wait_for_timeout(300)
        page.locator("#view-note button", has_text="保存棋谱").click()
        page.wait_for_timeout(600)
        check(
            "记谱-保存SGF",
            page.evaluate(
                "() => [...document.querySelectorAll('.goc-toast')].some(t => t.textContent.includes('已下载'))"
            ),
            "下载 + 剪贴板兜底",
        )
        page.locator("#view-note button", has_text="复制 SGF").click()
        page.wait_for_timeout(500)
        check(
            "记谱-复制SGF",
            page.evaluate(
                "() => [...document.querySelectorAll('.goc-toast')].some(t => t.textContent.includes('复制'))"
            ),
            "剪贴板写入",
        )
        # 载入 3 手棋谱
        page.locator("#note-sgf-input").fill(
            "(;GM[1]FF[4]SZ[9]KM[6.5]PB[黑方]PW[白方];B[ee];W[eg];B[gf])"
        )
        page.locator("#view-note button", has_text="载入继续记录").click()
        page.wait_for_timeout(700)
        check(
            "记谱-载入SGF",
            "第 3 / 3 手" in page.locator("#note-move-indicator").inner_text()
            and page.evaluate("() => document.body.dataset.noteCount") == "3",
            "载入后自动分析",
        )
        page.screenshot(path=str(EVIDENCE / "note-04-loaded.png"))

        # 7) 复盘回归（复盘功能不动）
        page.locator("#tab-review").click()
        page.wait_for_timeout(600)
        check(
            "复盘-回归（关键手）",
            page.locator(".keymove-item").count() == 3,
            "关键手 3 条",
        )
        page.locator("#view-review button", has_text="试摆").first.click()
        page.wait_for_timeout(300)
        host2 = page.locator("#board-host")
        box2 = host2.bounding_box()
        W2 = page.evaluate("() => document.querySelector('#board-host canvas').clientWidth")
        l2, s2 = 3 * W2 / 38, 4 * W2 / 38
        page.mouse.click(box2["x"] + l2 + 4 * s2, box2["y"] + l2 + 5 * s2)
        page.wait_for_timeout(300)
        t1 = page.evaluate("() => document.body.dataset.trialStones")
        page.locator("#view-review button", has_text="退出试摆").first.click()
        page.wait_for_timeout(400)
        check(
            "复盘-回归（试摆）",
            t1 == "1" and page.evaluate("() => document.body.dataset.trialStones") == "0",
            "试摆落子/退出恢复正常",
        )

        browser.close()

    failed = [r for r in results if not r[1]]
    print(f"\n共 {len(results)} 项断言，失败 {len(failed)} 项")
    return 1 if failed else 0


def grid_point(page, host_sel, x, y, size=9):
    """读 (x, y) 交点：'X' 黑 / 'O' 白 / '.' 空（合并所有棋子层像素采样）。

    WGo 几何：fieldWidth = 4W/(4*size+2)，left = 3W/(4*size+2)。
    """
    return page.evaluate(
        """([sel, x, y, size]) => {
        const cvs = [...document.querySelectorAll(sel + ' canvas')].filter(
          (c) => c.style.zIndex === '300'
        );
        const cv0 = document.querySelector(sel + ' canvas');
        const W = cv0.clientWidth;
        const dpr = cv0.width / W;
        const L = (3 * W / (4 * size + 2)) * dpr;
        const S = (4 * W / (4 * size + 2)) * dpr;
        const cx = Math.round(L + x * S);
        const cy = Math.round(L + y * S);
        let dark = 0, light = 0;
        for (const cv of cvs) {
          const d = cv.getContext('2d').getImageData(cx - 8, cy - 8, 16, 16).data;
          for (let p = 0; p < d.length; p += 4) {
            if (d[p + 3] > 128) {
              const lum = 0.299 * d[p] + 0.587 * d[p + 1] + 0.114 * d[p + 2];
              if (lum < 80) dark++; else if (lum > 170) light++;
            }
          }
        }
        return dark > 40 ? 'X' : light > 40 ? 'O' : '.';
      }""",
        [host_sel, x, y, size],
    )


def click_point(page, host_sel, x, y, size=9):
    """点击棋盘 (x, y) 交点（CSS 坐标由 WGo 几何公式换算）。"""
    box = page.locator(host_sel).bounding_box()
    W = page.evaluate(
        "(sel) => document.querySelector(sel + ' canvas').clientWidth",
        host_sel,
    )
    left = 3 * W / (4 * size + 2)
    sp = 4 * W / (4 * size + 2)
    page.mouse.click(box["x"] + left + x * sp, box["y"] + left + y * sp)
    page.wait_for_timeout(200)


if __name__ == "__main__":
    sys.exit(main())
