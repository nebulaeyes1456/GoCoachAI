"""PV 变化逐步播放 · Playwright 自检脚本（可视化功能验收）。

流程（全部 Mock，不烧 API）：
  1. 复盘：点第 7 手关键手 → 讲解面板 → 变化播放（0/3）；
  2. 步进到 2/3、3/3，截图棋盘逐步摆出 PV；
  3. 自动播放（▶）跑完，⏮ 回开头；
  4. 退出播放 → 恢复第 7 手；
  5. 练习：第 1 题试下（点 E5 错招）→ 判定 → 变化 F5 H2 G5 播放 → 退出恢复；
  6. 截图保存到 docs/tasks/evidence/pv-*.png；末尾打印断言摘要。

用法（项目根，需后端已运行 8765）：
  .venv\\Scripts\\python.exe scripts\\playwright_pv.py
"""
from __future__ import annotations

import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "docs" / "tasks" / "evidence"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}/"


def stone_pixels(page, host_sel):
    """棋子层 canvas 的黑/白像素数（>0 表示有棋子渲染）。"""
    return page.evaluate(
        """(sel) => {
        const cvs = [...document.querySelectorAll(sel + ' canvas')];
        let dark = 0, light = 0;
        cvs.forEach(cv => {
          if (cv.style.zIndex !== '300') return;
          const d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;
          let ld = 0, ll = 0;
          for (let p = 0; p < d.length; p += 4) {
            if (d[p + 3] > 128) {
              const lum = 0.299 * d[p] + 0.587 * d[p + 1] + 0.114 * d[p + 2];
              if (lum < 80) ld++; else if (lum > 170) ll++;
            }
          }
          dark = Math.max(dark, ld); light = Math.max(light, ll);
        });
        return { dark, light };
      }""",
        host_sel,
    )


def grid_point(page, host_sel, x, y):
    """读 (x, y) 交点上的子：'X' 黑 / 'O' 白 / '.' 空（像素采样，合并所有棋子层）。"""
    return page.evaluate(
        """([sel, x, y]) => {
        const cvs = [...document.querySelectorAll(sel + ' canvas')].filter(
          (c) => c.style.zIndex === '300'
        );
        const cv0 = document.querySelector(sel + ' canvas');
        const W = cv0.clientWidth;
        const dpr = cv0.width / W;
        const L = (3 * W / 38) * dpr;
        const S = (4 * W / 38) * dpr;
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
        [host_sel, x, y],
    )


def click_point(page, host_sel, x, y):
    """点击棋盘 (x, y) 交点（CSS 坐标由棋盘几何公式换算）。"""
    page.evaluate(
        "(sel) => document.querySelector(sel).scrollIntoView({block: 'center'})",
        host_sel,
    )
    page.wait_for_timeout(250)
    box = page.locator(host_sel).bounding_box()
    W = page.evaluate(
        "(sel) => document.querySelector(sel + ' canvas').clientWidth",
        host_sel,
    )
    left = 3 * W / 38
    sp = 4 * W / 38
    page.mouse.click(box["x"] + left + x * sp, box["y"] + left + y * sp)
    page.wait_for_timeout(200)


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

        # ---------- 复盘：第 7 手 → 讲解面板 ----------
        page.locator(".keymove-item").filter(has_text="第 7 手").first.click()
        page.wait_for_timeout(600)
        check(
            "复盘-讲解面板打开",
            page.locator("#explain-panel").count() == 1,
            "第 7 手疑问手",
        )
        check(
            "复盘-变化播放按钮",
            page.locator("#btn-pv-play").count() == 1,
            "PV H6 H5 J5 H4 H3 J3 J2（7 步）",
        )
        page.screenshot(path=str(EVIDENCE / "pv-01-review-explain.png"))

        # ---------- 变化播放：0/3 → 2/3 → 3/3 ----------
        page.locator("#btn-pv-play").click()
        page.wait_for_timeout(500)
        check(
            "复盘-pv-bar 浮层出现",
            page.locator("#pv-bar").count() == 1,
            page.locator("#pv-progress").inner_text(),
        )
        page.screenshot(path=str(EVIDENCE / "pv-02-review-start.png"))

        for _ in range(2):
            page.locator("#pv-bar button").nth(3).click()
            page.wait_for_timeout(250)
        page.wait_for_timeout(300)
        check(
            "复盘-步进 2/7",
            "2 / 7" in page.locator("#pv-progress").inner_text(),
            "棋盘逐步摆出 PV 第 1-2 手",
        )
        page.screenshot(path=str(EVIDENCE / "pv-03-review-step2.png"))

        page.locator("#pv-bar button").nth(3).click()
        page.wait_for_timeout(300)
        check(
            "复盘-步进 3/7",
            "3 / 7" in page.locator("#pv-progress").inner_text(),
            "PV 继续摆出",
        )
        page.screenshot(path=str(EVIDENCE / "pv-04-review-step3.png"))

        # ---------- 自动播放：⏮ 回开头 → ▶ ----------
        page.locator("#pv-bar button").nth(0).click()
        page.wait_for_timeout(400)
        page.locator("#pv-bar button").nth(2).click()
        page.wait_for_timeout(5600)  # 7 步 × 700ms + 余量
        check(
            "复盘-自动播放跑完",
            "7 / 7" in page.locator("#pv-progress").inner_text(),
            "700ms/步自动步进",
        )

        # ---------- 退出播放 → 恢复第 7 手 ----------
        page.locator("#pv-bar button").last.click()
        page.wait_for_timeout(600)
        check(
            "复盘-退出后恢复",
            page.locator("#pv-bar").count() == 0
            and "第 7 / 20 手" in page.locator("#move-indicator").inner_text(),
            page.locator("#move-indicator").inner_text(),
        )
        page.screenshot(path=str(EVIDENCE / "pv-05-review-restored.png"))

        # ---------- 复盘：试摆落子显示（WGo type 陷阱回归断言） ----------
        # 注意：当前定位在第 7 手局面，中心 E5 已有黑子；点中心偏下一格的
        # 空点（E4）验证落子显示。
        page.locator("button", has_text="试摆").first.click()
        page.wait_for_timeout(400)
        host = page.locator("#board-host")
        box = host.bounding_box()
        spacing = box["height"] / 9
        page.mouse.click(
            box["x"] + box["width"] / 2, box["y"] + box["height"] / 2 + spacing
        )
        page.wait_for_timeout(300)
        px = stone_pixels(page, "#board-host")
        check(
            "复盘-试摆落子显示",
            (px["dark"] + px["light"]) > 0
            and page.evaluate("() => document.body.dataset.trialStones") == "1",
            f"黑子像素 {px['dark']} 白子像素 {px['light']}",
        )
        page.screenshot(path=str(EVIDENCE / "pv-08-trial-stone.png"))
        page.locator("button", has_text="退出试摆").first.click()
        page.wait_for_timeout(400)
        check(
            "复盘-退出试摆恢复",
            page.evaluate("() => document.body.dataset.trial") == "0"
            and page.evaluate("() => document.body.dataset.trialStones") == "0"
            and "第 7 / 20 手"
            in page.locator("#move-indicator").inner_text(),
            "试摆子已清除、定位保持第 7 手",
        )

        # ---------- 复盘：试摆提子（WGo.Game 判定） ----------
        # 先回到第 0 手（空盘黑先），再进入试摆。
        page.locator("button").filter(has_text="⏮").first.click()
        page.wait_for_timeout(400)
        # B E5, W E4, B D4, W B5, B F4, W B6, B E3 → 提白 E4
        if page.evaluate("() => document.body.dataset.trial") == "1":
            page.locator("button", has_text="退出试摆").first.click()
            page.wait_for_timeout(300)
        page.locator("button", has_text="试摆").first.click()
        page.wait_for_timeout(300)
        for x, y in [(4, 4), (4, 5), (3, 5), (1, 4), (5, 5), (1, 5), (4, 6)]:
            click_point(page, "#board-host", x, y)
        page.wait_for_timeout(300)
        check(
            "复盘-试摆提子",
            page.evaluate("() => document.body.dataset.trialStones") == "6"
            and grid_point(page, "#board-host", 4, 5) == "."
            and grid_point(page, "#board-host", 1, 4) == "O",
            "E4 白被提、trialStones=6",
        )
        page.screenshot(path=str(EVIDENCE / "pv-09-trial-capture.png"))
        # 自杀拦截：退出重进 → B A8, W B8, B B9, W A9（自杀）
        page.locator("button", has_text="退出试摆").first.click()
        page.wait_for_timeout(300)
        page.locator("button", has_text="试摆").first.click()
        page.wait_for_timeout(300)
        for x, y in [(0, 1), (1, 1), (1, 0), (0, 0)]:
            click_point(page, "#board-host", x, y)
        page.wait_for_timeout(300)
        check(
            "复盘-试摆自杀拦截",
            page.evaluate("() => document.body.dataset.trialStones") == "3",
            f"trialStones={page.evaluate('() => document.body.dataset.trialStones')}",
        )
        check(
            "复盘-试摆自杀点为空",
            grid_point(page, "#board-host", 0, 0) == ".",
            f"A9={grid_point(page, '#board-host', 0, 0)}",
        )
        check(
            "复盘-试摆自杀提示",
            page.evaluate(
                "() => [...document.querySelectorAll('.goc-toast')].some(t => t.textContent.includes('非法落子'))"
            ),
            page.evaluate(
                "() => [...document.querySelectorAll('.goc-toast')].map(t => t.textContent).join('|')"
            ),
        )
        # 退出试摆（清场）
        page.locator("button", has_text="退出试摆").first.click()
        page.wait_for_timeout(400)

        # ---------- 练习：第 1 题试下错招 → 变化播放 ----------
        page.locator("#tab-practice").click()
        page.wait_for_timeout(600)
        page.locator(".problem-item").first.click()
        page.wait_for_timeout(600)
        host = page.locator("#practice-board-host")
        box = host.bounding_box()
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.wait_for_timeout(500)
        px = stone_pixels(page, "#practice-board-host")
        check(
            "练习-试下落子显示",
            px["dark"] > 0 and px["light"] > 0,
            f"题面+试下子 黑{px['dark']} 白{px['light']}",
        )
        page.locator("#btn-practice-judge").click()
        page.wait_for_timeout(1600)
        check(
            "练习-判定+变化出现",
            page.locator("#btn-pv-practice").count() == 1,
            page.locator("#practice-feedback-box code").first.inner_text(),
        )
        page.locator("#btn-pv-practice").click()
        page.wait_for_timeout(500)
        page.locator("#pv-bar button").nth(3).click()
        page.wait_for_timeout(250)
        page.locator("#pv-bar button").nth(3).click()
        page.wait_for_timeout(300)
        check(
            "练习-变化播放 2/5",
            "2 / 5" in page.locator("#pv-progress").inner_text(),
            "题面 + 正解变化逐步摆出（5 步变化）",
        )
        page.screenshot(path=str(EVIDENCE / "pv-06-practice-step2.png"))

        page.locator("#pv-bar button").last.click()
        page.wait_for_timeout(600)
        check(
            "练习-退出后恢复",
            page.locator("#pv-bar").count() == 0
            and not page.locator("#btn-practice-judge").is_disabled(),
            "试下摆子保留、判定可用",
        )
        page.screenshot(path=str(EVIDENCE / "pv-07-practice-restored.png"))

        # ---------- 练习：试下提子（黑先 7 手 → 提白 C4+D4） ----------
        page.locator("#btn-practice-reset").click()
        page.wait_for_timeout(300)
        for x, y in [(3, 4), (1, 0), (2, 6), (0, 0), (2, 4), (0, 2), (1, 5)]:
            click_point(page, "#practice-board-host", x, y)
        page.wait_for_timeout(300)
        check(
            "练习-试下提子",
            grid_point(page, "#practice-board-host", 2, 5) == "."
            and grid_point(page, "#practice-board-host", 3, 5) == "."
            and page.evaluate("() => document.body.dataset.practiceTrial") == "7",
            "白 C4+D4 被提、试下子保留",
        )
        page.screenshot(path=str(EVIDENCE / "pv-10-practice-capture.png"))
        page.locator("#btn-practice-reset").click()
        page.wait_for_timeout(300)
        check(
            "练习-重摆恢复题面",
            grid_point(page, "#practice-board-host", 2, 5) == "O"
            and grid_point(page, "#practice-board-host", 3, 5) == "O"
            and page.evaluate("() => document.body.dataset.practiceTrial") == "0",
            "C4+D4 白子恢复",
        )

        browser.close()

    failed = [r for r in results if not r[1]]
    print(f"\n共 {len(results)} 项断言，失败 {len(failed)} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
