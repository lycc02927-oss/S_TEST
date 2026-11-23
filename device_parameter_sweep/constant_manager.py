"""复用已有 manager；补充无设备写入的预检及仅恢复目标字段的功能。"""

from cpd_modules.cpqpbop_test_constant_manager import TestConstantManager
from sw.cm.ConstantManager import OK
from sw.cm.gen.python import CMServiceMsg_pb2


class SweepConstantManager(TestConstantManager):
    __test__ = False

    def _parent(self, root, path):
        prefix, _, name = path.rpartition(".")
        return (self._get_nested_attr(root, prefix) if prefix else root), name

    def validate_values(self, values):
        """在 proto 副本上赋值，检查部署版本的真实路径/类型，不调用 WriteData。"""
        data = type(self.cpqpbop_tc)()
        data.CopyFrom(self.cpqpbop_tc)
        for path, value in values.items():
            self._set_nested_attr(data, path, value)

    def snapshot_fields(self, paths):
        states = {}
        for path in sorted(paths):
            parent, name = self._parent(self.cpqpbop_tc, path)
            value = self._get_nested_attr(self.cpqpbop_tc, path)
            if not isinstance(value, (str, int, float, bool)):
                raise ValueError("仅支持 proto 标量常数: " + path)
            states[path] = {"present": parent.HasField(name), "value": value}
        return states

    def restore_fields(self, states):
        """最新整对象上仅恢复曾修改的字段，保留 CPD 更新的其他字段及 proto2 presence。"""
        ret = self._load_data()
        if ret != OK:
            return ret
        data = type(self.cpqpbop_tc)()
        data.CopyFrom(self.cpqpbop_tc)
        for path, state in states.items():
            parent, name = self._parent(data, path)
            if state["present"]:
                setattr(parent, name, state["value"])
            else:
                parent.ClearField(name)
        ret = self.constant_manager.WriteData(
            "CPQPBOPTC", data, "Restore parameter sweep fields.", "CPD Calibration",
            constanttype=CMServiceMsg_pb2.CM_TEST_CONSTANT_TYPE,
        )
        if ret == OK:
            self.cpqpbop_tc.CopyFrom(data)
        return ret
