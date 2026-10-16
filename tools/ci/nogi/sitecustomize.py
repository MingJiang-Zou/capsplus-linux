# SPDX-License-Identifier: GPL-2.0-only
# Copyright (c) 2026 capsplus-linux contributors
# 本文件属 capsplus-linux —— CapsLock+ v3.3.0 的 Linux 移植（衍生作品）；按键映射数据译自上游：
#   上游 https://github.com/wo52616111/capslock-plus （GPL-2.0，© wo52616111）；改动见 README「许可与上游」。
"""CI 自检用的"假机器"：把 `gi` / `cairo` 从导入路径里抹掉。

为什么用 sitecustomize 而不是 venv：GitHub runner 上**本来就没装** python3-gi，那条路 CI 已经在走；
这个文件是给**装了 gi 的开发机**用的 —— 一行就能复现"没有 PyGObject 的机器上会发生什么"：

    PYTHONPATH=tools/ci/nogi python3 -m unittest discover -s tests

判据（`tests/test_no_gi_smoke.py`）在 CI 之外也守着这条：测试套件的收集阶段**不许**依赖 gi，
谁在测试模块顶层裸写 `import gi`，这里就红。

机制：往 `sys.meta_path` 插一个 finder，命中这两个顶层名字就抛 ModuleNotFoundError
—— 与"没装"完全等价（Python 找不到模块时也是从 find_spec 抛这个名字与类型）。
"""

import sys

_BLOCKED = ("gi", "cairo")          # cairo = pycairo，gi 的另一种绑法


class _NogiFinder:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in _BLOCKED:
            raise ModuleNotFoundError(
                f"No module named {name!r}（被 tools/ci/nogi/sitecustomize.py 屏蔽，"
                "用于在无 PyGObject 的机器上复现 CI 行为）", name=name)
        return None

    # 老 API：有代码按 find_module 探测，留个空实现别让它意外拿到东西
    def find_module(self, name, path=None):
        return None


sys.meta_path.insert(0, _NogiFinder())
for _name in list(sys.modules):
    if _name.split(".")[0] in _BLOCKED:
        del sys.modules[_name]
