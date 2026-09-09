# T1 任务书：后端 GPU/CPU 自动适配（W1）

## ① 做什么

目标：**电脑无论有没有 GPU 都能用**。KataGo 引擎在启动/首次使用时自动选择可用的后端：

1. **引擎后端自动探测**（核心）：
   - `engine/katago-opencl.exe`：OpenCL 后端，有 GPU（独显或 Intel/AMD 核显）时优先；
   - `engine/katago-eigenavx2.exe`：纯 CPU（Eigen/AVX2）后端，无 OpenCL 时回退；
   - 探测**必须快速**：禁止触发 KataGo OpenCL kernel 编译（首次约 5 分钟）。优先用 KataGo 的轻量子命令（如 `katago version`）或启动后快速握手超时判定；探测结果缓存并写入 `config.yaml`（`katago.backend: auto|cpu|opencl`），支持手动强制。
   - 探测逻辑放 `backend/services/engine/`（新文件如 `backend.py` 或 `engine_factory.py`），供 `analyze_sgf`、`verify` 与窗口1/2/3 复用。
2. **配置层适配**：`config.yaml` 增加 `katago.backend`（默认 `auto`）；`settings.py` 默认值同步；探测结果幂等写入（不覆盖手动强制值）。
3. **对外联动（与 T0 协同）**：`GET /api/v1/system/info` 的 `engine_ready` 改为真实状态（引擎存活），并返回 `engine_backend`（当前所选后端），前端设置页可显示。
4. **回归**：全量后端测试保持通过（101 项全绿 + T0 新增测试）。

## ② 禁止做什么

- 禁止修改契约 `docs/architecture.md §4` 的 API 格式（新增内部字段除外）；
- 禁止改动现有 `config.yaml` 中与探测无关的字段；
- 禁止在无 GPU 时静默失败或崩溃——必须自动回退并给出可读日志；
- 禁止每次请求都重跑探测（必须缓存）。

## ③ 验收方式

- [ ] 命令：项目根 `.venv\Scripts\python.exe -m unittest discover -s tests` → 101 全绿；
- [ ] 提供可复现的探测验证：写一个测试/脚本证明"强制 cpu → 用 eigenavx2；强制 opencl → 用 opencl；auto → 探测并回退"，不依赖真机 GPU 也能测（如 mock 探测函数）；
- [ ] 启动后端 `uvicorn backend.main:app --port 8765`（项目根）日志显示所选后端与原因；
- [ ] 引擎端到端：9 路 fast 档分析 < 60s（沿用测试 benchmark）。

## 参考文件

- `backend/config.yaml`、`backend/common/settings.py`、`backend/services/engine/engine.py`、`backend/services/engine/analyze_sgf.py`、`backend/services/engine/verify.py`
- `engine/` 目录（两个 exe + 模型 + cfg）
- `经验总结.md`（引擎已下载，OpenCL 首次启动 5 分钟）
