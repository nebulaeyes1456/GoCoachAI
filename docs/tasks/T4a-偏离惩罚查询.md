# T4a 任务书：偏离惩罚查询（W1 引擎 · M2 前置）

> 为「每手一选讲解 + 变化必然性」提供数据：解释"对手为什么必须这么应——不应对的代价"。

## ① 做什么

在 `backend/services/engine/` 新建 `deviate.py`：

1. `query_deviation(setup_sgf, moves, pv, profile="standard", max_visits=200, timeout=...)` → 对 PV 每步计算偏离惩罚：
   - 输入：某手落下后的局面（setup_sgf + moves，复用 `analyze_sgf._game_to_query` 的构造方式）+ 该手的 KataGo PV（如 `["F5","D5","E3",...]`）；
   - 对 PV 中第 k 步（k=1..len(pv)，含义：局面按 PV 演进后，"轮到行棋的一方"若偏离 PV[k-1] 会损失多少）：
     - 构造"局面 + 已按 PV 走了前 k-1 步"的查询（低 visits）；
     - 从 `moveInfos` 取：最优手（order 0，应为 PV[k-1]）与一个"偏离手"（PV 之外的次优点或显式传入的偏离点）；
     - 计算 `winrate_loss`（最优 vs 偏离的胜率差，行棋方视角）与 `score_loss`（目数差）；
   - 输出 `list[DeviationPenalty]`：`{pv_step, pv_move, deviation_move, winrate_loss, score_loss, visits}`；
   - PV 中某步为 pass 时跳过该步。
2. 效率：一次函数调用内串行查询（复用 `KataGoEngine` 单实例）；仅对关键手由上层启用（本函数本身不做选点）。
3. 单元测试 `tests/test_deviate.py`：
   - mock `KataGoEngine.query` 构造响应，验证 winrate/score 差计算、方向（行棋方视角）、pass 跳过；
   - 1 个真实引擎轻量用例（9 路 1 手 PV 2 步，`skipUnless` 引擎存在），验证端到端可跑、单次 <30s。

## ② 禁止做什么

- 禁止新增 REST 端点（M2 的 T4b 再挂端点）；禁止改动 `docs/architecture.md`（契约文本放进你的报告，由我统一合并）；
- 禁止改动 `engine.py`/`analyze_sgf.py`/`verify.py` 的现有函数签名（只能 import 复用）；
- 禁止对每步跑高 visits（默认 200，上限由参数控制）；禁止真实调用 DeepSeek。

## ③ 验收方式

- `.\.venv\Scripts\python.exe -m unittest tests.test_deviate -v` 全绿（项目根 cwd）；
- 真实引擎用例输出打印 `[bench]` 耗时，单次 <30s；
- 回归：`.\.venv\Scripts\python.exe -m unittest discover -s tests` 原测试全绿（可只跑 engine/review 相关 + 你的新测试，避免全量引擎 e2e 太慢——自己权衡，但结论写进报告）。

## 报告

把完整报告写入 `docs/tasks/evidence/t4a-report.md`（改动文件清单、测试结果、偏离惩罚输出样例、API 签名与字段说明），最终回复简要引用。
