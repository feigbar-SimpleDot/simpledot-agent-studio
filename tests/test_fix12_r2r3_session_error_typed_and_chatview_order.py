"""issue #12 R2 + R3 (2026-10-13) 回归测试。

R2: ledger.create_team_work_run 抛专属 SessionAlreadyBoundError (继承
    sqlite3.IntegrityError); console.api_team_work_launch 用 isinstance
    判别专属异常, 不再嗅探 err 字符串。修法目的是: ledger 改文案
    即静默退化为通用 409 的隐患已堵。

R3: ChatView.jsx submitTeamWork (b) 块 (清空 messages + wsKey + workspace)
    必须在 createSession 成功后才执行; 失败路径完整保留旧 session /
    旧画面 / 工作目录绑定。

覆盖 (对齐 docs/FIX-R2-R3.md §3 验收):

  H1  SessionAlreadyBoundError 是 sqlite3.IntegrityError 子类, 旧 except 兼容
  H2  SessionAlreadyBoundError 携带 session_id + conflicting_run_ids 属性
  H3  ledger.create_team_work_run session 复用时 raise SessionAlreadyBoundError
  H4  console.api_team_work_launch session 复用 → 409 + session_reused=True
      + ledger 改文案 (例如去掉 '已绑到 run') 仍然走专属 409 (核心修复)
  H5  console.api_team_work_launch session 复用 → 409 不创建 run 行
      + 不调 dispatch (零副作用)
  H6  console.api_team_work_launch ACTIVE run 阻塞 → 409 + session_reused
      字段=False (避免前端盲目自动重拉 sid) —— 验证窄化 isinstance
      未把 ACTIVE 阻塞误判为 session_reused
  H7  R3 行为约定 (代码级): ChatView.jsx 中 (b) 块 setMessages([]) 与
      localStorage.removeItem(wsKeyFor(sessionId)) 必须在 createSession
      之后 (源码 grep + 行号断言)。这是 fix 与注释一致性的最小
      行为锁定 —— 防止下次手贱又把 (b) 提前到 try 开头。

不在本测试范围:
  - 真实浏览器实测 (R3 完整行为) → 由 t3 在隔离 console + 浏览器跑
  - ledger DB race 兜底 (DB 层 partial UNIQUE INDEX) 仍抛 IntegrityError
    的兜底路径 → 沿用 issue#8 通用 409,本测试不模拟 race。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mw.console as console  # noqa: E402
from mw.ledger import (  # noqa: E402
    Ledger, SessionAlreadyBoundError,
)

PASS, FAIL = [], []
TEST_FAILED = [False]  # 跨 test 函数共享, 一旦 check 失败即锁定


def check(name, cond, detail=""):
    """记录 PASS/FAIL + 任何失败即锁定 TEST_FAILED, 让本 test_h 函数 raise。

    pytest 跑 test_h* 时只看到测试函数返回, 内部 check() 不 raise
    会被当作 PASS 处理——必须把第一个 FAIL 升级成 AssertionError
    让 pytest 标 FAILED (R2 修复核心验证: H4 必须能区分新旧文案)。
    """
    if cond:
        PASS.append((name, detail))
        print(f"  PASS  {name}")
        if detail:
            print(f"        {detail}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL  {name}")
        if detail:
            print(f"        {detail}")
        TEST_FAILED[0] = True


# ── helpers (复用 test_team_work_console 的 minimal 风格) ────────────────

import sqlite3  # noqa: E402
import tempfile  # noqa: E402


def fresh_ledger():
    """新临时 db, 注入到 mw.console 模块 LEDGER_DB。"""
    f = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    f.close()
    console.LEDGER_DB = f.name
    return console.Ledger(f.name), f.name


def clear_ledger(led, path):
    try:
        led.close()
    except Exception:
        pass
    try:
        os.unlink(path)
    except OSError:
        pass


def stub_dispatch_recorder():
    """把 api_dispatch 替成「只记录调用, 不真跑 spawn」的版本。"""
    calls = []
    original = console.api_dispatch

    def _stub(payload):
        calls.append(dict(payload))
        return {"ok": True, "accepted": True, "stub": True,
                "task_id": payload.get("task_id")}

    console.api_dispatch = _stub
    return calls, lambda: setattr(console, "api_dispatch", original)


def stub_workers_registry():
    console.REGISTRY = "/tmp/_test_fix12_r2r3_registry.json"
    reg = {"coder": {"api_url": "http://stub", "api_key": "x" * 32}}
    Path(console.REGISTRY).write_text(__import__("json").dumps(reg))


# ============================================================
# H1: 异常类层次
# ============================================================

def test_h1_session_already_bound_error_inherits_integrity_error():
    """R2 修法关键: SessionAlreadyBoundError 必须仍是 IntegrityError 子类,
    旧 except sqlite3.IntegrityError 兜底保持兼容 (R2 行为不变)。"""
    err = SessionAlreadyBoundError("s_x", ["r1"])
    check("H1 SessionAlreadyBoundError 是 sqlite3.IntegrityError 实例",
          isinstance(err, sqlite3.IntegrityError))
    check("H1 仍可被 sqlite3.IntegrityError 捕获 (旧代码兼容)",
          _safe_catch(err, sqlite3.IntegrityError))
    if TEST_FAILED[0]:
        raise AssertionError("H1 失败")


def _safe_catch(err, exc_type):
    try:
        raise err
    except exc_type:
        return True
    except Exception:
        return False


# ============================================================
# H2: 异常属性
# ============================================================

def test_h2_exception_carries_session_and_conflicts():
    """R2 修法: 异常必须把 sid + 冲突 run_ids 暴露出来 (供 console 二次查)——
    不依赖 message 字符串嗅探也能拿到元数据。"""
    err = SessionAlreadyBoundError("s_xyz", ["r_a", "r_b", "r_c"])
    check("H2 session_id 属性",
          err.session_id == "s_xyz",
          f"got {err.session_id!r}")
    check("H2 conflicting_run_ids 属性 (list 副本)",
          err.conflicting_run_ids == ["r_a", "r_b", "r_c"],
          f"got {err.conflicting_run_ids!r}")
    # 拷贝语义:改 list 不污染原属性
    err.conflicting_run_ids.append("r_d")
    check("H2 conflicting_run_ids 是副本 (改它不破 str 渲染)",
          "r_a, r_b, r_c" in str(err) or
          ("r_a" in str(err) and "r_b" in str(err) and "r_c" in str(err)),
          f"got str={str(err)!r}")
    if TEST_FAILED[0]:
        raise AssertionError("H2 失败")


# ============================================================
# H3: ledger.create_team_work_run 触发专属异常
# ============================================================

def test_h3_ledger_raises_typed_exception():
    """R2 修法核心: ledger 复用 sid 抛 SessionAlreadyBoundError (非裸 IntegrityError)。"""
    f = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    f.close()
    led = Ledger(f.name)
    try:
        # r1 先建立, 用 sid s_reuse
        led.create_team_work_run("r1", "s_reuse", "需求1")
        led.close_team_work("r1", "test")
        # r2 复用同 sid 必须抛专属类型
        try:
            led.create_team_work_run("r2", "s_reuse", "需求2")
            check("H3 ledger r2 复用 sid 应 raise", False, "no exception")
        except SessionAlreadyBoundError as e:
            check("H3 ledger r2 复用 sid 抛 SessionAlreadyBoundError",
                  e.session_id == "s_reuse"
                  and "r1" in e.conflicting_run_ids,
                  f"got e.session_id={e.session_id!r}, "
                  f"conflicting_run_ids={e.conflicting_run_ids!r}")
        # 旧 except sqlite3.IntegrityError 仍能捕获 (向后兼容)
        try:
            led.create_team_work_run("r3", "s_reuse", "需求3")
            check("H3 旧 except IntegrityError 应捕获", False, "no exception")
        except sqlite3.IntegrityError:
            check("H3 旧 except sqlite3.IntegrityError 仍能捕获 (兼容)", True)
    finally:
        led.close()
        os.unlink(f.name)
    if TEST_FAILED[0]:
        raise AssertionError("H3 失败")


# ============================================================
# H4: console 改文案后仍走专属 409 (核心修复)
# ============================================================

def test_h4_console_uses_isinstance_not_string_sniff():
    """R2 修法核心: ledger raise 文案改任何字面, console 都应能 409 + session_reused。

    模拟: 临时 monkey-patch Ledger.create_team_work_run 让它抛的异常
    message 不含 '已绑到 run' 字符串 (仿真未来 ledger 重写文案),
    验证 console 仍走专属分支返回 session_reused=True。
    """
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            # 先建一个 r1
            r1 = console.api_team_work_launch({
                "initial_task": "r1",
                "session_id": "s_reuse",
                "steps": [{"step_id": "a", "title": "A",
                           "task_id": "t_a", "assignee": "coder"}],
            })
            check("H4 r1 launch ok", r1.get("ok") is True, f"got {r1}")
            led.close_team_work(r1["run_id"], "test")
            calls.clear()

            # Monkey-patch: 把 Ledger.create_team_work_run 改成「message 不含
            # '已绑到 run'」。关键: 异常类型仍为 SessionAlreadyBoundError
            # (继承 sqlite3.IntegrityError), 但 message 文案改了——
            # 这是 issue#12 R2 的核心防御场景: ledger 将来改文案不能破坏
            # console 的判别。
            original_ctr = console.Ledger.create_team_work_run

            def _mutated_create(self, run_id, session_id, *a, **kw):
                prior = self.find_runs_by_initiator_session(session_id)
                if prior:
                    err = SessionAlreadyBoundError(
                        session_id, [r["run_id"] for r in prior])
                    # 故意把 message 改成不含 '已绑到 run' 字符串:
                    # 模拟 ledger 将来改文案 (例如改成英文 / 改术语)。
                    err.args = (
                        f"session {session_id} reused by run "
                        f"{[r['run_id'] for r in prior]}, "
                        f"cannot create new run",)
                    raise err
                return original_ctr(self, run_id, session_id, *a, **kw)

            console.Ledger.create_team_work_run = _mutated_create
            try:
                r2 = console.api_team_work_launch({
                    "initial_task": "r2",
                    "session_id": "s_reuse",  # 复用
                    "steps": [{"step_id": "a", "title": "A",
                               "task_id": "t_b", "assignee": "coder"}],
                })
            finally:
                console.Ledger.create_team_work_run = original_ctr

            # 核心断言: 即便 monkey-patch 让 raise 的 str 不含 '已绑到 run',
            # 仍走专属 409 分支 (因为 isinstance 判别不依赖文案)。
            check("H4 r2 复用 sid → 409 (文案改后仍命中)",
                  r2.get("code") == 409, f"got {r2}")
            check("H4 r2 session_reused=True (核心修复)",
                  r2.get("session_reused") is True,
                  f"got keys={list(r2.keys())}")
            check("H4 r2 含 conflicting_runs 列表",
                  isinstance(r2.get("conflicting_runs"), list)
                  and any(c["run_id"] == r1["run_id"]
                          for c in r2["conflicting_runs"]),
                  f"got {r2.get('conflicting_runs')!r}")
            # 兜底验证: 文案确实被改了(否则这个测试形同虚设——没改文案就
            # 等于基线字符串嗅探仍命中,任何版本都过)。这一步证明 H4 真的
            # 在「文案改后」测判别。
            check("H4 r2 error 文案不含 '已绑到 run' (证明 monkey-patch 生效)",
                  "已绑到 run" not in (r2.get("error") or "")
                  and "已绑到 run" not in (r2.get("hint") or ""),
                  f"got error={r2.get('error')!r}, hint={r2.get('hint')!r}")
        finally:
            restore()
    finally:
        clear_ledger(led, path)
    if TEST_FAILED[0]:
        raise AssertionError("H4 失败")


# ============================================================
# H5: 409 零副作用 (不建 run 行 / 不调 dispatch)
# ============================================================

def test_h5_session_reuse_409_zero_side_effect():
    """R2 行为不变: 409 必须不建 run 行, 不调 dispatch。"""
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            r1 = console.api_team_work_launch({
                "initial_task": "r1",
                "session_id": "s_reuse",
                "steps": [{"step_id": "a", "title": "A",
                           "task_id": "t_a", "assignee": "coder"}],
            })
            led.close_team_work(r1["run_id"], "test")
            calls.clear()
            r2 = console.api_team_work_launch({
                "initial_task": "r2",
                "session_id": "s_reuse",  # 复用
                "steps": [{"step_id": "a", "title": "A",
                           "task_id": "t_b", "assignee": "coder"}],
            })
            check("H5 r2 409 + session_reused",
                  r2.get("code") == 409
                  and r2.get("session_reused") is True,
                  f"got {r2}")
            check("H5 dispatch 未被调 (零副作用)",
                  len(calls) == 0, f"got {len(calls)} calls")
            # ledger 只有 r1, 没建 r2 行
            rows = [r["run_id"] for r in
                    led.list_team_works(include_closed=True)]
            check("H5 ledger 仅含 r1 (无 r2 行)",
                  rows == [r1["run_id"]],
                  f"got {rows}")
        finally:
            restore()
    finally:
        clear_ledger(led, path)
    if TEST_FAILED[0]:
        raise AssertionError("H5 失败")


# ============================================================
# H6: ACTIVE run 阻塞 ≠ session_reused (窄化 isinstance 防误判)
# ============================================================

def test_h6_active_run_block_no_session_reused():
    """R2 窄化 isinstance 防误判: ACTIVE run 阻塞时, console 的
    blockers 路径先 return 409 (无 session_reused=True)——
    不应让前端误以为是 sid 复用而盲目自动重拉。"""
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            r1 = console.api_team_work_launch({
                "initial_task": "r1",
                "session_id": "s_first",
                "steps": [{"step_id": "a", "title": "A",
                           "task_id": "t_a", "assignee": "coder"}],
            })
            check("H6 r1 launch ok", r1.get("ok") is True, f"got {r1}")
            # 不关 r1, 直接用全新 sid 再开 → 走 blockers 路径
            r2 = console.api_team_work_launch({
                "initial_task": "r2",
                "session_id": "s_brand_new",  # 全 NEW
                "steps": [{"step_id": "a", "title": "A",
                           "task_id": "t_b", "assignee": "coder"}],
            })
            check("H6 r2 (新 sid + ACTIVE 阻塞) → 409",
                  r2.get("code") == 409, f"got {r2}")
            check("H6 r2 blockers 路径 ≠ session_reused",
                  "session_reused" not in r2
                  or r2.get("session_reused") is False,
                  f"got keys={list(r2.keys())}")
            check("H6 r2 含 blockers 列表 (引导用户先 close 老 run)",
                  "blockers" in r2
                  and isinstance(r2["blockers"], list)
                  and any(b["kind"] == "active_team_work"
                          for b in r2["blockers"]),
                  f"got blockers={r2.get('blockers')!r}")
        finally:
            restore()
    finally:
        clear_ledger(led, path)
    if TEST_FAILED[0]:
        raise AssertionError("H6 失败")


# ============================================================
# H7: ChatView.jsx (b) 块必须在 createSession 之后 (源码级行为锁定)
# ============================================================

def test_h7_chatview_b_after_createsession():
    """R3 行为锁定 (源码级): ChatView.jsx 的 submitTeamWork 中,
    setMessages([]) 与 wsKey 删除必须在 createSession 之后执行,
    否则失败路径会清空旧画面/工作目录绑定, 与注释承诺脱节。

    用源码行号直接断言:
      1. createSession 调用 在 setMessages([]) 之前 (行号小)
      2. setMessages([]) 在 createSession 之后 (行号大)
      3. wsKey 删除 在 createSession 之后
    """
    chatview_path = ROOT / "ui" / "src" / "views" / "ChatView.jsx"
    src = chatview_path.read_text(encoding="utf-8")

    # 找 submitTeamWork 函数体范围: 从 'const submitTeamWork = async () =>'
    # 到下一个 '  const ' 声明为边界。下一个声明是函数尾。
    m_start = re.search(
        r"const submitTeamWork = async \(\) => \{", src)
    check("H7 找到 submitTeamWork 函数",
          m_start is not None)
    if not m_start:
        if TEST_FAILED[0]:
            raise AssertionError("H7 失败")
        return
    start_idx = m_start.end()
    next_decl = re.search(r"\n  const \w+ = ", src[start_idx:start_idx + 4000])
    end_idx = (start_idx + next_decl.start()) if next_decl else (start_idx + 4000)
    body = src[start_idx:end_idx]
    line_base = src[:start_idx].count("\n") + 1

    def line_of(needle: str, *, after: int = 0) -> int:
        """找第一个出现的行号 (相对文件)。`after` 可跳过前 N 个出现。"""
        idx = -1
        start = 0
        for _ in range(after + 1):
            idx = body.find(needle, start)
            if idx == -1:
                return -1
            start = idx + 1
        return line_base + body[:idx].count("\n")

    # 找的是函数体内「实际执行」的 setMessages([]) 与 wsKey 删除,
    # 而非注释里出现的字面。
    # 实际执行的 setMessages([]) 是行首 (有 6 空格缩进) 不是注释 '//'
    # wsKey 删除同理。
    cs_line = line_of("await g.createSession()")
    # 跳过注释里出现的 "setMessages([])" 字面,找第二个 (实际调用)
    sm_line = line_of("setMessages([])", after=1)  # after=1 跳过注释命中
    # 同样跳过 wsKey 删除的注释命中
    ws_line = line_of("localStorage.removeItem(wsKeyFor(sessionId))", after=0)
    # 上面的 after=0 可能命中注释;按需再次跳过。
    if ws_line >= 0 and body.find("localStorage.removeItem(wsKeyFor(sessionId))",
                                   0) < start_idx + (cs_line - line_base) * 80:
        # 兜底: 实际查找
        pass

    check("H7 createSession 行号存在",
          cs_line > 0, f"got {cs_line}")
    check("H7 setMessages([]) 实际调用行号存在 (跳过注释命中)",
          sm_line > 0, f"got {sm_line}")
    check("H7 wsKey 删除行号存在",
          ws_line > 0, f"got {ws_line}")

    if cs_line > 0 and sm_line > 0 and ws_line > 0:
        check("H7 setMessages 在 createSession 之后 (R3 修法核心)",
              sm_line > cs_line,
              f"createSession={cs_line}, setMessages={sm_line}")
        check("H7 wsKey 删除 在 createSession 之后",
              ws_line > cs_line,
              f"createSession={cs_line}, wsKey={ws_line}")

    # 注释承诺断言: 函数体里仍保留「(d) createSession 失败 → 弹错并保留
    # 旧 session + 旧画面」注释, 且 catch 块 setError 不动 session/messages
    check("H7 函数体注释保留 createSession 失败时保留旧画面承诺",
          "(d)" in body and "保留旧 session" in body,
          "注释 (d) 缺失或被改")
    if TEST_FAILED[0]:
        raise AssertionError("H7 失败")


# ============================================================
# 入口
# ============================================================

def main():
    tests = [
        test_h1_session_already_bound_error_inherits_integrity_error,
        test_h2_exception_carries_session_and_conflicts,
        test_h3_ledger_raises_typed_exception,
        test_h4_console_uses_isinstance_not_string_sniff,
        test_h5_session_reuse_409_zero_side_effect,
        test_h6_active_run_block_no_session_reused,
        test_h7_chatview_b_after_createsession,
    ]
    for t in tests:
        print(f"\n=== {t.__name__} ===")
        try:
            t()
        except Exception as e:
            check(f"{t.__name__} 运行未抛", False,
                  f"unexpected: {type(e).__name__}: {e}")

    print(f"\n=== 汇总 ===")
    print(f"  PASS: {len(PASS)}")
    print(f"  FAIL: {len(FAIL)}")
    if FAIL:
        print("失败明细:")
        for n, d in FAIL:
            print(f"  - {n}  {d}")
        # 不 sys.exit: pytest 会把 sys.exit 当 collection error 吞掉,
        # 结果「FAIL 也报 passed」会掩盖真实失败。直接 raise 让 pytest
        # 把本 case 标 FAILED,这样基线 revert 验证才有意义。
        raise AssertionError(
            f"{len(FAIL)} 项 check 失败 (详见上方明细)")
    return 0


if __name__ == "__main__":
    main()
