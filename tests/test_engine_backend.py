"""T1 测试：引擎后端自动探测/选择（不依赖真机 GPU，mock 探测函数）。

覆盖任务书验收：
- 强制 cpu → eigenavx2；强制 opencl → opencl；
- auto 探测失败 → 回退 cpu；auto 探测成功 → opencl；
- 探测结果写入 config（detected_backend）且不覆盖手动指定值；
- 探测结果进程级缓存（不每次重探测）+ 持久化结果复用；
- restart 临时切换与非法值校验。
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from backend.services.engine import backend as engine_backend

# 不依赖真实引擎文件：所有可执行文件/模型路径仅作字符串断言
CPU_EXE = "engine/katago-eigenavx2.exe"
OPENCL_EXE = "engine/katago-opencl.exe"


def _cfg(mode: str = "auto", **extra: str) -> dict:
    katago = {
        "executable": OPENCL_EXE,
        "executable_cpu": CPU_EXE,
        "model": "engine/b10c128.bin.gz",
        "backend": mode,
    }
    katago.update(extra)
    return {"katago": katago}


class TestChooseBackend(unittest.TestCase):
    def setUp(self) -> None:
        engine_backend.reset_state()
        # 任何意外写盘都被拦截（测试不得污染真实 config.yaml）
        self.save_patch = mock.patch.object(
            engine_backend.settings_mod, "save_settings"
        )
        self.mock_save = self.save_patch.start()
        self.addCleanup(self.save_patch.stop)

    def _no_probe(self):
        """探测不应被调用的场景。"""
        with mock.patch.object(
            engine_backend, "detect_opencl", side_effect=AssertionError("不应探测")
        ) as probe:
            return probe

    def test_force_cpu_uses_eigenavx2(self) -> None:
        probe = self._no_probe()
        choice = engine_backend.choose_backend(_cfg("cpu"))
        self.assertEqual(choice.name, "eigenavx2")
        self.assertEqual(choice.mode, "cpu")
        self.assertTrue(choice.executable.replace("\\", "/").endswith(CPU_EXE))
        self.assertFalse(choice.detected)
        probe.assert_not_called()
        self.mock_save.assert_not_called()  # 手动指定不写 detected_backend

    def test_force_opencl_uses_opencl(self) -> None:
        probe = self._no_probe()
        choice = engine_backend.choose_backend(_cfg("opencl"))
        self.assertEqual(choice.name, "opencl")
        self.assertEqual(choice.mode, "opencl")
        self.assertTrue(
            choice.executable.replace("\\", "/").endswith(OPENCL_EXE)
        )
        probe.assert_not_called()
        self.mock_save.assert_not_called()

    def test_auto_probe_fail_falls_back_to_cpu(self) -> None:
        with mock.patch.object(
            engine_backend, "detect_opencl", return_value=(False, "模拟：OpenCL 不可用")
        ):
            choice = engine_backend.choose_backend(_cfg("auto"))
        self.assertEqual(choice.name, "eigenavx2")
        self.assertTrue(choice.detected)
        self.assertIn("回退", choice.reason)
        # 探测失败也写 detected_backend=eigenavx2（幂等缓存，避免下次再探测）
        self.mock_save.assert_called_once()
        args, _ = self.mock_save.call_args
        self.assertEqual(args[0], {"katago": {"detected_backend": "eigenavx2"}})

    def test_auto_probe_success_uses_opencl(self) -> None:
        with mock.patch.object(
            engine_backend,
            "detect_opencl",
            return_value=(True, "KataGo v1.18.1 (OpenCL)"),
        ):
            choice = engine_backend.choose_backend(_cfg("auto"))
        self.assertEqual(choice.name, "opencl")
        self.assertTrue(choice.detected)
        self.mock_save.assert_called_once()
        args, _ = self.mock_save.call_args
        self.assertEqual(args[0], {"katago": {"detected_backend": "opencl"}})

    def test_auto_probe_persist_disabled(self) -> None:
        """persist=False（请求路径）：探测但不写 config.yaml。"""
        with mock.patch.object(
            engine_backend, "detect_opencl", return_value=(True, "ok")
        ):
            choice = engine_backend.choose_backend(_cfg("auto"), persist=False)
        self.assertEqual(choice.name, "opencl")
        self.mock_save.assert_not_called()

    def test_auto_reuses_persisted_detected_backend(self) -> None:
        """config 已有 detected_backend 时不重探测、不写盘。"""
        probe = self._no_probe()
        cfg = _cfg("auto", detected_backend="opencl")
        choice = engine_backend.choose_backend(cfg)
        self.assertEqual(choice.name, "opencl")
        probe.assert_not_called()
        self.mock_save.assert_not_called()

    def test_probe_cached_per_process(self) -> None:
        """同一进程两次选择只探测一次。"""
        probe = mock.MagicMock(return_value=(True, "ok"))
        with mock.patch.object(engine_backend, "detect_opencl", new=probe):
            c1 = engine_backend.choose_backend(_cfg("auto"), persist=False)
            c2 = engine_backend.choose_backend(_cfg("auto"), persist=False)
        self.assertEqual(c1.name, "opencl")
        self.assertEqual(c2.name, "opencl")
        self.assertEqual(probe.call_count, 1)

    def test_manual_backend_not_overwritten_by_detected(self) -> None:
        """手动指定 cpu 时，即使探测可用也不写 detected_backend。"""
        with mock.patch.object(
            engine_backend, "detect_opencl", return_value=(True, "ok")
        ) as probe:
            choice = engine_backend.choose_backend(_cfg("cpu"))
        self.assertEqual(choice.name, "eigenavx2")
        probe.assert_not_called()
        self.mock_save.assert_not_called()

    def test_model_resolved_absolute(self) -> None:
        choice = engine_backend.choose_backend(_cfg("cpu"))
        self.assertTrue(Path(choice.model).is_absolute())
        self.assertTrue(
            choice.model.replace("\\", "/").endswith("engine/b10c128.bin.gz")
        )


class TestRestartAndResolve(unittest.TestCase):
    def setUp(self) -> None:
        engine_backend.reset_state()
        self.save_patch = mock.patch.object(
            engine_backend.settings_mod, "save_settings"
        )
        self.mock_save = self.save_patch.start()
        self.addCleanup(self.save_patch.stop)

    def test_restart_temporary_switch_and_clear(self) -> None:
        with mock.patch.object(
            engine_backend, "detect_opencl", return_value=(False, "模拟失败")
        ):
            # 临时切 cpu → eigenavx2（不写 config）
            result = engine_backend.restart_engine("cpu")
            self.assertEqual(result["backend"], "eigenavx2")
            self.mock_save.assert_not_called()
            # 不带 body 重启 → 清除临时切换，回到 config 的 auto
            result = engine_backend.restart_engine(None)
            self.assertEqual(result["mode"], "auto")

    def test_restart_invalid_backend_raises(self) -> None:
        with self.assertRaises(ValueError):
            engine_backend.restart_engine("cuda")

    def test_resolve_paths_returns_backend_name(self) -> None:
        executable, model, name = engine_backend.resolve_paths(
            _cfg("cpu"), persist=False
        )
        self.assertEqual(name, "eigenavx2")
        self.assertTrue(Path(executable).is_absolute())
        self.assertTrue(Path(model).is_absolute())


if __name__ == "__main__":
    unittest.main()
