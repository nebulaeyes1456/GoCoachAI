# Claude Code 接手提示词（复制以下全部内容给 Claude Code）

你是「弈友 v2」（GoCoachAI）项目的接任开发工程师。项目在 Windows，路径 `C:\Users\31878\Documents\GoCoachAI2`。请先通读 `docs/architecture.md`（API 契约）、`README.md`、`更新日志.md` 三份文档，再开始任何编码。

## 一、项目地图

- 技术栈：Python 3.13（项目虚拟环境 `.venv`）/ FastAPI / SQLite（`data/goapp.db`）；前端为**无构建** Vue 3（CDN ESM）+ WGo.js，代码在 `frontend/`；AI 引擎为 KataGo v1.18（`engine/`，模型 b10c384 人类版 + b10c128/b6c96 备用）。
- 后端分层：`backend/routers/*`（HTTP 层）→ `backend/services/*`（业务）→ `backend/common/*`（db/models/settings/sgf_io）。
- 题目系统：`backend/services/problems/{store,utils,generator,checker}.py` + `backend/routers/problems.py`。**你要做的新功能大量复用这里的代码**。
- 关键复用件：
  - `backend/services/engine/verify.py::verify_position(setup_sgf, candidate_coords, profile, max_visits, timeout)` —— KataGo 局部批量验证，返回每候选点胜率/PV。
  - `backend/services/problems/utils.py::setup_sgf()` —— 题面 SGF 构造（AB/AW 摆子；**白先题面必须前置一手 `;B[tt]` 修正奇偶**）。
  - `backend/services/problems/store.py::insert_problem()` —— 幂等入库（INSERT OR IGNORE）。
  - `backend/services/problems/generator.py` —— 参考它的「裁剪局部 + 验题阈值 + 难度分级」逻辑（你应把裁剪函数抽取为公共函数复用，而不是复制）。
- 成长视图（v1.6.0）：`backend/services/progress/*`，与你无关，但不要破坏。

## 二、运行手册（先跑通再动手）

```powershell
cd C:\Users\31878\Documents\GoCoachAI2
# 单元测试（168 项，含真实引擎 e2e，全量约 40 分钟；日常只跑相关文件）
.\.venv\Scripts\python.exe -m unittest tests.test_problems_generator -v
# dev 后端（注意：端口 8766 与桌面版单实例锁冲突，先确认桌面 exe 未运行）
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --app-dir . --port 8766
# 浏览器验收 http://127.0.0.1:8766/（右上角数据源可选 Mock/真实后端）
# git 不在 PATH：
& 'C:\Program Files\Git\cmd\git.exe' status
```

## 三、铁律（违反会导致隐性 bug）

1. **契约**：`docs/architecture.md` §4.3 是题目系统既有契约，只许追加小节、不许改动既有内容；新接口必须同步写进该文档。
2. **数据库**：只通过 `backend/common/db.py` 的 `MIGRATIONS` 列表追加迁移（version 严格递增，当前最新是 9），禁止手改 `SCHEMA_SQL`。
3. **编码**：所有含中文的文件用 UTF-8 无 BOM；修改文件时若 PowerShell 会破坏编码，用 Python `io.open(..., encoding='utf-8')` 读写。
4. **PowerShell**：不要用内嵌 `python -c "..."` 跑复杂逻辑（引号必坏），写临时 `.py` 脚本执行后删除；GBK 控制台不要打印 ✓/✗/─ 等特殊字符（用 [OK]/[X]）。
5. **版本号联动**（每次前端/接口变更交付前）：`frontend/index.html` 里 css/js 的 `?v=`、`frontend/js/api.js` 里 `mock.js?v=`、`backend/routers/system.py` 的 `VERSION`、`backend/main.py` 的 FastAPI version、`scripts/setup_installer.py` 的 `VERSION`、`更新日志.md` 加新条目。
6. **成本**：DeepSeek 调用是付费的，开发期 LLM 相关功能一律先 mock 验证、真实调用单次验证即可；`max_tokens` 控制在 900 以内。KataGo 本地验证免费、放心用。
7. **坐标**：前后端坐标一律界面坐标（如 "D15"）；SGF 解析交给 `backend/common/sgf_io.py` 或前端 WGo，不要自己解析。
8. 不要提交 `data/*`、`*.exe`、`build/`、`dist/`（已 gitignore）。

## 四、任务：死活题生长链条（Growth Chains）

**需求背景**：用户希望题库从「平面题集」升级为「有来源、有脉络的成长树」——从布局定式（如托退定式）出发，长出后续的死活/对杀变化题，学棋者沿链条练习能理解"这个死活是从哪里来的、为什么出现"。

### 4.1 数据模型（db 迁移 v10/v11）

```sql
-- v10
CREATE TABLE IF NOT EXISTS problem_chains (
    id TEXT PRIMARY KEY,            -- 如 "chain-tuotui"
    name TEXT NOT NULL,             -- 定式名，如「托退定式」
    theme TEXT,                     -- life_death / capturing_race / mixed
    root_sgf TEXT NOT NULL,         -- 定式起点 SGF（含全部手顺）
    seed_sgf TEXT,                  -- 定式完成棋形 SGF（摆子局，可空=自动从 root_sgf 重放裁剪）
    description TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT
);
-- v11
ALTER TABLE problems ADD COLUMN chain_id TEXT;
ALTER TABLE problems ADD COLUMN chain_step INTEGER;
```

### 4.2 定式种子库（首期 ≥ 10 个）

新建 `data/chains/` 目录，放定式 SGF（19 路），建议清单：托退定式、双飞燕、小雪崩、大飞守角点三三、小目二间高挂、星位挂角点三三、无忧角托角、一间低夹、二间高夹、村正妖刀。每个 SGF 根节点用 `C[...]` 注释写明定式名。这些 SGF 用 WGo 界面坐标手写即可（如 `(;GM[1]FF[4]SZ[19]C[托退定式];B[pd];W[dp];B[pp];W[dd];B[fq];W[cn];B[qj]...)`）。配套写一个 `scripts/seed_chains.py`，幂等地把这些 SGF 注册进 `problem_chains` 表。

### 4.3 生长引擎（新文件 `backend/services/problems/chains.py`）

`grow_chain(chain_id, max_depth=3, max_per_level=3, profile="standard", db_path=None)`：

1. 读链 root_sgf → 重放全部手顺 → 取定式终局局面。
2. **局部裁剪**：以定式落子区域为包围盒（外扩 1 格，保留完整棋串，逻辑参考 generator 的裁剪，抽取公共函数），得到题面 setup SGF（摆子局；轮到方若为白，前置 `;B[tt]` 修奇偶）。
3. **候选枚举**：局部空邻点 + 角部要点（如 2-1、2-2、2-3、3-3 的界面坐标）+ "pass"（检验紧迫性）。
4. **验题**（调用 verify_position，fine 档）：
   - 战术题合格线：正解胜率 > 0.95 且次优 < 0.3；
   - 死活/对杀额外要求：pass 后胜率 < urgency_max_winrate（必须现在处理，否则只是普通应手题）；
   - 不达标丢弃该分支。
5. **入库**：`source='chain'`、`theme/goal` 按 verify 与紧迫性归类、`chain_id/chain_step` 写入、hint/verdict 用规则模板（参考 generator）、explanation 置 null。题 id 仍用 setup_sgf 的 sha256 前 16 位（与 generator 一致的幂等方案）。
6. **递归生长**：正解落子后的局面 + PV 中对手最强应对 → 下一层种子棋形，深度 ≤ max_depth、每层 ≤ max_per_level。链上题以 `chain_step` 排序，保证「第 n 题的答案局面可重放出第 n+1 题的题面」。
7. 同一链重复 grow 必须幂等（不产生重复题、不改变既有 step 排序）。

### 4.4 API（追加到 `backend/routers/problems.py`，契约写进 architecture.md §4.3 追加小节）

```
GET  /api/v1/problems/chains                  → 链列表（含题数、主题）
GET  /api/v1/problems/chains/{chain_id}       → 链详情（按 chain_step 排序的题目列表）
POST /api/v1/problems/chains/{chain_id}/grow  → 触发生长（同步执行，返回新增题数与丢弃数）
```

### 4.5 前端（练习视图内，不新增顶层 tab）

- 练习页主题筛选区新增「题链」入口 → 链列表卡片（名称、描述、题数、示例棋盘缩略图可省）。
- 链详情：竖向时间线展示步序（每步：棋形来源说明 + 目标 + 难度），顶部「连续练习」按钮：按 step 顺序进做题模式（做完一题自动进入下一题的题面）。
- 题目卡片在链内时显示来源徽标：「托退定式 · 第 3 变」。
- `frontend/js/mock.js` 同步加 1~2 条演示链与 mock 接口。

### 4.6 验证与交付

- 新增 `tests/test_chains.py`：数据模型 CRUD、grow 的 **mock verify** 分支（参考 `tests/test_problems_generator.py` 里 `mock.patch.object(generator, "verify_position", ...)` 的写法）、幂等性；e2e 测试用 `@unittest.skipUnless(引擎与模型存在)` 且只生成 1 条链。
- 测试通过后：起 dev 服务 → 浏览器 mock/真实双模式验收 → 版本号 bump（见铁律 5）→ 更新 `docs/architecture.md`、`更新日志.md`（v1.7.0 条目）、README Highlights。
- git commit（英文 message），**不要 push**（网络受限，由用户手动推送）。

## 五、验收标准（必须全部满足）

1. 每条定式链首轮 grow 产出 ≥ 3 道达标题（战术阈值如上），定式种子全部注册成功。
2. 链上题面局部性：题面包围盒 ≤ 9×9（角部定式）；题面 SGF 可被 `sgf_io.parse_sgf` 解析且轮到方正确。
3. 链内生长逻辑一致：从链起点按「正解 + 对手应对」可重放到任意后续题的题面（用测试断言）。
4. 幂等：同一链 grow 两次，第二次新增 0 题、题列表不变。
5. 既有功能零回归：`python -m unittest tests.test_problems_store tests.test_problems_generator tests.test_problems_checker tests.test_progress -v` 全绿（此子集约 1 分钟内跑完）。

## 六、完成后向我汇报

- 实现了哪些文件（列表）
- 每个定式链生成题数统计
- 测试结果（子集 + 新增测试）
- 你踩到的坑与对既有代码的改动（若有）
- 未完成项与建议

开始工作吧。先跑通测试手册，再动手。
