"""题目系统 REST 冒烟测试（窗口3，手动运行）。

前置：uvicorn backend.main:app --port 8765 已启动，且题库里已有题目
（先跑 tests 或 scripts/seed_basic.py / import_library.py 入库）。

用法：python scripts/problems_smoke.py
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8765/api/v1/problems"


def call(method: str, path: str, payload=None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return {"status": resp.status, "body": json.loads(resp.read())}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "body": json.loads(exc.read())}


def main() -> int:
    # 1) library 分页与筛选
    r = call("GET", "/library?limit=5")
    print("[1] library:", r["status"], "total =", r["body"].get("total"))
    assert r["status"] == 200, r
    total = r["body"]["total"]
    assert total > 0, "题库为空：先跑 seed_basic.py / import_library.py / tests"
    first = r["body"]["problems"][0]
    for key in ("id", "theme", "setup_sgf", "hint"):
        assert key in first, key

    # 2) 主题筛选
    r = call("GET", f"/library?theme={first['theme']}&limit=5")
    assert r["status"] == 200
    assert all(p["theme"] == first["theme"] for p in r["body"]["problems"])

    # 3) 详情
    pid = first["id"]
    r = call("GET", f"/{pid}")
    print("[2] detail:", r["status"], "answer =", r["body"].get("answer"))
    assert r["status"] == 200
    for key in ("answer", "branches", "verdict", "explanation"):
        assert key in r["body"], key

    # 4) 判题：正解
    answer = r["body"]["answer"]
    r = call("POST", f"/{pid}/attempt", {"coord": answer})
    print("[3] attempt(正解):", r["status"], r["body"])
    assert r["status"] == 200
    assert r["body"]["correct"] is True
    assert r["body"]["solved"] is True

    # 5) 判题：错解（找一个非正解的合法点）
    setup = first["setup_sgf"]
    import re  # noqa: E402

    board = 9 if "SZ[9]" in setup else 19
    from backend.common import sgf_io  # noqa: E402

    taken = set()
    for block in re.finditer(r"(?:AB|AW)((?:\[[a-zA-Z]*\])+)", setup):
        for m in re.finditer(r"\[([a-zA-Z]*)\]", block.group(1)):
            if m.group(1):
                taken.add(sgf_io.sgf_to_coord(m.group(1), board))
    for m in re.finditer(r";[BW]\[([a-zA-Z]{2})\]", setup):
        taken.add(sgf_io.sgf_to_coord(m.group(1), board))
    cols = "ABCDEFGHJKLMNOPQRST"[:board]
    wrong = None
    for y in range(1, board + 1):
        for x in cols:
            c = f"{x}{y}"
            if c.upper() != answer.upper() and c not in taken:
                wrong = c
                break
        if wrong:
            break
    if wrong:
        r = call("POST", f"/{pid}/attempt", {"coord": wrong})
        print("[4] attempt(错解):", r["status"], r["body"])
        assert r["status"] == 200
        assert r["body"]["correct"] is False

    # 6) 404 契约
    r = call("GET", "/p0000000000000000")
    assert r["status"] == 404, r
    r = call("POST", "/p0000000000000000/attempt", {"coord": "A1"})
    assert r["status"] == 404, r
    print("[5] 404 契约 OK")
    print("\n[smoke] 全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
