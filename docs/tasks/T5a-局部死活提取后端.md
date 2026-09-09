# T5a 任务书：局部死活提取（后端 · M3 核心管线）

> 从任意棋局扫描未定型区域 → 死活/对杀识别 → 多分支验题 → 入库。前端练习页下轮做（T5b）。

## ① 做什么

在 `backend/services/problems/` 新建 `extractor.py` + 端点 `POST /api/v1/problems/extract`：

1. **输入**：`{sgf_text | review_id, max_problems, target_rank}`；有 sgf_text 时先走复盘管线（复用 `review.service.submit` 轮询 done，或直接 `analyze_sgf`）拿每手数据。
2. **扫描未定型区域**（"高变化量"判定）：
   - 信号：某手 delta 大（复用 moves.category）；或该手 `moveInfos` 前几名 winrate 接近（候选多）；或终局附近（手数 > endgame_move_fraction）仍有 winrate 波动；
   - 对每处信号取"局面 + 中心点"→ `crop_radius` 裁剪（复用 `utils` 的裁剪/坐标工具，保留完整棋串）。
3. **成题 + 验题**（复用 v1 规则，见 `generator.py` 注释）：
   - 候选点 = 中心 + 半径 1 空点 + 该局面 KataGo 一选（best_coord），死活/对杀加 "pass" 检验紧迫性；
   - `verify_position` 批量验证：正解 > 0.95 且次优 < 0.3 才入库；死活/对杀还要求 pass 后胜率 < `urgency_max_winrate`；
   - 主题用 `utils.classify_theme`；难度用 `utils.difficulty_from_gap`；题 id = 题面哈希（幂等，重复提取不重复入库）；`store.add_problem` 入库（INSERT OR IGNORE）。
4. **端点**：`routers/problems.py` 增 `POST /api/v1/problems/extract`（response：`{extracted: [...brief], failed: n, skipped: n}`），并发保护（引擎单进程，同一时间只跑一个提取任务或直接同步阻塞 + 简单锁）。
5. **契约**：更新 `docs/architecture.md` §4.3 增补 extract 端点契约与字段示例（只增不改）。
6. **测试** `tests/test_extractor.py`：mock verify_position 的管线单测（扫描/裁剪/过滤/幂等/入库）；1 个真实 9 路 e2e（skipUnless 引擎存在）验证端到端 ≥1 题。

## ② 禁止做什么

- 禁止改 v1 §4.3 已有字段；禁止为凑题数放宽验题阈值（提取率低不丢人）；
- 禁止改动 `verify.py`/`checker.py` 现有函数签名；
- 禁止真实调用 DeepSeek；禁止前端改动（练习页下轮 T5b）。

## ③ 验收方式

- `.\.venv\Scripts\python.exe -m unittest tests.test_extractor -v` 全绿（项目根 cwd）；
- 真实 9 路 e2e 打印 `[bench]` 耗时与产出题数（目标 ≤ 3 分钟，≥1 题）；
- curl 验证端点 200 且字段完整（可配合 TestClient 单测）；
- 回归：全量 `unittest discover` 中受影响的 tests 不新增失败（全量跑引擎 e2e 很慢，可只跑你涉及的模块 + 原 problems 测试，结论写报告）。

## 报告

完整报告写入 `docs/tasks/evidence/t5a-report.md`（改动文件、测试结果、9 路 e2e 产出样例、端点响应示例、遗留事项），最终回复简要引用。
