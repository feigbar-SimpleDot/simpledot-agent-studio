"""pytest 全局 conftest(i18n v1.0,架构 §6 / D3 零回归锚)。

将后端 `mw.i18n` 钉在 zh-cn 部署时钟下,保证 36 个 pytest 文件
既有中文断言零改即绿(`LANG=zh-cn` 部署下,t() 返回的 zh-cn 字面
与改造前逐字相同)。

实现注意: 必须在 `from mw.* import ...` 之前设置 `MW_LANG` env,
且重置 mw.i18n 模块缓存(避免别的测试提前 import 触发 _load,
缓存到 en 翻译)。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 把 repo 根塞进 path,让所有 `import mw.xxx` 找到本地包
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 钉部署时钟为 zh-cn(架构 §6.1 / D3)
os.environ.setdefault("MW_LANG", "zh-cn")
# pytest 自身的 locale 不影响 mw 解析,但避免 POSIX LANG 污染(架构 §3.1)
os.environ.setdefault("LANG", "en_US.UTF-8")

# 强制 mw.i18n 在 conftest 阶段就处于 zh-cn 时钟下
import mw.i18n  # noqa: E402  (必须在 sys.path 调整后)
mw.i18n._cache.clear()
mw.i18n._LOADED.clear()