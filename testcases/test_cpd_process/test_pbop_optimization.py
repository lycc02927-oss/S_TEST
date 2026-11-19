"""用户提供的寻优循环测试：默认沿用当前常数，重复运行 10 轮。"""

import os
import sys
from pathlib import Path

os.environ["QT_API"] = "pyside2"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import logging
import pytest
from cpfr import init_dopra_platform
from cpfr.util.testability import cpfr_test  # noqa: F401，注册项目 fixture
from cpqpbop.cpqpbop_app import CpqpbopApp
from cpqpbop.cpqpbop_presenter import CpqpbopPresenter

from cpd_modules.pbop_batch import run_batch

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class TestPbopOptimization:
    init_dopra_platform()
    app_class = CpqpbopApp
    presenter_class = CpqpbopPresenter
    cpd_name = "CPQPBOP"
    subsystem = "pgec"
    component = "cpqpbop"

    def test_optimization_repeat(self, cpfr_test, qtbot):
        run_batch(cpfr_test, qtbot, PROJECT_ROOT / "config/optimization.json", PROJECT_ROOT)


if __name__ == "__main__":
    raise SystemExit(pytest.main([
        __file__, "-k", "test_optimization_repeat", "-s", "-v", "--log-cli-level=INFO",
    ]))
