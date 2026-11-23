"""显式运行此文件才连接设备；默认 pytest.ini 的离线测试不会收集它。"""

import os
import sys
from pathlib import Path

os.environ["QT_API"] = "pyside2"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import logging

import pytest
from cpfr import init_dopra_platform
from cpfr.util.testability import cpfr_test  # noqa: F401，沿用用户的 fixture 注册方式
from cpqpbop.cpqpbop_app import CpqpbopApp
from cpqpbop.cpqpbop_presenter import CpqpbopPresenter

from device_parameter_sweep.runner import run_sweep

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class TestDeviceParameterSweep:
    init_dopra_platform()
    app_class = CpqpbopApp
    presenter_class = CpqpbopPresenter
    cpd_name = "CPQPBOP"
    subsystem = "pgec"
    component = "cpqpbop"

    def test_search_parameters(self, cpfr_test, qtbot):
        run_sweep(cpfr_test, qtbot, PROJECT_ROOT)


if __name__ == "__main__":
    raise SystemExit(pytest.main([
        __file__, "-k", "test_search_parameters", "-s", "-v", "--log-cli-level=INFO",
    ]))
