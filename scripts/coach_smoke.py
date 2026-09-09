"""窗口2 冒烟验证：用 mock 复盘数据跑 1 次 explain + 1 次 summary。

- 需要 backend/config.yaml 已填 DeepSeek api_key（勿在命令行传 key）；
- 小额成本，结果打印模型名 / 成本 / 字段摘要，供 docs/plan.md 记录；
- 用法：.venv\\Scripts\\python.exe scripts\\coach_smoke.py
"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from backend.common import cost  # noqa: E402
from backend.services.coach import service  # noqa: E402

REVIEW_ID = "mock-review-001"
MOVE = 37  # mock 数据中的坏手：黑 P11，胜率 -0.150，最佳点 P10


def main() -> int:
    print("=== coach smoke: explain ===")
    try:
        result = service.explain(REVIEW_ID, MOVE)
    except Exception as exc:  # noqa: BLE001
        print(f"explain 失败: {exc}")
        return 1
    content = result["content"]
    print(json.dumps({
        "kind": result["kind"],
        "model": result["model"],
        "cost": result["cost"],
        "problem": content.get("problem"),
        "recommendation": content.get("recommendation"),
        "variation": content.get("variation"),
        "takeaway": content.get("takeaway"),
    }, ensure_ascii=False, indent=2))

    print("\n=== coach smoke: summary ===")
    try:
        summary = service.summary(REVIEW_ID)
    except Exception as exc:  # noqa: BLE001
        print(f"summary 失败: {exc}")
        return 1
    print(json.dumps({
        "kind": summary["kind"],
        "model": summary["model"],
        "cost": summary["cost"],
        "opening": summary["content"].get("opening"),
        "middle": summary["content"].get("middle"),
        "weaknesses": summary["content"].get("weaknesses"),
        "suggestions": summary["content"].get("suggestions"),
    }, ensure_ascii=False, indent=2))

    print("\n=== 本月用量 ===")
    print(f"cost={cost.month_cost():.6f} 元, tokens={cost.month_tokens()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
