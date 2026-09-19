# 架构与接口契约（Architecture & API Contract）

> 本文档是**所有智能体窗口的唯一契约来源**。任何窗口不得私自修改接口；确需修改时，先改本文档，再在 `docs/plan.md` 中记录变更并通知主集成人。

---

## 1. 仓库结构

```
GoCoachAI/
├── 项目书.md
├── docs/
│   ├── architecture.md        # 本文档（契约）
│   ├── plan.md                # 进度记录（主集成人维护）
│   └── agents/                # 各窗口任务书
├── backend/
│   ├── main.py                # FastAPI 入口（窗口0 搭骨架，各窗口注册路由）
│   ├── config.yaml            # 本地配置（不入库，提供 config.example.yaml）
│   ├── common/
│   │   ├── models.py          # Pydantic 数据模型（契约落地）
│   │   ├── db.py              # SQLite 连接与建表（窗口0）
│   │   ├── sgf_io.py          # SGF 读写工具（窗口0 提供最小实现）
│   │   └── settings.py        # 配置加载（窗口0）
│   ├── services/
│   │   ├── engine/            # KataGo 封装（窗口1）
│   │   ├── review/            # 复盘服务（窗口1）
│   │   ├── coach/             # LLM 教练服务（窗口2）
│   │   └── problems/          # 题目系统（窗口3）
│   └── routers/
│       ├── review.py          # /api/v1/review/*（窗口1）
│       ├── coach.py           # /api/v1/coach/*（窗口2）
│       ├── problems.py        # /api/v1/problems/*（窗口3）
│       └── system.py          # /api/v1/system/*（窗口0/4）
├── frontend/
│   ├── index.html             # 复盘页（窗口4）
│   ├── practice.html          # 练习页
│   ├── ask.html               # 答疑页
│   ├── assets/
│   │   ├── vue.esm.js         # 本地 vendored Vue3 ESM 构建
│   │   └── wgo/               # WGo.js 本地文件
│   └── js/app.js              # 前端逻辑
├── engine/                    # KataGo 目录（不入库，下载脚本放 scripts/）
├── scripts/
│   ├── download_katago.py     # 下载引擎+模型（窗口1）
│   └── build_desktop.py       # 打包脚本（窗口4）
├── tests/
└── data/
    ├── goapp.db               # SQLite（运行时生成）
    └── library/               # 用户导入的题库 SGF
```

**约定**：`engine/` 与 `data/` 不入 git（`.gitignore`）；`config.yaml` 含 API key 不入库。

---

## 2. 运行模型

- 后端：`uvicorn backend.main:app --port 8765`（固定端口 **8765**，桌面版同）。
- 前端：纯静态文件，由后端挂载：`GET /` → `frontend/index.html`。
- 桌面版：pywebview 启动内置 WebView2 指向 `http://127.0.0.1:8765`，后端线程常驻。

---

## 3. 数据模型（SQLite 表）

```sql
CREATE TABLE IF NOT EXISTS reviews (
  id TEXT PRIMARY KEY,              -- SGF 的 sha256 前 16 位
  sgf_path TEXT NOT NULL,           -- 原始 SGF 存储路径
  board_size INT NOT NULL,
  black TEXT, white TEXT,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending/analyzing/done/failed
  profile TEXT NOT NULL DEFAULT 'fast',    -- fast/standard/fine
  progress REAL NOT NULL DEFAULT 0,        -- 0~1
  error TEXT
);

CREATE TABLE IF NOT EXISTS moves (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  review_id TEXT NOT NULL REFERENCES reviews(id),
  move_number INT NOT NULL,
  color TEXT NOT NULL,              -- B/W
  coord TEXT NOT NULL,              -- 如 "Q16"，pass 为 ""
  winrate REAL,                     -- 当前方胜率 0~1
  score_lead REAL,                  -- 目数领先
  visits INT,
  category TEXT,                    -- blunder/question/good/normal
  delta REAL,                       -- 胜率变化（当前方）
  best_coord TEXT,                  -- KataGo 推荐点
  pv TEXT,                          -- 变化序列 "Q16 R15 ..."（JSON 数组）
  UNIQUE(review_id, move_number)
);

CREATE TABLE IF NOT EXISTS explanations (
  review_id TEXT NOT NULL,
  move_number INT NOT NULL,
  kind TEXT NOT NULL,               -- move/summary/answer
  content TEXT NOT NULL,            -- 结构化 JSON（见 §6）
  model TEXT, cost REAL,            -- token 成本
  created_at TEXT NOT NULL,
  PRIMARY KEY (review_id, move_number, kind)
);

CREATE TABLE IF NOT EXISTS problems (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,             -- generated/library/imported
  review_id TEXT,                   -- 来源对局（generated 时非空）
  theme TEXT NOT NULL,              -- life_death/capturing_race/endgame/middle
  rank_min INT, rank_max INT,       -- 适用级位（K 为负，D 为正：15K=-15, 3D=3）
  setup_sgf TEXT NOT NULL,          -- 题面 SGF（到出题点为止）
  answer TEXT NOT NULL,             -- 正解第一手 coord
  branches TEXT NOT NULL,           -- 验证分支 JSON：变化树（KataGo 输出）
  verdict TEXT NOT NULL,            -- 死活/对杀结论说明
  hint TEXT,                        -- LLM 生成提示
  explanation TEXT,                 -- LLM 生成讲解
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  problem_id TEXT NOT NULL REFERENCES problems(id),
  answer_coord TEXT NOT NULL,
  correct INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (   -- T0：schema 版本表（迁移机制）
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);
```

### §3.1 迁移约定（T0，2026-08-31）

- 以上全部 `CREATE TABLE IF NOT EXISTS` 视为**基线 schema version 1**，由
  `backend/common/db.py` 的 `init_db()` 幂等执行并写入 `schema_migrations`。
- 后续**任何改表结构（加表/加列/改索引）只允许在 `db.MIGRATIONS` 列表末尾
  追加增量迁移**（`(version, sql)`，version 严格递增），**禁止手改旧 SQL**；
  每次迁移成功即写入 `schema_migrations(version, applied_at)`。
- 迁移 SQL 必须可重试（`IF NOT EXISTS` / 先查后改）：迁移中途失败不会记录
  版本，下次启动会整体重跑该版本。
- 旧库（已有表但无版本表）首次启动时按 version 1 基线补记，不重复建表。
- 当前版本查询：`db.schema_version()`，经 `GET /api/v1/system/version`
  的 `schema_version` 字段对外展示。

### §3.2 题链表（v1.7.0 追加，迁移 v10/v11）

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
ALTER TABLE problems ADD COLUMN chain_id TEXT;    -- 所属链（NULL=不在链上）
ALTER TABLE problems ADD COLUMN chain_step INTEGER;  -- 链内步序（第 n 变）
```

- 题链数据流：`data/chains/*.sgf`（定式种子，根节点 `C[...]` 写定式名、
  `RE[...]` 写主题、`GC[...]` 写说明）→ `scripts/seed_chains.py` 幂等注册到
  `problem_chains` → `POST /api/v1/problems/chains/{id}/grow` 长出题目
  （`problems.source='chain'`，按 `chain_step` 排序即学习顺序）。
- `chain_step` 从 1 起；链上第 n 题的题面可由第 n-1 题的「正解 + 对手应手」
  重放得到，完整手顺记录在该题 `branches.chain.moves`。

---

## 4. REST API 契约

统一约定：JSON；错误返回 `{"detail": "..."}`；分析类接口为异步任务，返回后通过 `GET /status` 或 WebSocket 推送进度。

### 4.1 复盘（窗口1 实现）

```
POST /api/v1/review/analyze
  入: { "sgf_text": "<SGF 文本>" , "profile": "fast" }   # 或 sgf_base64
  出: { "review_id": "3f2a..." }

GET /api/v1/review/{review_id}/status
  出: { "status": "pending|analyzing|done|failed", "progress": 0.42, "error": null }

WS  /ws/review/{review_id}
  推: { "type": "progress", "progress": 0.42 }
      { "type": "done" } | { "type": "failed", "error": "..." }

GET /api/v1/review/{review_id}
  出: {
    "id", "board_size", "black", "white", "profile", "status",
    "winrate_curve": [ {"move":1,"color":"B","coord":"Q16","winrate":0.51,"score_lead":1.2,
                        "category":"normal","delta":0.001,"best_coord":"R4","pv":["R4","Q16"],"visits":600}, ... ],
    "key_moves": [ 同上结构，仅 category != normal 的手 ],
    "stats": { "blunders": 3, "questions": 7, "good": 2 }
  }
```

关键手判定（契约固定，阈值放 `config.yaml`）：

- 当前方胜率变化 `delta = winrate_after - winrate_before`；
- `delta <= -0.08` → **blunder（坏手）**；`-0.08 < delta <= -0.03` → **question（疑问手）**；
- `delta >= +0.06` 且 KataGo 最佳点与落点不同 → **good（好手）**；
- 其余 → normal。让子棋/早期布局可放宽（由窗口1 按段位参数微调，写入 config）。

### 4.2 教练讲解（窗口2 实现）

```
POST /api/v1/coach/explain
  入: { "review_id": "3f2a...", "move_number": 37 }
  出: {
     "move_number": 37, "kind": "move",
     "content": {
        "problem": "这手棋的问题在于……",
        "reason": "因为白棋可以……",
        "recommendation": "推荐下在 D15：……",
        "variation": ["D15","E16","C16"],   # 变化序列，前端棋盘播放
        "takeaway": "本手提醒：断点优先补……",
        "level_note": "（针对 5K 的解释深度）"
     },
     "model": "deepseek-chat", "cost": 0.031
   }

POST /api/v1/coach/summary
  入: { "review_id": "3f2a..." }
  出: { "kind": "summary",
        "content": {
          "opening": "布局阶段……",
          "middle": "中盘第 55 手起连续失误……",
          "endgame": "官子……",
          "strengths": ["计算局部对杀时有耐心", ...],
          "weaknesses": ["断点意识不足", ...],
          "suggestions": ["本周重点练习：接触战断点 10 题", ...]
        },
        "model": "deepseek-chat", "cost": 0.06 }

POST /api/v1/coach/ask
  入: { "sgf_text": "<局面 SGF>", "question": "这里该不该断？", "level": "-5" }
  出: { "answer": { "conclusion": "...", "reasoning": "...",
         "variation": [...], "kata_winrate": 0.62 }, "model", "cost" }
```

**质量红线（prompt 内强制）**：讲解只能引用调用方传入的 KataGo 变化与数据，不得自行编造着法；输出必须为可解析 JSON，字段齐全；`variation` 数组只允许来自 KataGo PV。

### 4.3 题目系统（窗口3 实现）

```
POST /api/v1/problems/generate
  入: { "review_id": "3f2a...", "themes": ["life_death","capturing_race","endgame"],
        "max_problems": 6, "target_rank": -5 }
  出: { "problems": [ { "id": "p1..." , "theme": "life_death", "setup_sgf": "...",
        "rank_min": -8, "rank_max": -3, "hint": "黑先，做活左边一块" } ],
        "failed": 2 }        # 验证未通过被丢弃的数量

GET  /api/v1/problems/{problem_id}
  出: { 完整题目含 answer/branches/verdict/explanation（答题后才返回 explanation 也允许，字段始终存在，可为 null） }

POST /api/v1/problems/{problem_id}/attempt
  入: { "coord": "D15" }     # 或 "pass"
  出: {
     "correct": false,
     "response": "这步没有护住断点，白棋 E16 断后……",
     "variation": ["E16","F16"],
     "solved": false,        # true 时表示本题已过
     "explanation": null
   }

GET  /api/v1/problems/library?theme=life_death&rank=-5&limit=20&offset=0
  出: { "problems": [ { "id", "theme", "rank_min", "rank_max", "setup_sgf", "hint" } ],
        "total": 64 }   # total 为筛选后总数（分页前），offset 为窗口3 扩展参数（2026-08-28）

POST /api/v1/problems/extract           # M3 新增（2026-08-31）：局部死活提取
  入: { "sgf_text": "(;GM[1]FF[4]SZ[9]...)", "max_problems": 5, "target_rank": -5 }
      # 或 { "review_id": "3f2a...", "max_problems": 5, "target_rank": -5 }
      # sgf_text 与 review_id 至少提供一个，均缺省返回 400；仅 sgf_text 时
      # 后端先走复盘管线（submit+轮询 done，复用缓存）再提取。
  出: { "extracted": [ { "id": "p3f2a9c1b7e5d042", "theme": "life_death",
        "setup_sgf": "(;GM[1]FF[4]CA[UTF-8]SZ[9]KM[7.5]AB[cc][dc]...;B[tt])",
        "rank_min": -8, "rank_max": -3,
        "hint": "黑先，左下的棋形处在生死关口，请找出急所（做活或杀棋）。" } ],
        "failed": 2, "skipped": 3 }
      # extracted：本次新入库的题（题 id = 题面哈希，INSERT OR IGNORE 幂等，
      #   重复提取/重复题面不重复计数）；
      # failed：验题未通过（正解 ≤0.95 / 次优 ≥0.3 / 死活不紧迫）丢弃的中心数；
      # skipped：未验题跳过的信号数（超出前 N 中心、pass 等无效中心、重复题面）；
      # 同步接口（耗时数分钟，线程池执行）；内部互斥锁：引擎单进程，并发请求
      #   在提取任务运行期间返回 409。
  说明：提取流程 = 扫描未定型信号（失误手 / 相邻两手 delta 反转 /
  终局 winrate 波动）→ 以中心点 + KataGo 推荐点裁剪局部 →
  classify_theme 归类 → 验题（规则同 generate）→ 入库，source 仍为
  "generated"（不引入新枚举值）。
```

**验题规则（契约）**：`generated` 来源的题必须经 KataGo 多分支验证——正解分支最终胜率 > 0.95（当前方）且非正解候选点最终胜率 < 0.3，否则丢弃；每道题记录 `branches` 供讲解与判题使用。

**§4.3 附注：局面验证接口（窗口1 提供，窗口3 调用，签名稳定）**

`backend/services/engine/verify.py`：

```python
verify_position(
    setup_sgf: str,                 # 题面 SGF（AB/AW 摆子或到出题点为止的主线）
    candidate_coords: list[str],    # 候选着点（界面坐标 "D15"；pass 用 "pass"）
    profile: str = "standard",      # 对应 katago.max_visits
    max_visits: int | None = None,  # 覆盖 profile 的搜索量
    timeout: float = 600.0,
) -> list[VerifyResult]             # VerifyResult{coord, winrate, score_lead,
                                    #   visits, pv(含候选点), best_coord}
```

- `winrate`：候选方（落子方）搜索后胜率 0~1；`score_lead`：候选方目数领先；
- 一次调用启动常驻 `katago analysis` 引擎进程批量完成全部候选点（引擎首次
  启动加载模型约 1 分钟，窗口3 应把多题验证合并成更少的调用批次）；
- 底层胜率视角为 `reportAnalysisWinratesAs = SIDETOMOVE`（见
  `engine/analysis_example.cfg`），已换算为候选方视角返回。

**§4.3 附：死活题生长链条（Growth Chains，v1.7.0 追加）**

```
GET  /api/v1/problems/chains
  出: { "chains": [ { "id": "chain-tuotui", "name": "托退定式",
        "theme": "mixed", "description": "……", "status": "active",
        "problems_count": 3, "themes": ["life_death","capturing_race"],
        "created_at": "2026-09-18T10:00:00+00:00" } ] }

GET  /api/v1/problems/chains/{chain_id}
  出: { "chain": { …同上… }, "root_sgf": "(;GM[1]…)", "problems": [
        { "id": "p1a2…", "theme": "life_death", "goal": "做活",
          "rank_min": -7, "rank_max": -3, "setup_sgf": "(;GM[1]FF[4]…SZ[19]…)",
          "hint": "黑先，左下的棋形处在生死关口……", "answer": "D5",
          "verdict": "D5 是正解（黑方胜率 97%）……",
          "chain_id": "chain-tuotui", "chain_step": 1, "solved": false } ] }
      # problems 按 chain_step 升序（即学习顺序）；404 链不存在

POST /api/v1/problems/chains/{chain_id}/grow
  入: { "max_depth": 3, "max_per_level": 3, "profile": null }
  出: { "chain_id": "chain-tuotui", "added": 3, "discarded": 4, "steps": 3,
        "problems": [ …同上 ChainProblemBrief… ] }
      # added=本次新入库题数（重复 grow 幂等为 0）、discarded=验题未过/超框丢弃数；
      # 同步接口（引擎单进程，已有生长任务在跑时 409）；404 链不存在 / 422 SGF 无法重放
```

- 生长流程：定式 `root_sgf` 重放 → 局部裁剪（包围盒外扩 1 格、保留完整棋串，
  超 9×9 丢弃）→ 候选枚举（局部空邻点 + 角部要点 + pass）→ 验题 → 入库
  （`source='chain'`、`chain_step` 写步序）→ 递归到 `max_depth` 层。
- **验题口径**：复用 `verify_position`，档位取 `problems.verify_profile`
  （死活/对杀 fine），并以 `allow_moves`（`branches.allow_moves`，题面包围盒
  外扩 2 格，同古典题 v0.9.8 口径）局部聚焦——19 路摆子局在空棋盘上全盘搜索
  时一选常是局外大场、胜率被子力朝向主导。`verify_position` 因此新增**可选**
  参数 `allow_moves: list[str] | None = None`（追加、向后兼容，缺省全盘）。
- **验收口径（`problems.chain_verify_mode`，默认 `local_board`）**，三种可选：
  - `local_board`（默认，**19 路推荐**）：把局部棋形裁成**小棋盘**（保留与
    角部两条边线的距离，`chain_board_margin`/`chain_board_min_size`）后按契约
    阈值验题（正解 > `chain_answer_min_winrate`、次优 < `chain_second_max_winrate`、
    死活/对杀 pass < `urgency_max_winrate`）。小棋盘上局部攻杀就是全局内容，
    与 app 里 9 路整盘复盘题（正解胜率 0.99）同一尺度；题面/答案/变化一律
    换算回 19 路界面坐标，`branches.verify_board` 记录小棋盘 size 与偏移。
  - `local_death`：**局部死活画像**（与 v1.5.0 练习深度讲解 `explainer.py`
    局部推演、原型 `scripts/classify_result.py` 同一套算法）——`allowMoves`
    锁局部 + `includeOwnership`：正解须达成目标（目标区归属 ≥ `chain_own_min`）、
    死活/对杀须现在处理（脱先损失 ≥ `chain_tenuki_min`）、次优点不得同样达成；
    画像存 `branches.local`（`own_pv`/`own_after`/`tenuki_loss`/`grade`/
    `result_type`），结论写进 `verdict`/`hint`（「目标：黑方净杀」）。
  - `relative`：**相对口径**——正解需比次优高 ≥ `chain_relative_gap`（默认 0.5）
    且比脱先高 ≥ `chain_relative_urgency`（默认 0.3），可选绝对下限
    `chain_relative_floor`（默认 0）；判的是「唯一急所 + 必须现在处理」，
    不受空盘面稀释（`branches.gaps` 记录两个差值）。
  - `winrate`：契约原文口径（19 路整盘 + `allow_moves` 局部聚焦）。
  可按调用覆盖：`POST /chains/{id}/grow` 的 `verify_mode` 字段。
- **种子挖掘（v1.7.3）**：`scripts/mine_chain_seeds.py` 从定式起点沿局部实战
  搜索「一手定生死」的局面——规则气数预筛（不启引擎）+ 与 grow 完全相同的
  验题路径判定，找到的种子保证可长出题；`--register` 可直接注册并生长。
- **实测（v1.7.1）**：契约口径在现有 10 条定式种子上均产出 0 题——定式终局是
  两分局面，没有「一手定生死」的棋串（正确判读）；古典死活题（真死活）同样
  过不了阈值，原因是结构性的：角部死活中防守方子力天然少于围攻方，活棋只多
  几目，胜率（哪怕小棋盘）也上不去。要长出达标题，种子须是**先手方明显领先
  且有一手定生死**的局面。体检工具：`scripts/check_chain_seeds.py`。
- 判题复用既有 `POST /api/v1/problems/{id}/attempt`：链上题 `branches.allow_moves`
  非空时，错误答案的补查沿用同一局部口径，保证反馈里的胜率与 `branches` 一套尺度。
- 题面性质：`setup_sgf` 为 19 路摆子局（AB/AW，白先前置 `;B[tt]` 修奇偶），
  包围盒 ≤ 9×9；题 id = `sha256(theme|setup_sgf)[:16]`（与 generate 同方案，幂等）。

**§4.3 附二：快速死活判定（v1.7.2 追加）**

```
POST /api/v1/problems/life_death
  入: { "sgf_text": "(;GM[1]FF[4]SZ[19]…)", "profile": "fast", "region_pad": null }
  出: { "report": {
          "board_size": 19, "to_move": "W", "stones": 11,
          "control": { "B": -0.75, "W": -0.33 },   # 各方**自己棋子**处的平均归属
          "status":  { "B": "死", "W": "未定" },    # 活 / 死 / 未定（保守判定）
          "ko": true, "ko_points": ["D13"],        # 同一条变化内被重复落子 = 回提（劫）
          "tenuki_loss": 0.42, "grade": "紧急",     # 先手脱先、对手抢要点后的归属损失
          "line": ["C16","D16","D15"], "profile": "fast", "note": "…" },
        "verdict": "局部未定型（黑 -0.75 / 白 -0.33）……" }
```

- 口径与 v1.5.0 练习深度讲解（`explainer.py` 局部推演）同源：`allowMoves`
  锁局部 + `includeOwnership`；**不调 LLM、不生成题、不写库**，同步接口（数秒）。
- 判据（保守）：各方**自己棋子处**的平均归属（活棋≈本方为正、死子翻负）→
  ≥0.5 判「活」、≤-0.5 判「死」、其余「未定」；劫争只在**同一条变化内**出现
  重复落子（回提）时置位——不同变化之间撞同一点是常态，不算劫。**刻意不报
  「劫活/双活」**（需更多证据：气数、双方最佳应对），细分结论仍由深度讲解
  （LLM 复核）给出。
- 气数分析（v1.7.3，纯规则不启引擎）：`race_analysis` 给出**对杀/双活**结论
  ——双方各有一个 ≤3 气棋串且**公共气**非空即对杀；双方 ≤2 气、公共气非空且
  **各自都有眼位**（对方无法入气）判双活；只有一方紧气则为「单方受攻」。
  报告里对应 `race` / `race_text` 字段，`status` 会据此给出「对杀」「双活」。
- 已知边界：空旷盘面上的绝对死活不可靠（19 路摆子局被「谁朝向空旷盘面」
  主导），本接口适合**封闭局部**棋形；两分局面如实返回「未定」。

### 4.4 系统（窗口0/4）

```
GET /api/v1/system/info
  出: { "version": "0.9.0", "engine_ready": true, "model_ready": true,
        "engine_backend": "opencl", "schema_version": 1,
        "profile": "fast", "token_usage_month": 3.2, "token_limit_month": 30 }
       # engine_backend（T1 新增）：当前所选引擎后端 opencl/eigenavx2；
       # schema_version（T0 新增）：数据库 schema 版本；
       # engine_ready：真实引擎进程存活状态（按需启动，未运行时为 false）
PUT /api/v1/system/settings
  入: { "profile": "standard", "blunder_threshold": 0.08, ... }
```

**§4.4 运维/诊断端点（T0 新增，2026-08-31）**——修 bug/打补丁/更新功能的操作端：

```
GET /api/v1/system/health
  出: {
    "status": "ok",                       # ok | degraded（数据库/模型文件/DeepSeek key 三项）
    "checks": {
      "database":      { "ok": true,  "detail": "connected（schema v1）" },
      "engine_process":{ "ok": false, "detail": "引擎进程未运行（首次分析时自动启动）" },
      "model_file":    { "ok": true,  "detail": "C:/.../engine/b10c128.bin.gz" },
      "deepseek_key":  { "ok": true,  "detail": "configured" }
    }
  }
  说明：engine_process 报告真实存活状态，按需启动未运行不算故障（不影响 status）。

GET /api/v1/system/engine/status
  出: { "backend": "opencl", "mode": "auto", "running": false,
        "process_count": 0, "last_error": "" }

POST /api/v1/system/engine/restart
  入: { "backend": "cpu" }               # 可选；缺省=清除临时切换回到配置值
  出: { "ok": true, "backend": "eigenavx2", "mode": "auto",
        "message": "OpenCL 不可用（...），自动回退 CPU（Eigen/AVX2）" }
  说明：backend 可选 auto|cpu|opencl，仅进程内临时生效（不写 config）；
  终止在册分析进程并重置后端选择。

GET /api/v1/system/logs?lines=200
  出: { "path": "data/app.log", "lines": 12,
        "log": ["2026-08-31 10:00:01 [INFO] [server] 服务启动 v0.9.0，schema v1\n", ...] }
  说明：server.log_file（默认 data/app.log），>5MB 轮转 .1 备份；lines 1~5000。

POST /api/v1/system/cache/clear
  入: { "kind": "all" }                  # coach | review | all
  出: { "kind": "all", "cleared": { "explanations": 12, "coach_asks": 3,
                                    "moves": 20, "reviews_reset": 1 } }
  说明：coach=清讲解缓存（explanations+coach_asks）；review=清分析结果（moves）
  并把 done 复盘回到 pending（同 SGF 再提交会重新分析）。

POST /api/v1/system/db/backup
  出: { "path": "data/backups/goapp-20260831-100501.db",
        "filename": "goapp-20260831-100501.db", "size_bytes": 65536 }
  说明：VACUUM INTO 一致性备份；同秒重复调用自动加序号后缀。

GET /api/v1/system/version
  出: { "version": "0.9.0", "engine_backend": "opencl",
        "schema_version": 1, "api_prefix": "/api/v1" }
```

### 4.5 成长视图（窗口5 实现，2026-09-12）

```
POST /api/v1/progress/profiles
  入: { "name": "小明", "note": "九路练习" }
  出: { "profile": { "id": "pp…", "name": "小明", "note": "",
                      "games_count": 0, "created_at": "…", "updated_at": "…" } }
GET /api/v1/progress/profiles
  出: { "profiles": [ …同上… ] }
DELETE /api/v1/progress/profiles/{profile_id}
  出: { "deleted": true }                # 只解除棋谱关联，不删除复盘数据
GET /api/v1/progress/profiles/{profile_id}
  出: { "profile": …, "games": [ { "review_id", "board_size", "moves_count",
          "created_at", "blunders", "questions", "good" } ],
        "insight": null | { "games_count", "features", "rank_estimate",
                            "advice", "updated_at" } }
POST /api/v1/progress/profiles/{profile_id}/attach
  入: { "review_id": "…" }
  出: { "attached": true }               # 404 档案/复盘不存在；重复收藏幂等
POST /api/v1/progress/profiles/{profile_id}/import
  入: { "sgf_text": "(;…)", "review_profile": "fast" }
  出: { "review_id": "…", "status": "pending" }   # 提交复盘分析并立即归档
POST /api/v1/progress/profiles/{profile_id}/insight
  入: { "force": false }                  # force=true 跳过缓存重算
  出: { "insight": { "games_count", "features": {
          "n_games", "avg_loss_per_move", "blunders", "questions", "good_moves",
          "direction_errors", "complexity_errors", "reading_errors",
          "weakest", "weakest_loss",
          "phases": { "layout"|"middle"|"endgame": { "avg_loss", "moves",
                                                     "blunder_rate" } },
          "trend": "improving"|"declining"|"flat",
          "rank_estimate" }, "rank_estimate", "advice", "advice_model" } }
  说明：无棋谱返回 422；画像按档案缓存，attach/import 后自动失效。
POST /api/v1/progress/profiles/{profile_id}/advice
  入: { "force": false }
  出: { "insight": { …, "advice": { "summary", "strengths": [],
          "weaknesses": [], "plan": [ { "focus", "why", "practice", "theme" } ],
          "homework": [] } }, "model": "…", "cost": 0.01 }
  说明：theme 限定做活/杀棋/对杀/吃棋筋/逃棋筋/收官最大/中盘要点（题库主题）。

GET /api/v1/review/{review_id}?include_sgf=true
  出: ReviewDetailResponse 增加 sgf_text 字段（仅 include_sgf=true 时返回，
      供成长页跳复盘重载棋盘用）
```

---

## 5. 配置（config.yaml）

```yaml
katago:
  executable: engine/katago.exe        # OpenCL 版路径
  executable_cpu: engine/katago-eigenavx2.exe   # CPU 回退版（T1 新增）
  backend: auto                        # auto|cpu|opencl（T1 新增，默认 auto）
  detected_backend: opencl             # 探测结果自动写入（backend=auto 时）
  model: engine/b10c128.bin.gz
  max_visits: { fast: 200, standard: 600, fine: 1500 }
  threads: 12
coach:
  provider: deepseek                   # deepseek | ollama
  api_key: "sk-..."                    # 不在 example 中放真值
  base_url: https://api.deepseek.com
  model: deepseek-chat
  ollama: { base_url: "http://192.168.x.x:11434", model: "qwen2.5:14b" }
review:
  profile: fast                       # fast/standard/fine（§4.4 的 profile）
  blunder_threshold: 0.08
  question_threshold: 0.03
  good_threshold: 0.06
problems:                             # 题目系统（窗口3）
  crop_radius: 4                      # 错题局部裁剪半径（路）
  candidate_radius: 1
  max_candidates: 12
  verify_profile:                     # 各主题验证深度
    life_death: fine
    capturing_race: fine
    endgame: standard
    middle: standard
  urgency_max_winrate: 0.3            # 死活/对杀题 pass 后胜率上限
  endgame_move_fraction: 0.8          # 官子阶段手数占比阈值
server: { port: 8765, log_file: "data/app.log" }   # log_file：服务日志（>5MB 轮转，T0 新增）
budget: { token_limit_month: 30 }      # 元
```

**T1 后端选择规则（2026-08-31）**：`katago.backend=cpu` → 用 `executable_cpu`；
`=opencl` → 用 `executable`；`=auto` → 快速探测（`katago-opencl.exe version`，
秒级，不触发 kernel 编译），失败回退 CPU。探测结果幂等写入
`katago.detected_backend`（backend 保持 auto；手动指定时不写）；进程级缓存，
不每次请求重探测。`POST /system/engine/restart` 的 backend 参数仅临时生效。

**§5.1 题链生长可选配置（v1.7.0 追加，全部有默认值，可不写）**：

```yaml
problems:
  chain_crop_pad: 1               # 题面裁剪：定式区域包围盒外扩格数
  chain_max_bbox: 9               # 题面最大包围盒（超框丢弃该分支）
  chain_region_pad: 2             # 验题局部聚焦区域 = 题面包围盒外扩格数
  chain_verify_mode: local_board  # local_board（默认，局部裁小棋盘再验）| local_death | winrate
  chain_board_margin: 2           # local_board：小棋盘在非贴边两侧留的余量
  chain_board_min_size: 5         # local_board：小棋盘最小边长
  chain_own_min: 0.75             # local_death：正解目标区归属下限（正=控住该区）
  chain_tenuki_min: 0.15          # local_death：脱先损失下限（0.15 半紧急 / 0.4 紧急）
  chain_death_profile: fast       # local_death：局部推演档位
  chain_death_pv_len: 6           # local_death：局部推演走多少手 PV
  chain_answer_min_winrate: 0.95  # winrate 口径：正解胜率下限（契约值）
  chain_second_max_winrate: 0.3   # winrate 口径：次优胜率上限（契约值）
  chain_target_rank: -5           # 链上题目标级位中心（难度分级用）
```

---

## 6. 讲解 JSON 结构（`explanations.content`）

统一字段名，前端渲染不感知模型差异：

| kind | 字段 |
|---|---|
| move | problem / reason / recommendation / variation / takeaway / level_note |
| summary | opening / middle / endgame / strengths / weaknesses / suggestions |
| answer | response / variation / solved |

---

## 7. 协作与变更流程

1. 窗口开发中需要新接口 → 先在本文件 §4 追加并写清入/出参数与**真实字段示例**；
2. 修改 `common/models.py` 同步模型定义；
3. 在 `docs/plan.md` 记录：日期、变更内容、原因；
4. 通知主集成人（你）确认后，其他窗口以新契约为准。

### §7.1 契约版本化约定（T0，2026-08-31）

- **API 只增不改**：任何已有端点的请求/响应字段只能新增、不得删除或改变
  类型/含义（例外走 §7 通知流程并做兼容处理）；
- **废弃字段至少保留一个小版本**（x.y 的 y+1）再移除，期间保持旧值兼容；
- 所有新端点在 `frontend/js/api.js` 同步封装（窗口4 负责维护）；
- **改表结构只走迁移**（§3.1：`db.MIGRATIONS` 追加，禁止手改旧 SQL）；
- 更新功能的操作端：`PUT /system/settings` 配置热更新、`POST /system/cache/clear`
  清缓存、`/system/version` 供检查更新、`/system/engine/restart` 换引擎后端。
