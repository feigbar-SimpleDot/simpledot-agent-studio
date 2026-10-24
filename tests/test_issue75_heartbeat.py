"""issue #75 B1: GET /api/heartbeat 字段契约单测 (ARCH-75 §6.3 / OQ-3)。

  HB-1 心跳新鲜 (<90s) → reaper_alive=true, age 透传
  HB-2 心跳陈旧 (>=90s) → reaper_alive=false, age 仍透传 (前端可展示)
  HB-3 心跳文件缺失 → alive=false, age=None
  HB-4 字段名与 api_workers 顶层逐字一致 (前端顶栏灯换源零改语义, R8)
  HB-5 心跳读取单一源: api_workers 与 api_heartbeat 同 helper, 判定式
       hb_age < 90 逐字保留

issue #75 s9 M-3: 断言真判定化 (check() 只 print 不 raise 曾致 pytest
收集下恒绿; 现每个 test_* 用 @_test_case 包装收尾真 assert, __main__
直跑走 __wrapped__ 保持旧汇总口径)。
"""
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mw.console as console

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  {detail}" if detail else ""))


def _test_case(fn):
    """s9 M-3: 把本用例新增的 FAIL 条目转成 pytest 可见的真实 assert。

    pytest 收集调用包装体 (收尾真 assert, 失败即判红);
    __main__ 直跑路径调用 fn.__wrapped__ (functools.wraps 保留), 保持
    "跑完全部用例再汇总、exit(1 if FAIL)" 旧口径不变。
    """
    import functools

    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        mark = len(FAIL)
        fn(*args, **kwargs)
        assert len(FAIL) == mark, (
            "用例内失败: " + "; ".join(n for n, _ in FAIL[mark:]))
    return _wrapped


def _with_hb(content, fn):
    """把 MW_HOME 指到临时目录 (心跳文件落 <MW_HOME>/reaper_heartbeat)。"""
    with tempfile.TemporaryDirectory() as td:
        if content is not None:
            (Path(td) / "reaper_heartbeat").write_text(content)
        with mock.patch.dict(os.environ, {"MW_HOME": td}):
            return fn()


@_test_case
def test_hb1_fresh():
    d = _with_hb(str(time.time() - 10), console.api_heartbeat)
    check("HB-1.1 新鲜心跳 → alive=true", d["reaper_alive"] is True, str(d))
    check("HB-1.2 age ≈ 10s", 5 < d["reaper_heartbeat_age_s"] < 60, str(d))


@_test_case
def test_hb2_stale():
    d = _with_hb(str(time.time() - 600), console.api_heartbeat)
    check("HB-2.1 陈旧心跳 (>=90s) → alive=false", d["reaper_alive"] is False)
    check("HB-2.2 age 仍透传 (非 None)",
          isinstance(d["reaper_heartbeat_age_s"], float)
          and d["reaper_heartbeat_age_s"] > 500)


@_test_case
def test_hb3_missing():
    d = _with_hb(None, console.api_heartbeat)
    check("HB-3.1 心跳缺失 → alive=false", d["reaper_alive"] is False)
    check("HB-3.2 心跳缺失 → age=None", d["reaper_heartbeat_age_s"] is None)


@_test_case
def test_hb4_field_parity_with_api_workers():
    """HB-4/5: 同一心跳源, 字段名与 api_workers 顶层一致。"""
    def both():
        hb = console.api_heartbeat()
        # api_workers 走真实 registry (容器缺省 → {}), 不探活不受影响
        w = console.api_workers()
        return hb, w
    hb, w = _with_hb(str(time.time() - 20), both)
    check("HB-4.1 两响应字段名集合一致 (reaper 族)",
          set(hb.keys()) <= set(w.keys())
          and {"reaper_alive", "reaper_heartbeat_age_s"} <= set(w.keys()))
    check("HB-4.2 同一心跳文件下 alive 布尔一致",
          hb["reaper_alive"] == w["reaper_alive"],
          f"hb={hb['reaper_alive']} workers={w['reaper_alive']}")
    src = (ROOT / "mw" / "console.py").read_text(encoding="utf-8")
    check("HB-5.1 判定式 hb_age < 90 逐字保留",
          "(hb_age is not None and hb_age < 90)" in src)
    # issue #81-5: api_workers 复用 _reaper_hb_age() 单一读取源 (原先
    # 内联重读心跳文件)。该断言在修复前为 False → 真实红→绿判定力。
    import inspect
    workers_src = inspect.getsource(console.api_workers)
    check("HB-5.2 api_workers 已复用 _reaper_hb_age() 单一源",
          "_reaper_hb_age()" in workers_src, workers_src[:200])
    check("HB-5.3 api_workers 不再内联读心跳文件 (路径字面量已移出)",
          '"reaper_heartbeat"' not in workers_src, workers_src[:200])


if __name__ == "__main__":
    for fn in [test_hb1_fresh, test_hb2_stale, test_hb3_missing,
               test_hb4_field_parity_with_api_workers]:
        print(f"== {fn.__name__}")
        # 直跑口径: 走 __wrapped__ 绕过 pytest 化 assert, 保持
        # "全部跑完再汇总 + exit(1)" 旧行为 (M-3 双轨兼容)。
        getattr(fn, "__wrapped__", fn)()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
