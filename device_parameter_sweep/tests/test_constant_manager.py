"""用模拟 CM/proto 对象验证生产 manager 的恢复逻辑，不依赖设备 SDK。"""

import copy
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class ScalarMessage:
    def __init__(self):
        object.__setattr__(self, "values", {"gain": 0.1, "other": 7.0, "missing": 0.5})
        object.__setattr__(self, "present", set())

    def __getattr__(self, name):
        values = object.__getattribute__(self, "values")
        if name not in values:
            raise AttributeError(name)
        return values[name]

    def __setattr__(self, name, value):
        if name not in self.values:
            raise AttributeError(name)
        if type(value) not in (int, float):
            raise TypeError("要求数值")
        self.values[name] = value
        self.present.add(name)

    def HasField(self, name):
        return name in self.present

    def ClearField(self, name):
        defaults = {"gain": 0.1, "other": 7.0, "missing": 0.5}
        self.values[name] = defaults[name]
        self.present.discard(name)


class RootMessage:
    def __init__(self):
        self.scenario = ScalarMessage()

    def CopyFrom(self, other):
        self.scenario = copy.deepcopy(other.scenario)


class FakeCM:
    def __init__(self):
        self.data = RootMessage()
        self.data.scenario.gain = 0.2
        self.writes = 0
        self.write_error = 0

    def ReadData(self, name, data, version, constant_type):
        data.CopyFrom(self.data)
        return 0

    def WriteData(self, name, data, description, scene, constanttype):
        self.writes += 1
        if not self.write_error:
            self.data.CopyFrom(data)
        return self.write_error


def load_manager_class():
    module_names = [
        "cpfr", "cpqpbop", "cpqpbop.gen", "cpqpbop.gen.python",
        "cpqpbop.gen.python.CPQPBOPTC_pb2", "sw", "sw.cm", "sw.cm.ConstantManager",
        "sw.cm.gen", "sw.cm.gen.python", "sw.cm.gen.python.CMServiceMsg_pb2",
    ]
    modules = {name: types.ModuleType(name) for name in module_names}
    for module in modules.values():
        module.__path__ = []
    modules["cpfr"].init_dopra_platform = lambda: None
    modules["cpqpbop.gen.python.CPQPBOPTC_pb2"].CPQPBOPTC = RootMessage
    modules["sw.cm.ConstantManager"].ConstantManager = FakeCM
    modules["sw.cm.ConstantManager"].OK = 0
    modules["sw.cm.gen.python.CMServiceMsg_pb2"].CM_TEST_CONSTANT_TYPE = 1
    path = Path(__file__).resolve().parents[1] / "constant_manager.py"
    spec = importlib.util.spec_from_file_location("_sweep_manager_under_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module.SweepConstantManager


class ConstantManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager_class = load_manager_class()

    def test_preflight_uses_copy_without_writing(self):
        manager = self.manager_class()
        manager.validate_values({"scenario.gain": 0.3})
        self.assertEqual(manager.read_test_constant("scenario.gain"), 0.2)
        self.assertEqual(manager.constant_manager.writes, 0)
        for values, error in (({"scenario.typo": 1.0}, AttributeError), ({"scenario.gain": "x"}, TypeError)):
            with self.assertRaises(error):
                manager.validate_values(values)
        self.assertEqual(manager.constant_manager.writes, 0)

    def test_restore_preserves_other_device_updates_and_field_presence(self):
        manager = self.manager_class()
        original = manager.snapshot_fields(["scenario.gain", "scenario.missing"])
        self.assertFalse(original["scenario.missing"]["present"])
        self.assertEqual(manager.write_test_constants({"scenario.gain": 0.4, "scenario.missing": 0.9}), 0)
        # 模拟算法在设备侧更新非测试字段，本地对象仍是旧值。
        manager.constant_manager.data.scenario.other = 123.0
        self.assertEqual(manager.restore_fields(original), 0)
        self.assertEqual(manager.snapshot_fields(original), original)
        self.assertEqual(manager.read_test_constant("scenario.other"), 123.0)
        self.assertFalse(manager.cpqpbop_tc.scenario.HasField("missing"))

    def test_restore_write_failure_returns_error_without_local_fake_success(self):
        manager = self.manager_class()
        original = manager.snapshot_fields(["scenario.gain"])
        manager.write_test_constants({"scenario.gain": 0.4})
        manager.constant_manager.write_error = 77
        self.assertEqual(manager.restore_fields(original), 77)
        self.assertEqual(manager.read_test_constant("scenario.gain"), 0.4)


if __name__ == "__main__":
    unittest.main()
