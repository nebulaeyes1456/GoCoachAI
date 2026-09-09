"""KataGo 引擎封装（窗口1）。

- 进程生命周期管理：启动、心跳（query_version）、自动重启、优雅退出；
- 使用 ``katago analysis`` 协议（stdin/stdout JSON 行，见 KataGo
  docs/Analysis_Engine.md）：
    * 查询为单行 JSON：{"id", "moves", "rules", "komi", "boardXSize",
      "boardYSize", "analyzeTurns", "maxVisits", ...}；
    * 每个局面一条最终响应（isDuringSearch=false）：
      {"id", "turnNumber", "moveInfos": [...], "rootInfo": {...}}；
    * ``reportAnalysisWinratesAs = SIDETOMOVE``（见 engine/analysis_example.cfg），
      因此 moveInfos/rootInfo 的 winrate 均为"当前行棋方"视角 0~1。
- 线程安全：单读线程 + 按 id 分发的 pending 字典，支持并发提交、
  乱序返回、超时。

用法（供 verify.py / 测试 / 窗口3 复用）：
    engine = KataGoEngine(executable, model, config_path, threads)
    engine.start()
    resp = engine.query({...})            # 阻塞到最终响应
    engine.stop()
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

READ_TIMEOUT = 1.0  # 读线程轮询间隔（秒）
START_RETRIES = 3
RESTART_SLEEP = 2.0


def _abs_path(path: str | Path) -> str:
    """相对路径按当前进程 cwd 转绝对路径（Windows 下也规范化）。"""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    return str(p)


class EngineError(RuntimeError):
    """引擎进程异常（启动失败 / 运行中崩溃 / 响应错误）。"""


class KataGoEngine:
    """``katago analysis`` 引擎进程封装。"""

    def __init__(
        self,
        executable: str | Path,
        model: str | Path,
        config_path: str | Path,
        analysis_threads: int = 1,
        search_threads: Optional[int] = None,
        extra_args: Optional[list[str]] = None,
    ) -> None:
        # 统一转绝对路径：子进程 cwd 为 exe 目录，相对路径会解析失败
        self.executable = _abs_path(executable)
        self.model = _abs_path(model)
        self.config_path = _abs_path(config_path)
        # 并行分析的局面数（-analysis-threads）；单局面搜索线程数由 cfg 的
        # numSearchThreads 控制，也可用 search_threads 覆盖（14 核机器：
        # numSearchThreads=12 + numAnalysisThreads=1，避免 CPU 争抢）。
        self.analysis_threads = int(analysis_threads)
        self.extra_args = list(extra_args or [])
        if search_threads is not None:
            self.extra_args += [
                "-override-config", f"numSearchThreads={int(search_threads)}"
            ]

        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()          # 保护进程状态
        self._pending: dict[str, _Pending] = {}
        self._reader: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stopped = threading.Event()
        self._stderr_lines: list[str] = []

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """启动引擎（幂等）。若已运行则直接返回。

        锁外调用 _launch：握手等待期间读线程需要锁分发响应。
        """
        with self._lock:
            if self.is_alive():
                return
        self._launch()

    def _spawn(self) -> subprocess.Popen:
        """Popen 引擎并启动读线程（调用方需持锁）。"""
        cmd = [
            self.executable,
            "analysis",
            "-config", self.config_path,
            "-model", self.model,
            "-analysis-threads", str(self.analysis_threads),
            *self.extra_args,
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                # cwd 设为 exe 目录：确保引擎在同目录找到运行库 DLL
                cwd=str(Path(self.executable).parent),
            )
        except OSError as exc:
            raise EngineError(f"无法启动引擎 {self.executable}: {exc}") from exc
        self._proc = proc
        self._stopped.clear()
        self._reader = threading.Thread(
            target=self._read_loop, args=(proc,), daemon=True, name="katago-reader"
        )
        self._reader.start()
        # stderr 必须持续排空，否则管道缓冲区写满会卡死引擎
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop, args=(proc,), daemon=True,
            name="katago-stderr",
        )
        self._stderr_thread.start()
        return proc

    def _launch(self) -> None:
        for attempt in range(1, START_RETRIES + 1):
            with self._lock:
                if self.is_alive():
                    return
                proc = self._spawn()
            # 握手：query_version 就绪即认为引擎可用（首次启动需加载模型，
            # 14 线程 Eigen 后端实测约 60~90s，超时放宽到 300s）。
            # 注意：必须在锁外等待——读线程分发响应需要同一把锁。
            try:
                version = self.query_version(timeout=300)
                if not version:
                    raise EngineError("引擎启动后 query_version 无响应")
                _notify_engine_started(self)
                return
            except Exception as exc:
                self._terminate(proc)
                with self._lock:
                    if self._proc is proc:
                        self._proc = None
                if attempt >= START_RETRIES:
                    _notify_engine_failed(self, exc)
                    tail = "\n".join(self._stderr_lines[-40:])
                    raise EngineError(
                        f"引擎启动失败（第 {attempt} 次）: {exc}\nstderr 尾部:\n{tail}"
                    ) from exc
                time.sleep(RESTART_SLEEP * attempt)

    def stop(self, timeout: float = 15.0) -> None:
        """优雅退出：关闭 stdin 等引擎自行退出；超时则强杀。"""
        with self._lock:
            self._stopped.set()
            proc, self._proc = self._proc, None
            self._reader = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            proc.kill()
            proc.wait(timeout=5)

    def _terminate(self, proc: subprocess.Popen) -> None:
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

    def is_alive(self) -> bool:
        with self._lock:
            proc = self._proc
            return (
                proc is not None
                and proc.poll() is None
                and proc.stdin is not None
                and not proc.stdin.closed
            )

    def read_stderr_tail(self, n: int = 10) -> str:
        with self._lock:
            return "\n".join(self._stderr_lines[-n:])

    # ------------------------------------------------------------------ 查询协议

    def query(self, request: dict[str, Any], timeout: float = 600.0) -> dict[str, Any]:
        """提交一条分析查询并阻塞等待最终响应（isDuringSearch=false）。

        ``request`` 无需带 id（自动生成 uuid 前缀字符串）；返回该 id 的
        最终响应 JSON（含 moveInfos / rootInfo / turnNumber）。
        """
        qid = request.get("id") or f"go-{uuid.uuid4().hex[:12]}"
        req = dict(request)
        req["id"] = qid
        pending = _Pending()
        # 启动检查在锁外：_launch 内部的握手等待需要读线程拿锁分发响应
        if not self.is_alive():
            self._launch()
        with self._lock:
            proc = self._proc
            if proc is None or proc.stdin is None or proc.stdin.closed:
                raise EngineError("引擎不可用：进程未运行或 stdin 已关闭")
            self._pending[qid] = pending
            try:
                proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            except Exception as exc:
                self._pending.pop(qid, None)
                raise EngineError(f"写入引擎 stdin 失败: {exc}") from exc
        if not pending.event.wait(timeout):
            with self._lock:
                self._pending.pop(qid, None)
            raise EngineError(
                f"查询超时（{timeout}s），id={qid}\nstderr:\n{self.read_stderr_tail()}"
            )
        with self._lock:
            self._pending.pop(qid, None)
        if pending.error is not None:
            raise EngineError(f"引擎返回错误: {pending.error}")
        return pending.response or {"id": qid, "noResults": True}

    def query_version(self, timeout: float = 15.0) -> dict[str, Any]:
        """心跳 / 版本查询。返回 {"id","action","version","git_hash"}。"""
        qid = f"go-ver-{uuid.uuid4().hex[:8]}"
        return self.query({"id": qid, "action": "query_version"}, timeout=timeout)

    # ------------------------------------------------------------------ 读线程

    def _read_loop(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        while not self._stopped.is_set():
            try:
                line = proc.stdout.readline()
            except (ValueError, OSError):
                break
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("isDuringSearch"):
                continue  # 中间报告，忽略（最终报告 isDuringSearch=false）
            qid = data.get("id")
            with self._lock:
                pending = self._pending.get(qid)
            if pending is None:
                continue
            if "error" in data or "warning" in data:
                if "error" in data:
                    pending.error = f"{data.get('error')} field={data.get('field')}"
                    pending.event.set()
                continue
            pending.response = data
            pending.event.set()
            if "noResults" in data:
                with self._lock:
                    self._pending.pop(qid, None)
                continue
        # 读线程退出 → 进程死亡：唤醒所有 pending（stderr 由专门线程处理）
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:
            pass
        with self._lock:
            for p in list(self._pending.values()):
                if not p.event.is_set():
                    p.error = "引擎进程已退出（stdout 关闭）"
                    p.event.set()
            self._pending.clear()
            if self._proc is proc:
                self._proc = None

    def _stderr_loop(self, proc: subprocess.Popen) -> None:
        """持续读取 stderr 防止管道写满阻塞引擎。"""
        try:
            if proc.stderr is None:
                return
            for line in proc.stderr:
                self._append_stderr(line.rstrip())
        except (ValueError, OSError):
            pass
        finally:
            try:
                if proc.stderr is not None:
                    proc.stderr.close()
            except Exception:
                pass

    def _append_stderr(self, line: str) -> None:
        with self._lock:
            self._stderr_lines.append(line)
            if len(self._stderr_lines) > 300:
                self._stderr_lines = self._stderr_lines[-150:]


class _Pending:
    __slots__ = ("event", "response", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: Optional[dict[str, Any]] = None
        self.error: Optional[str] = None


# ---------------------------------------------------------------------------
# 状态通知（T0/T1）：向 backend.py 登记引擎进程状态，供
# /system/engine/status、/system/health 报告真实存活。
# 懒导入 backend 模块避免循环依赖；通知失败不影响引擎本身。
# ---------------------------------------------------------------------------

def _notify_engine_started(engine: "KataGoEngine") -> None:
    try:
        from . import backend as engine_backend

        engine_backend.track_engine(engine)
    except Exception:
        pass


def _notify_engine_failed(engine: "KataGoEngine", exc: Exception) -> None:
    try:
        from . import backend as engine_backend

        name = Path(engine.executable).name if engine.executable else "katago"
        engine_backend.note_engine_error(f"{name} 启动失败: {exc}")
    except Exception:
        pass
