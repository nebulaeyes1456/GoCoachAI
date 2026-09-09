# T4a 偏离惩罚查询 —— 实施报告

> 任务书：`docs/tasks/T4a-偏离惩罚查询.md`（W1 引擎 · M2 前置）
> 完成日期：2026-08-31
> 状态：✅ 完成并验收通过

## ① 改动文件清单

| 文件 | 操作 | 说明 |
| --- | --- | --- |
| `backend/services/engine/deviate.py` | 修改 | 偏离惩罚查询实现（修正查询构造为"局面 + moves + PV 前 k-1 步"） |
| `tests/test_deviate.py` | 修改 | mock 单测 + 真实引擎轻量用例 |
| `docs/tasks/evidence/t4a-report.md` | 新增 | 本报告 |

未改动：`engine.py` / `analyze_sgf.py` / `verify.py`（仅 import 复用）、`docs/architecture.md`（契约文本在本报告 §③，由统一合并）、无新增 REST 端点、未真实调用 DeepSeek。

## ② API 签名与字段说明

```python
from backend.services.engine.deviate import query_deviation, DeviationPenalty

query_deviation(setup_sgf: str,
                moves: list[list[str]],      # 该手之前的行棋序列 [[color, coord], ...]
                pv: list[str],               # 该手 KataGo 后续变化（行棋方视角）
                profile: str = "standard",
                max_visits: int | None = 200,
                timeout: float = 600.0,
                ) -> list[DeviationPenalty]
```

`DeviationPenalty` dataclass 字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `pv_step` | `int` | PV 序号（1 基，对应输入 `pv` 的第 k 项） |
| `pv_move` | `str` | 该步 PV 着点（界面坐标大写；pass 步不产出） |
| `deviation_move` | `str \| None` | 偏离手（引擎次优，界面坐标大写；pass 记 `""`） |
| `winrate_loss` | `float \| None` | 最优手 − 偏离手的胜率差（行棋方视角，正值=偏离吃亏） |
| `score_loss` | `float \| None` | 最优手 − 偏离手的目数差（同视角） |
| `visits` | `int \| None` | 该查询搜索量（`rootInfo.visits`） |
| `error` | `str \| None` | 该步查询失败/无偏离手原因；`None` 表示正常 |

**语义与口径**（对 PV 第 k 步，k=1..len(pv)）：

1. 查询局面 = `setup_sgf` 局面 + `moves` + PV 前 k-1 步（不含 PV[k-1] 本身），
   `analyzeTurns=[n]`（n=查询 moves 列表末手序号，与 `verify.py` 同法）。
2. 从 `moveInfos` 取 `order==0`（最优手，应为 PV[k-1]）与 `order==1`（偏离手；
   缺失时取列表首个非最优手）。
3. 口径：cfg 固定 `reportAnalysisWinratesAs = SIDETOMOVE`，`moveInfos` 的
   `winrate`/`scoreLead` 与 `rootInfo` 均为查询局面**行棋方视角**
   （2026-08-31 真实引擎实测确认），因此：
   - `winrate_loss = best.winrate − deviation.winrate`（正值=偏离吃亏）；
   - `score_loss = best.scoreLead − deviation.scoreLead`。
4. PV 中 pass 步跳过（pass 仍计入后续步骤的局面演进）；单步查询失败记入该步
   `error` 字段并继续，不整体抛异常；串行查询，单次调用复用同一引擎实例。

## ③ 测试结果

**`.venv\Scripts\python.exe -m unittest tests.test_deviate -v` → 8 项全绿（共 5.3s）**

- 单元测试（mock 引擎，7 项）：差值方向/口径（0.62−0.55=0.07 胜率、3.0−(−1.0)=4.0 目）、
  pass 跳过与 `pv_step` 映射、maxVisits 覆盖（默认 200）、order 0 不在首位时以引擎返回为准、
  偏离手更好时差值为负、单步查询失败降级不阻断后续、空 PV/全 pass、无偏离手降级。
- 真实引擎（1 项，opencl，9 路 SGF 10 手、PV 3 步、maxVisits=200）：
  **`[bench] 偏离惩罚查询耗时 3.3s`（<30s 达标）**，返回 3 条非空、数值合理。

**回归**：`tests.test_review tests.test_problems_checker -v` → **21 项全绿（20.6s，无新增失败）**。

## ④ 真实输出样例

```
[bench] 偏离惩罚查询耗时 3.3s
[sample] [
  {"pv_step": 1, "pv_move": "C6", "deviation_move": "C5",
   "winrate_loss": 0.1959, "score_loss": 4.169, "visits": 210, "error": null},
  {"pv_step": 2, "pv_move": "F4", "deviation_move": "E3",
   "winrate_loss": 0.0220, "score_loss": 0.446, "visits": 211, "error": null},
  {"pv_step": 3, "pv_move": "G5", "deviation_move": "C4",
   "winrate_loss": 0.0239, "score_loss": 2.106, "visits": 211, "error": null}
]
```

解读：第 1 步若偏离 C6（改走 C5）损失 19.6% 胜率、约 4.2 目；后两步偏离惩罚
较小（0.02~0.024 胜率），符合 9 路 10 手棋局中"关键手集中"的预期。

## ⑤ 遗留事项

1. `deviation_move` 可能等于 `pv_move`：对深层 PV 步以满 visits 重搜该局面时，
   引擎最优手可能与原 PV 续着不一致（原 PV 步落到 order 1 成为"偏离手"）。属
   正常现象，上层讲解服务应容忍（`winrate_loss` 仍以 order 0 为基准计算）。
2. `deviation_move` 首选 `order==1`；若其为最优手的对称点（winrate 相同），
   `winrate_loss=0`。上层如需"最差明显偏离点"，可自行筛选（本函数按任务书
   只取次优点）。
3. T4b 挂 REST 端点时，建议由上层按"仅关键手启用 + maxVisits≤200"调用，避免
   整局逐手查询的开销。
