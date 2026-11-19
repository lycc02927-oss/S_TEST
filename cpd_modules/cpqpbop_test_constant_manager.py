"""CPQPBOP 测试常数读取与写入；字段路径使用 proto 原始字段名。"""

import logging
from functools import reduce

from cpfr import init_dopra_platform
from cpqpbop.gen.python.CPQPBOPTC_pb2 import CPQPBOPTC
from sw.cm.ConstantManager import ConstantManager, OK
from sw.cm.gen.python import CMServiceMsg_pb2


class TestConstantManager:
    __test__ = False

    def __init__(self):
        init_dopra_platform()
        self.constant_manager = ConstantManager()
        self.cpqpbop_tc = None
        self._load_data()

    def _load_data(self):
        """读取或重新加载整个 CPQPBOP 测试常数对象。"""
        self.cpqpbop_tc = None
        data = CPQPBOPTC()
        err_code = self.constant_manager.ReadData(
            "CPQPBOPTC", data, -1, CMServiceMsg_pb2.CM_TEST_CONSTANT_TYPE
        )
        if err_code != OK:
            logging.error("Read CPQPBOPTC failed! err_code = %s", err_code)
            return err_code
        self.cpqpbop_tc = data
        return OK

    def _get_nested_attr(self, root, name):
        if not isinstance(name, str) or not name or any(
            not part for part in name.split(".")
        ):
            raise ValueError("属性路径必须是非空字符串，且不能包含空层级")
        try:
            return reduce(getattr, name.split("."), root)
        except AttributeError as exc:
            raise AttributeError("属性路径 {!r} 无效: {}".format(name, exc)) from exc

    def _set_nested_attr(self, root, name, value):
        self._get_nested_attr(root, name)
        parent_path, _, attr_name = name.rpartition(".")
        parent = self._get_nested_attr(root, parent_path) if parent_path else root
        setattr(parent, attr_name, value)

    def read_test_constant(self, name):
        """从已加载对象读取；如需设备最新值，先调用 _load_data()。"""
        if self.cpqpbop_tc is None:
            logging.error("CPQPBOPTC 未加载成功，请先调用 _load_data() 重试")
            return None
        try:
            value = self._get_nested_attr(self.cpqpbop_tc, name)
        except (AttributeError, TypeError, ValueError) as exc:
            logging.error("Read CPQPBOPTC.%s failed: %s", name, exc)
            return None
        logging.info("CPQPBOPTC.%s = %s", name, value)
        return value

    def write_test_constant_data(self, name, value):
        """保留原来的单字段写入接口。"""
        return self.write_test_constants({name: value})

    def write_test_constants(self, values):
        """在副本上修改多个标量字段，验证后一次性写入整个对象。"""
        if self.cpqpbop_tc is None:
            logging.error("CPQPBOPTC 未加载成功，无法写入")
            return -1
        if not values:
            return OK
        data = CPQPBOPTC()
        data.CopyFrom(self.cpqpbop_tc)
        try:
            for name, value in values.items():
                self._set_nested_attr(data, name, value)
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            logging.error("Set CPQPBOPTC failed: %s", exc)
            return -1
        err_code = self.constant_manager.WriteData(
            "CPQPBOPTC", data, "Updated parameters.", "CPD Calibration",
            constanttype=CMServiceMsg_pb2.CM_TEST_CONSTANT_TYPE,
        )
        if err_code != OK:
            logging.error("Write CPQPBOPTC failed! err_code = %s", err_code)
            return err_code
        self.cpqpbop_tc.CopyFrom(data)
        logging.info("Write CPQPBOPTC succeeded: %s", values)
        return OK
