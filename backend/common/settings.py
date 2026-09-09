"""配置加载（窗口0，T7 增加用户数据目录分离）。

- 读取 ``backend/config.yaml``；文件缺失时从 ``backend/config.example.yaml`` 复制一份。
- ``get_settings()`` 返回深合并了默认值的配置字典（线程安全缓存）。
- ``save_settings(updates)`` 将增量更新写回 ``config.yaml`` 并刷新缓存。

用户数据目录（T7，桌面分发用）：
- 环境变量 ``GOCOACH_DATA_DIR`` 指定时，DATA_DIR / CONFIG_PATH 指向该目录
  （启动器在 import backend 之前设置）；开发模式未设置时沿用项目内路径。
- 数据目录下缺 config.yaml 时，首次从 ``backend/config.yaml`` 迁移复制，
  此后用户改配置/写库均落在数据目录，升级安装程序不丢数据。

契约来源：``docs/architecture.md`` §5。
"""
from __future__ import annotations

import copy
import os
import shutil
import threading
from pathlib import Path
from typing import Any

import yaml

BASE_DIR = Path(__file__).resolve().parents[2]          # 项目根 GoCoachAI/
BACKEND_DIR = BASE_DIR / "backend"
CONFIG_EXAMPLE_PATH = BACKEND_DIR / "config.example.yaml"
FRONTEND_DIR = BASE_DIR / "frontend"

# 用户数据目录：GOCOACH_DATA_DIR 覆盖（桌面启动器设置）；否则项目内 data/
DATA_DIR = Path(os.environ.get("GOCOACH_DATA_DIR")) if os.environ.get("GOCOACH_DATA_DIR") else BASE_DIR / "data"
# 配置：优先数据目录；首次启动时把 backend/config.yaml 迁移过去（保留示例模板在代码目录）
CONFIG_PATH = DATA_DIR / "config.yaml"
_LEGACY_CONFIG_PATH = BACKEND_DIR / "config.yaml"


def _ensure_config() -> None:
    """确保 CONFIG_PATH 存在：数据目录缺配置时从 backend/config.yaml 迁移。"""
    try:
        if not CONFIG_PATH.exists():
            if _LEGACY_CONFIG_PATH.exists():
                CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(_LEGACY_CONFIG_PATH, CONFIG_PATH)
            elif CONFIG_EXAMPLE_PATH.exists():
                CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(CONFIG_EXAMPLE_PATH, CONFIG_PATH)
    except OSError:
        pass

# §5 全部字段的默认值（与 config.example.yaml 保持一致）
DEFAULT_CONFIG: dict[str, Any] = {
    "katago": {
        "executable": "engine/katago.exe",
        "executable_cpu": "engine/katago-eigenavx2.exe",
        "backend": "auto",  # auto|cpu|opencl（T1：GPU/CPU 自动适配）
        # detected_backend：backend=auto 时由探测写入（opencl|eigenavx2），
        # 手动指定 cpu/opencl 时不写；供 /system/engine/status 展示。
        "model": "engine/b10c128.bin.gz",
        "max_visits": {"fast": 800, "standard": 2000, "fine": 5000},
        "threads": 12,           # numSearchThreads：单局面搜索线程数（写入 analysis_example.cfg）
        "analysis_threads": 1,   # numAnalysisThreads：并行分析的局数（1=顺序，避免 CPU 争抢）
    },
    "coach": {
        "provider": "deepseek",
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "ollama": {"base_url": "http://127.0.0.1:11434", "model": "qwen2.5:14b"},
        # 窗口2：按模型可配置单价（元/百万 token），用于成本换算
        "pricing": {"deepseek-chat": {"input": 2.0, "output": 8.0}},
    },
    "review": {
        "profile": "fast",
        "blunder_threshold": 0.12,
        "question_threshold": 0.06,
        "good_threshold": 0.06,
        "handicap_relax": 1.5,  # 让子棋（HA>0/AB）阈值放宽系数（窗口1）
    },
    # 题目系统（窗口3）：错题生成/题库验证参数
    "problems": {
        "crop_radius": 4,          # 局部裁剪半径（路），任务书要求 3~4
        "candidate_radius": 1,     # 候选点取失误点周围半径（路）
        "max_candidates": 12,      # 单题验证候选点上限
        "verify_profile": {        # 各主题验证深度（对应 katago.max_visits）
            "life_death": "fine",
            "capturing_race": "fine",
            "endgame": "standard",
            "middle": "standard",
        },
        "urgency_max_winrate": 0.3,  # 死活/对杀题要求 pass 后胜率低于此值
        "endgame_move_fraction": 0.8,  # 手数占比超过此值判为官子阶段
        # M3 提取（T5a）：未定型信号扫描阈值
        "extract_signal_threshold": 0.06,  # 相邻两手 delta 反转的最小幅值
        "endgame_swing_threshold": 0.12,   # 终局附近 winrate 波动阈值
        "extract_review_timeout": 900,     # sgf_text 提交复盘后的等待超时（秒）
    },
    "server": {"port": 8765, "log_file": "data/app.log"},  # log_file：服务日志（>5MB 轮转）
    "budget": {"token_limit_month": 30},
}

_cache: dict[str, Any] | None = None
_lock = threading.Lock()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并：override 覆盖 base，两个 dict 都有的键按 dict 递归合并。"""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _load_config() -> dict[str, Any]:
    _ensure_config()
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text("", encoding="utf-8")
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raw = {}
    return _deep_merge(DEFAULT_CONFIG, raw)


def get_settings() -> dict[str, Any]:
    """返回当前配置（深合并默认值后）。"""
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load_config()
        return copy.deepcopy(_cache)


def save_settings(updates: dict[str, Any]) -> dict[str, Any]:
    """把增量更新合并进当前配置，写回 config.yaml，刷新缓存并返回新配置。

    ``updates`` 中的键对应 config 顶层结构（如 ``{"review": {"profile": "standard"}}``）。
    """
    global _cache
    with _lock:
        current = _cache if _cache is not None else _load_config()
        new_config = _deep_merge(current, updates)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CONFIG_PATH.open("w", encoding="utf-8") as f:
            yaml.safe_dump(new_config, f, allow_unicode=True, sort_keys=False)
        _cache = new_config
        return copy.deepcopy(new_config)
