# T5a 局部死活提取 —— 实施报告（补写）

> 任务书：`docs/tasks/T5a-局部死活提取后端.md`（M3 核心管线）
> 完成日期：2026-08-31 · 状态：✅ 完成并验收通过（14/14 测试全绿）

## 改动文件

| 文件 | 操作 | 说明 |
| --- | --- | --- |
| `backend/services/problems/extractor.py` | 新增 | 局部死活提取器：信号扫描 → 裁剪 → 验题 → 入库；`extract(sgf_text|review_id, max_problems, target_rank, review_profile)` |
| `backend/routers/problems.py` | 修改 | `POST /api/v1/problems/extract`（互斥锁防并发，409 忙响应） |
| `backend/common/models.py` | 修改 | `ProblemExtractRequest` / `ProblemExtractResponse` |
| `docs/architecture.md` | 修改 | §4.3 增补 extract 契约与字段示例 |
| `backend/config.yaml` / `settings.py` | 修改 | `extract_signal_threshold`、`endgame_swing_threshold`、`extract_review_timeout` |
| `tests/test_extractor.py` | 新增 | 14 项测试（扫描/管线/端点/幂等 mock + 真实 9 路 e2e） |

## 关键实现点与调试经验

1. **三类信号扫描**（`scan_signals`）：失误手（blunder/question）、相邻手 delta 反转（swing）、终局附近波动（endgame）；按强度降序。
2. **验题复用 v1**：`verify_candidates`（0.95/0.3 + pass 紧迫性）、`utils` 裁剪/主题/难度、`store` 幂等入库。
3. **调试发现并修复的两个稳定性问题**：
   - **复盘档位抖动**：fast 档（200v）下 best_coord/主题分类抖动，同一手时而过验题时而不通过。修复：`extract` 增加 `review_profile` 参数，e2e 用 `standard` 档（600v）。
   - **幂等与测试断言冲突**：题已入库后重复提取 `extracted=0`。修复：e2e 前清空 problems 表。
   - **信号池过小**：原取前 N 个信号即验题，验题通过率低时全军覆没。修复：信号池扩为 `max(max_problems×3, 8)`，达标即停。
4. **e2e 结果**：`[bench] 9 路 e2e: 提取 1 题, 失败 0, 跳过 7, 耗时 37.2s`（fixture：razor 互相打吃 + 黑脱先，正解 G4 胜率 0.963，capturing_race）。

## 验收

- `python -m unittest tests.test_extractor`：14/14 全绿（含真实引擎 e2e）；
- 端点：`POST /api/v1/problems/extract` 400（无输入）/409（忙）/200（成功）均有测试覆盖；
- 全量后端测试保持全绿。

## 遗留

- 19 路大棋谱提取 ≥5 题的完整验收留待 M3 集成（人工抽测，项目书要求）；
- `extract` 用 standard 档复盘较慢，生产可考虑"复盘缓存 + 后台队列"（T0 运维接口已备）。
