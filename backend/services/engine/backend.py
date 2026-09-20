"""KataGo 引擎后端选择与探测（T1：GPU/CPU 自动适配）。

目标：**电脑无论有没有 GPU 都能用**。

- ``katago.backend``：``auto | cpu | opencl``（默认 auto）；
  - ``cpu`` → ``katago.executable_cpu``（Eigen/AVX2 纯 CPU 版）；
  - ``opencl`` → ``katago.executable``（OpenCL 版）；
  - ``auto`` → 快速探测 OpenCL 可用性，失败自动回退 CPU。
- **探测必须快速**：禁止触发 OpenCL kernel 编译（首次约 5 分钟）。
  用 ``katago-opencl.exe version`` 子命令（秒级返回，失败即不可用），
  超时 15s，子进程 cwd 设为 exe 所在目录（找到运行库 DLL），
  Windows 下加 ``CREATE_NO_WINDOW`` 避免弹窗。
- 结果缓存：进程级缓存（禁止每次请求重探测）+ 幂等写入 config.yaml
  的 ``katago.detected_backend``（``backend`` 保持 auto 时写；手动指定
  cpu/opencl 时不动）。
- 引擎实例工厂：``resolve_paths() -> (executable, model, backend_name)``，
  供 ``analyze_sgf`` / ``verify`` 复用。
- 引擎状态注册：``KataGoEngine`` 启动/失败时登记（weakref），供
  ``/system/engine/status`` 与 ``/system/health`` 报告真实存活状态。

日志：探测结果、回退原因、引擎错误均写入服务日志（common.serverlog）。
"""
from __future__ import annotations

import os
import subprocess
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ...common import serverlog
from ...common.settings import BASE_DIR, get_settings
from ...common import settings as settings_mod

PROBE_TIMEOUT = 15.0        # version 子命令超时（秒级命令，放宽到 15s）
DEFAULT_CPU_EXE = "engine/katago-eigenavx2.exe"
DEFAULT_OPENCL_EXE = "engine/katago.exe"
DEFAULT_MODEL = "engine/b10c128.bin.gz"

# Windows：子进程不弹控制台窗口
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass
class BackendChoice:
    """一次后端选择的结果。"""

    name: str          # 实际后端名："opencl" | "eigenavx2"
    mode: str          # 用户配置值："auto" | "cpu" | "opencl"
    executable: str    # 绝对路径
    model: str         # 绝对路径
    reason: str        # 选择原因（日志/接口展示）
    detected: bool     # 是否由探测得出（auto 模式且本次/历史探测结果生效）


_lock = threading.Lock()
_cache: Optional[BackendChoice] = None
_runtime_override: Optional[str] = None     # POST /engine/restart 的临时切换（不持久化）
_engines: "weakref.WeakSet" = weakref.WeakSet()   # 在册引擎实例（进程退出自动移除）
_last_error = ""


def _abs(base: Path, rel: str | Path) -> str:
    """相对路径按项目根转绝对路径。"""
    p = Path(rel).expanduser()
    if not p.is_absolute():
        p = (base / p).resolve()
    return str(p)


def _cfg_executable(cfg: dict, kind: str) -> str:
    """kind=cpu → executable_cpu；kind=opencl → executable。

    平台容错：config 里的默认路径是 Windows 写法（engine/katago-opencl.exe），
    实际文件不存在时 Windows 尝试补 .exe、非 Windows 尝试去掉 .exe——
    Linux 用户下载脚本落盘的是无后缀裸二进制。
    """
    katago = dict(cfg.get("katago") or {})
    if kind == "cpu":
        rel = katago.get("executable_cpu") or DEFAULT_CPU_EXE
    else:
        rel = katago.get("executable") or DEFAULT_OPENCL_EXE
    path = _abs(BASE_DIR, rel)
    if not os.path.isfile(path):
        if os.name == "nt" and not path.endswith(".exe") \
                and os.path.isfile(path + ".exe"):
            return path + ".exe"
        if os.name != "nt" and path.endswith(".exe") \
                and os.path.isfile(path[:-4]):
            return path[:-4]
    return path


def _cfg_model(cfg: dict) -> str:
    katago = dict(cfg.get("katago") or {})
    return _abs(BASE_DIR, katago.get("model") or DEFAULT_MODEL)


def _cfg_backend(cfg: dict) -> str:
    mode = str((cfg.get("katago") or {}).get("backend") or "auto").lower()
    return mode if mode in ("auto", "cpu", "opencl") else "auto"


# ---------------------------------------------------------------------------
# OpenCL 快速探测
# ---------------------------------------------------------------------------

def detect_opencl(
    executable: str,
    timeout: float = PROBE_TIMEOUT,
) -> tuple[bool, str]:
    """快速探测 OpenCL 可用性：运行 ``<exe> version``（秒级返回）。

    返回 (是否可用, 证据/失败原因)。不触发模型加载与 kernel 编译。
    """
    exe = Path(executable)
    if not exe.exists():
        return False, f"可执行文件不存在: {exe}"
    cmd = [str(exe), "version"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(exe.parent),
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        return False, f"version 探测超时（>{timeout}s）: {exc}"
    except OSError as exc:
        return False, f"启动失败: {exc}"
    if proc.returncode == 0:
        first = (proc.stdout or "").strip().splitlines()
        evidence = first[0].strip() if first else "version 返回 0"
        return True, evidence
    tail = (proc.stderr or "").strip().replace("\n", " ")[-300:]
    return False, f"version 退出码 {proc.returncode}: {tail}"


# ---------------------------------------------------------------------------
# 选择与缓存
# ---------------------------------------------------------------------------

def _auto_select(
    cfg: dict, model: str, force_probe: bool, persist: bool
) -> BackendChoice:
    katago = dict(cfg.get("katago") or {})
    persisted = str(katago.get("detected_backend") or "").lower()
    if not force_probe and persisted in ("opencl", "eigenavx2"):
        if persisted == "opencl":
            exe = _cfg_executable(cfg, "opencl")
            if Path(exe).exists():
                return BackendChoice(
                    "opencl", "auto", exe, model,
                    "复用上次探测结果（config katago.detected_backend=opencl）",
                    detected=True,
                )
        else:
            return BackendChoice(
                "eigenavx2", "auto", _cfg_executable(cfg, "cpu"), model,
                "复用上次探测结果（config katago.detected_backend=eigenavx2）",
                detected=True,
            )
    exe_opencl = _cfg_executable(cfg, "opencl")
    ok, evidence = detect_opencl(exe_opencl)
    if ok:
        if persist:
            _persist_detected(cfg, "opencl")
        return BackendChoice(
            "opencl", "auto", exe_opencl, model,
            f"OpenCL 探测成功（{evidence}）", detected=True,
        )
    cpu_exe = _cfg_executable(cfg, "cpu")
    reason = f"OpenCL 不可用（{evidence}），自动回退 CPU（Eigen/AVX2）"
    serverlog.warning(f"[engine] {reason}")
    if persist:
        _persist_detected(cfg, "eigenavx2")
    return BackendChoice("eigenavx2", "auto", cpu_exe, model, reason, detected=True)


def _persist_detected(cfg: dict, name: str) -> None:
    """探测结果幂等写入 config.yaml（backend=auto 才写；手动指定不动）。"""
    katago = dict(cfg.get("katago") or {})
    if _cfg_backend(cfg) != "auto":
        return
    if str(katago.get("detected_backend") or "").lower() == name:
        return  # 已一致，幂等跳过
    try:
        settings_mod.save_settings({"katago": {"detected_backend": name}})
        serverlog.info(
            f"[engine] 探测结果写入 config.yaml: katago.detected_backend={name}"
        )
    except Exception as exc:  # 写失败不阻断选择
        serverlog.error(f"[engine] 探测结果写入 config.yaml 失败: {exc}")


def choose_backend(
    config: Optional[dict] = None,
    persist: bool = True,
    force_probe: bool = False,
) -> BackendChoice:
    """选择引擎后端（进程级缓存，禁止每次请求重探测）。

    - ``config``：不传则读全局配置；
    - ``persist``：auto 探测后是否写 detected_backend 到 config.yaml
      （服务启动时 True，请求路径 False，避免请求期写盘）；
    - ``force_probe``：忽略 config 里已有的 detected_backend 重新探测。
    """
    global _cache
    cfg = dict(config or get_settings())
    mode = _cfg_backend(cfg)
    effective = _runtime_override or mode      # 临时切换优先
    with _lock:
        cached = _cache
    if (
        cached is not None
        and cached.mode == effective
        and not force_probe
    ):
        return cached

    model = _cfg_model(cfg)
    if effective == "cpu":
        choice = BackendChoice(
            "eigenavx2", mode, _cfg_executable(cfg, "cpu"), model,
            "手动指定 cpu 后端（katago.backend=cpu）", detected=False,
        )
    elif effective == "opencl":
        choice = BackendChoice(
            "opencl", mode, _cfg_executable(cfg, "opencl"), model,
            "手动指定 opencl 后端（katago.backend=opencl）", detected=False,
        )
    else:
        choice = _auto_select(cfg, model, force_probe, persist)
    with _lock:
        _cache = choice
    serverlog.info(f"[engine] 后端选择: {choice.name}（{choice.reason}）")
    return choice


def resolve_paths(
    config: Optional[dict] = None,
    persist: bool = True,
) -> tuple[str, str, str]:
    """引擎实例工厂：返回 (executable, model, backend_name)。

    供 ``analyze_sgf._resolve_paths`` / ``verify`` 复用。
    """
    choice = choose_backend(config=config, persist=persist)
    return choice.executable, choice.model, choice.name


# ---------------------------------------------------------------------------
# 引擎进程状态（供 /system/engine/status、/system/health）
# ---------------------------------------------------------------------------

def track_engine(engine: Any) -> None:
    """登记引擎实例（weakref：进程退出/对象回收后自动移除）。"""
    with _lock:
        _engines.add(engine)


def engine_alive() -> bool:
    """是否存在存活的分析引擎进程。"""
    with _lock:
        engines = list(_engines)
    return any(e.is_alive() for e in engines)


def alive_engine_count() -> int:
    with _lock:
        engines = list(_engines)
    return sum(1 for e in engines if e.is_alive())


def note_engine_error(message: str) -> None:
    """记录最近一次引擎错误（截断到 500 字符）。"""
    global _last_error
    with _lock:
        _last_error = message[:500]
    serverlog.error(f"[engine] {message}")


def last_engine_error() -> str:
    with _lock:
        return _last_error


def set_runtime_override(backend: Optional[str]) -> None:
    """POST /engine/restart 的临时切换（不持久化，服务重启即失效）。"""
    global _runtime_override, _cache
    with _lock:
        if backend is None:
            _runtime_override = None
        else:
            b = backend.lower()
            if b not in ("auto", "cpu", "opencl"):
                raise ValueError(f"非法后端: {backend}（可选 auto|cpu|opencl）")
            _runtime_override = b
        _cache = None  # 强制下次重新选择


def restart_engine(backend: Optional[str] = None) -> dict:
    """重启引擎：停止在册引擎进程，按需临时切换后端，返回选择结果。

    - 带 ``backend``：临时切换（写进程内 override，不写 config）；
    - 不带：清除临时切换，回到 config 配置值。
    """
    set_runtime_override(backend)
    with _lock:
        engines = list(_engines)
    for e in engines:
        try:
            e.stop(timeout=5.0)
        except Exception as exc:
            serverlog.warning(f"[engine] 停止旧引擎进程失败: {exc}")
    choice = choose_backend(persist=False, force_probe=True)
    serverlog.info(f"[engine] 重启完成: backend={choice.name}（{choice.reason}）")
    return {
        "backend": choice.name,
        "mode": choice.mode,
        "reason": choice.reason,
    }


def reset_state() -> None:
    """清空进程级缓存与临时切换（测试用）。"""
    global _cache, _runtime_override, _last_error
    with _lock:
        _cache = None
        _runtime_override = None
        _last_error = ""
