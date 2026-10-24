"""TODO.md 契约模块测试 (1A 边界, 2026-10-03 减法重构)。

契约校验和 / ContractTampered 防篡改机制已删 —— worker prompt 硬约束
("只改状态/结果两行") 取代密码学校验。本测试覆盖: 解析/渲染 roundtrip、
rotate 推进、kanban_task 绑定、worker prompt 契约。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mw.todo import (
    TodoTask, TodoDoc, parse, write, init_todo, rotate,
    RESULT_PLACEHOLDER,
)


def test_init_and_parse_roundtrip(tmp_path):
    p = tmp_path / "TODO.md"
    first = TodoTask(step_id="req-analysis", title="需求分析",
                    goal="分析乌龟骑车游戏需求", assignee="product_manager")
    init_todo(p, "乌龟骑自行车小游戏", first)
    doc = parse(p)
    assert doc is not None
    assert doc.title == "乌龟骑自行车小游戏"
    assert doc.current.step_id == "req-analysis"
    assert doc.current.assignee == "product_manager"
    assert doc.current.result == RESULT_PLACEHOLDER
    assert not doc.current.has_result


def test_worker_writes_result(tmp_path):
    p = tmp_path / "TODO.md"
    init_todo(p, "T", TodoTask("a", "A", "目标A", "coder"))
    doc = parse(p)
    doc.current.result = "已完成: 写了 index.html, 3 个函数"
    write(p, doc)
    doc2 = parse(p)
    assert doc2.current.has_result
    assert "index.html" in doc2.current.result


def test_rotate_archives_done(tmp_path):
    p = tmp_path / "TODO.md"
    init_todo(p, "T", TodoTask("a", "A", "目标A", "coder"))
    doc = parse(p)
    doc.current.result = "A 完成结果"
    write(p, doc)
    nxt = TodoTask("b", "B", "目标B", "system_engineer")
    rotate(p, nxt)
    doc2 = parse(p)
    assert doc2.current.step_id == "b"
    assert doc2.current.assignee == "system_engineer"
    assert len(doc2.done) == 1
    assert "[a]" in doc2.done[0]
    assert "A 完成结果" in doc2.done[0]


def test_rotate_end_of_chain(tmp_path):
    p = tmp_path / "TODO.md"
    init_todo(p, "T", TodoTask("a", "A", "目标A", "coder"))
    doc = parse(p)
    doc.current.result = "done"
    write(p, doc)
    rotate(p, None)
    doc2 = parse(p)
    assert doc2.current is None
    assert len(doc2.done) == 1


def test_rotate_multiple_steps(tmp_path):
    p = tmp_path / "TODO.md"
    init_todo(p, "T", TodoTask("a", "A", "目标A", "coder"))
    for step, who in [("b", "designer"), ("c", "tester")]:
        doc = parse(p)
        doc.current.result = f"{doc.current.step_id} 结果"
        write(p, doc)
        rotate(p, TodoTask(step, step.upper(), f"目标{step}", who))
    doc = parse(p)
    assert doc.current.step_id == "c"
    assert len(doc.done) == 2
    assert "[a]" in doc.done[1] and "[b]" in doc.done[0]


def test_no_todo_file_returns_none(tmp_path):
    assert parse(tmp_path / "nonexistent.md") is None


def test_current_task_isolates_from_done():
    doc = TodoDoc(title="T",
                 current=TodoTask("b", "B", "目标B", "coder"),
                 done=["- [a] A → A结果"])
    text = doc.render()
    cur_idx = text.index("## 当前任务")
    sep_idx = text.index("---")
    done_idx = text.index("## 已完成")
    assert cur_idx < sep_idx < done_idx
    current_section = text[cur_idx:sep_idx]
    assert "A结果" not in current_section


def test_kv_value_with_colon():
    """目标里含冒号不能被切错。"""
    p = Path("/tmp/test_todo_colon.md")
    init_todo(p, "T", TodoTask("a", "A", "格式: http://x.com 要支持", "coder"))
    doc = parse(p)
    assert doc.current.goal == "格式: http://x.com 要支持"
    p.unlink()


def test_result_placeholder_detection():
    assert not TodoTask("a", "A", "g", "c", result=RESULT_PLACEHOLDER).has_result
    assert not TodoTask("a", "A", "g", "c", result="").has_result
    assert not TodoTask("a", "A", "g", "c", result="   ").has_result
    assert TodoTask("a", "A", "g", "c", result="真结果").has_result


def test_init_from_steps_seeds_chain(tmp_path):
    from mw.todo import init_from_steps, parse
    p = tmp_path / "TODO.md"
    tasks = [
        TodoTask("req", "需求分析", "分析需求", "pm"),
        TodoTask("design", "技术设计", "设计架构", "se"),
        TodoTask("code", "编码", "写代码", "coder"),
    ]
    init_from_steps(p, "乌龟骑车", tasks)
    doc = parse(p)
    assert doc.current.step_id == "req"
    assert len(doc.pending) == 2
    assert doc.pending[0].step_id == "design"
    assert doc.pending[1].assignee == "coder"


def test_rotate_next_walks_chain(tmp_path):
    from mw.todo import init_from_steps, parse, rotate_next
    p = tmp_path / "TODO.md"
    tasks = [
        TodoTask("req", "需求分析", "分析需求", "pm"),
        TodoTask("design", "技术设计", "设计架构", "se"),
    ]
    init_from_steps(p, "T", tasks)
    doc = parse(p)
    doc.current.result = "需求已明确"
    write(p, doc)
    rotate_next(p)
    doc2 = parse(p)
    assert doc2.current.step_id == "design"
    assert len(doc2.pending) == 0
    assert "[req]" in doc2.done[0]
    assert "需求已明确" in doc2.done[0]
    doc2.current.result = "设计完成"
    write(p, doc2)
    rotate_next(p)
    doc3 = parse(p)
    assert doc3.current is None
    assert len(doc3.done) == 2


def test_pending_roundtrip_preserves_assignee(tmp_path):
    from mw.todo import init_from_steps, parse
    p = tmp_path / "TODO.md"
    init_from_steps(p, "T", [
        TodoTask("a", "A", "ga", "w1"),
        TodoTask("b", "B含空格", "gb", "w2"),
    ])
    doc = parse(p)
    assert doc.pending[0].title == "B含空格"
    assert doc.pending[0].assignee == "w2"


def test_pending_carries_fields(tmp_path):
    """rotate 后 goal/产物目录/产出文件 都要保留。"""
    from mw.todo import init_from_steps, parse, write, rotate_next
    p = tmp_path / "TODO.md"
    init_from_steps(p, "T", [
        TodoTask("a", "A", "目标A", "coder", required_files=["a.md", "b.md"]),
        TodoTask("b", "B", "目标B", "se",
                product_workspace="/share_workspace/projects/x",
                required_files=["c.md"]),
    ])
    doc = parse(p)
    doc.current.result = "A done"
    write(p, doc)
    rotate_next(p)
    doc2 = parse(p)
    assert doc2.current.goal == "目标B"
    assert doc2.current.required_files == ["c.md"]
    assert doc2.current.product_workspace == "/share_workspace/projects/x"


# ── kanban_task_id 绑定 + 确定性完成信号 ──────────────────────────
def test_bind_kanban_tasks_roundtrip(tmp_path):
    from mw.todo import TodoTask, init_from_steps, parse, bind_kanban_tasks
    p = tmp_path / "TODO.md"
    tasks = [TodoTask("a", "需求分析", "写需求", "coder", product_workspace="/tmp/x"),
             TodoTask("b", "技术设计", "做设计", "system_engineer", product_workspace="/tmp/x")]
    init_from_steps(p, "链测试", tasks)
    doc = bind_kanban_tasks(p, {"a": "t_aa123", "b": "t_bb456"})
    assert doc is not None and doc.current is not None
    assert doc.current.step_id == "a"
    assert doc.current.kanban_task_id == "t_aa123"
    d2 = parse(p)
    assert d2.current.kanban_task_id == "t_aa123"
    assert d2.pending[0].kanban_task_id == "t_bb456"


def test_bind_kanban_tasks_roundtrip_after_rotate(tmp_path):
    """绑定后 rotate, kanban_task_id 必须跟着升 current。"""
    from mw.todo import (TodoTask, init_from_steps, parse,
                        bind_kanban_tasks, rotate_next)
    p = tmp_path / "TODO.md"
    tasks = [TodoTask("a", "A", "g", "coder", product_workspace="/x"),
             TodoTask("b", "B", "g", "system_engineer", product_workspace="/x")]
    init_from_steps(p, "t", tasks)
    bind_kanban_tasks(p, {"a": "t_a", "b": "t_b"})
    doc = parse(p); doc.current.result = "A 完成摘要"
    write(p, doc)
    rotate_next(p)
    d2 = parse(p)
    assert d2.current.step_id == "b"
    assert d2.current.kanban_task_id == "t_b", "rotate 后 kanban_task_id 必须跟着"


# ── rotate_to 幂等推进 ────────────────────────────────────────────
def test_rotate_to_advances_matching_task(tmp_path):
    from mw.todo import (TodoTask, init_from_steps, parse, write,
                        bind_kanban_tasks, rotate_to)
    p = tmp_path / "TODO.md"
    init_from_steps(p, "T", [
        TodoTask("a", "A", "ga", "coder", product_workspace=str(tmp_path)),
        TodoTask("b", "B", "gb", "se", product_workspace=str(tmp_path)),
    ])
    bind_kanban_tasks(p, {"a": "t_aaa", "b": "t_bbb"})
    doc = parse(p); doc.current.result = "A 完成"; write(p, doc)
    d = rotate_to(p, "t_aaa")
    assert d is not None and d.current.step_id == "b"
    assert d.current.kanban_task_id == "t_bbb"
    assert "[a]" in d.done[0] and "A 完成" in d.done[0]


def test_rotate_to_idempotent_wrong_task(tmp_path):
    from mw.todo import (TodoTask, init_from_steps, parse, write,
                        bind_kanban_tasks, rotate_to)
    p = tmp_path / "TODO.md"
    init_from_steps(p, "T", [
        TodoTask("a", "A", "ga", "coder", product_workspace=str(tmp_path)),
        TodoTask("b", "B", "gb", "se", product_workspace=str(tmp_path)),
    ])
    bind_kanban_tasks(p, {"a": "t_aaa", "b": "t_bbb"})
    doc = parse(p); doc.current.result = "A 完成"; write(p, doc)
    before = p.read_text()
    d = rotate_to(p, "t_bbb")
    assert d is None
    assert p.read_text() == before, "不匹配时文件必须原样不动"


def test_rotate_to_idempotent_already_advanced(tmp_path):
    from mw.todo import (TodoTask, init_from_steps, parse, write,
                        bind_kanban_tasks, rotate_to)
    p = tmp_path / "TODO.md"
    init_from_steps(p, "T", [
        TodoTask("a", "A", "ga", "coder", product_workspace=str(tmp_path)),
        TodoTask("b", "B", "gb", "se", product_workspace=str(tmp_path)),
    ])
    bind_kanban_tasks(p, {"a": "t_aaa", "b": "t_bbb"})
    doc = parse(p); doc.current.result = "A 完成"; write(p, doc)
    d1 = rotate_to(p, "t_aaa")
    assert d1 is not None and d1.current.step_id == "b"
    d2 = rotate_to(p, "t_aaa")
    assert d2 is None
    d3 = parse(p)
    assert len(d3.done) == 1, "done 不应被重复追加"


def test_rotate_to_end_of_chain(tmp_path):
    from mw.todo import (TodoTask, init_from_steps, parse, write,
                        bind_kanban_tasks, rotate_to)
    p = tmp_path / "TODO.md"
    init_from_steps(p, "T", [TodoTask("a", "A", "ga", "coder",
                                     product_workspace=str(tmp_path))])
    bind_kanban_tasks(p, {"a": "t_aaa"})
    doc = parse(p); doc.current.result = "A 完成"; write(p, doc)
    d = rotate_to(p, "t_aaa")
    assert d is not None and d.current is None
    assert len(d.done) == 1


def test_rotate_to_no_file_returns_none(tmp_path):
    from mw.todo import rotate_to
    assert rotate_to(tmp_path / "nope.md", "t_x") is None


# ── worker prompt 契约 (统一格式) ─────────────────────────────────
def test_worker_contract_prompt_points_to_todo_file():
    from mw.pipeline import worker_contract_prompt
    prompt = worker_contract_prompt(
        "/share_workspace/projects/turtle-bike/TODO.md",
        "/share_workspace/projects/turtle-bike/.tasks/t_x")
    assert "TODO.md" in prompt
    assert "/share_workspace/projects/turtle-bike/TODO.md" in prompt
    assert "当前任务" in prompt
    # 关键: prompt 里不含 'continue task' 占位符
    assert "continue task" not in prompt
    # 统一格式: sed 必须保留 `- ` 前缀
    assert "sed -i 's/^- 状态: .*/- 状态: working/'" in prompt
    assert "sed -i 's/^- 状态: .*/- 状态: completed/'" in prompt
    # 只做这一步 + 只改状态/结果 的硬约束
    assert "只做这一步" in prompt
    assert "只能" in prompt and "状态" in prompt and "结果" in prompt
    # .clarify.json 协议
    assert ".clarify.json" in prompt


def test_build_worker_prompt_project_task(tmp_path):
    """项目任务: prompt 指向 <project>/TODO.md, 产物目录=project。"""
    from mw.pipeline import build_worker_prompt
    m = {
        "title": "T", "target": {"profile": "coder"},
        "input_spec": {"task_description": "d"},
        "harvest": {"required_files": [], "timeout_seconds": 1800},
        "idempotency_key": "k",
        "project_workspace": "/share_workspace/projects/proj",
    }
    from mw.schemas import TaskManifest
    mm = TaskManifest.model_validate(m)
    prompt = build_worker_prompt(mm, workspace="/share_workspace/projects/proj/.tasks/t_1")
    assert "/share_workspace/projects/proj/TODO.md" in prompt
    assert "continue task" not in prompt


def test_build_worker_prompt_infers_project_from_tasks_path():
    """漏传 project_workspace 时, 从 /.tasks/ 路径自动推算项目根。"""
    from mw.pipeline import build_worker_prompt
    from mw.schemas import TaskManifest
    mm = TaskManifest.model_validate({
        "title": "SE 任务", "target": {"profile": "system_engineer"},
        "input_spec": {"task_description": "做架构"},
        "harvest": {"required_files": [], "timeout_seconds": 1800},
        "idempotency_key": "bug1-test",
    })
    p = build_worker_prompt(mm,
        workspace="/share_workspace/projects/turtle-bike/.tasks/t_xxx",
        product_workspace=None)
    assert "turtle-bike/TODO.md" in p


# ── stuck 通知 (reaper 敲门) ──────────────────────────────────────
def test_stuck_notify_emits_event(tmp_path):
    import os
    from unittest.mock import patch
    from mw import reaper as _rp
    from mw.ledger import Ledger
    import time
    db = str(tmp_path / "ledger.db")
    led = Ledger(db)
    os.environ["MW_HOME"] = str(tmp_path)
    long_ago = time.time() - 31 * 60
    run_id = "r_test_stuck"
    led.create_team_work_run(run_id, "api_main_session", "需求")
    led.put_contract("t_stuck", {"required_files": []})
    led.register_team_work_card(run_id, "t_stuck")
    # 方案2 (2026-10-04): 敲门只针对 current_card_id, 非当前卡是排队不是卡死
    led.set_team_work_current_card(run_id, "t_stuck")
    led.touch_progress("t_stuck")
    led.conn.execute(
        "UPDATE harvest_contracts SET last_progress_at=? WHERE task_id=?",
        (long_ago, "t_stuck"))
    led.conn.commit()
    _rp.write_marker(str(tmp_path), "t_stuck", started_at=long_ago)
    sent = []
    with patch.dict(os.environ, {"API_SERVER_KEY": "testkey1234567890"}), \
         patch.object(_rp, "_send_to_main",
                     lambda sid, msg, gw, key, db, registry=None, notify_task=None:
                     sent.append((sid, msg, notify_task))):
        base = str(tmp_path)
        results = []
        _rp._notify_main_of_stuck(led, base, results, stuck_after_min=30,
                                 active_run_id=run_id, current_card_id="t_stuck")
    assert len(sent) == 1 and sent[0][0] == "api_main_session", \
        f"应发 chat 到 initiator: {sent}"
    assert sent[0][2] == "t_stuck", f"notify_task 应带上: {sent}"
    assert any(r.get("trigger") == "stuck_notify" for r in results), \
        f"results: {results}"


def test_stuck_notify_skips_recent_tasks(tmp_path):
    import os
    from unittest.mock import patch
    from mw import reaper as _rp
    from mw.ledger import Ledger
    import time
    db = str(tmp_path / "ledger.db")
    led = Ledger(db)
    os.environ["MW_HOME"] = str(tmp_path)
    recently = time.time() - 5 * 60
    run_id = "r_test_fresh"
    led.create_team_work_run(run_id, "api_main_session", "需求")
    led.put_contract("t_fresh", {"required_files": []})
    led.register_team_work_card(run_id, "t_fresh")
    # 方案2: 设 current_card 让测试真正走「挂太短」分支, 而非「无当前卡」
    led.set_team_work_current_card(run_id, "t_fresh")
    led.touch_progress("t_fresh")
    led.conn.execute(
        "UPDATE harvest_contracts SET last_progress_at=? WHERE task_id=?",
        (recently, "t_fresh"))
    led.conn.commit()
    _rp.write_marker(str(tmp_path), "t_fresh", started_at=recently)
    sent = []
    with patch.dict(os.environ, {"API_SERVER_KEY": "testkey1234567890"}), \
         patch.object(_rp, "_send_to_main",
                     lambda *a, **k: sent.append(a)):
        base = str(tmp_path)
        results = []
        _rp._notify_main_of_stuck(led, base, results, stuck_after_min=30,
                                 active_run_id=run_id, current_card_id="t_fresh")
    assert sent == [], f"挂太短不应发: {sent}"
    assert results == [], f"results 应为空: {results}"


def test_stuck_notify_skips_non_current_card(tmp_path):
    """方案2 核心断言: 非当前活动卡即使挂很久也不敲门。

    非当前卡在 ledger 语义里是「排队等 main advance」, 不是「卡死」。
    对它敲门会误导 main 去救一张本就不该动的卡。
    """
    import os
    from unittest.mock import patch
    from mw import reaper as _rp
    from mw.ledger import Ledger
    import time
    db = str(tmp_path / "ledger.db")
    led = Ledger(db)
    os.environ["MW_HOME"] = str(tmp_path)
    long_ago = time.time() - 31 * 60
    run_id = "r_test_noncurrent"
    led.create_team_work_run(run_id, "api_main_session", "需求")
    # 两张卡都挂很久, 但 current_card 指向另一张 (t_active)
    led.put_contract("t_queued", {"required_files": []})
    led.register_team_work_card(run_id, "t_queued")
    led.put_contract("t_active", {"required_files": []})
    led.register_team_work_card(run_id, "t_active")
    led.set_team_work_current_card(run_id, "t_active")
    for tid in ("t_queued", "t_active"):
        led.touch_progress(tid)
        led.conn.execute(
            "UPDATE harvest_contracts SET last_progress_at=? WHERE task_id=?",
            (long_ago, tid))
    led.conn.commit()
    sent = []
    with patch.dict(os.environ, {"API_SERVER_KEY": "testkey1234567890"}), \
         patch.object(_rp, "_send_to_main",
                     lambda *a, **k: sent.append(k.get("notify_task"))):
        results = []
        _rp._notify_main_of_stuck(led, str(tmp_path), results,
                                 stuck_after_min=30, active_run_id=run_id,
                                 current_card_id="t_active")
    # 只有 t_active 被敲, t_queued 绝不
    assert "t_queued" not in sent, f"排队卡不该敲门: {sent}"
    assert sent == ["t_active"], f"只该敲当前卡: {sent}"


def test_stuck_notify_writes_last_notified_at(tmp_path):
    """bug fix (2026-10-05): stuck 路径必须写 last_notified_at。

    修前: _notify_main_of_stuck 走 _send_to_main 直接发送, confirm_delivered
    只改 notify_confirmed_at, 从未更新 last_notified_at。should_notify 看
    last_notified_at 永远 NULL/陈旧 → 返回 True → 每 15s 都敲门
    (70100fba85ad 实测: 同卡连敲 6 次, stuck_minutes 14.1→15.3)。

    修法: 起 _send_to_main 线程后调 mark_notified 写 last_notified_at=now。
    验证: 第二次 _notify_main_of_stuck 在同一窗口内应被 should_notify 挡住。
    """
    import os, time
    from unittest.mock import patch
    from mw import reaper as _rp
    from mw.ledger import Ledger
    db = str(tmp_path / "ledger.db")
    led = Ledger(db)
    os.environ["MW_HOME"] = str(tmp_path)
    long_ago = time.time() - 31 * 60
    run_id = "r_test_dedup"
    led.create_team_work_run(run_id, "api_main_session", "需求")
    led.put_contract("t_dedup", {"required_files": []})
    led.register_team_work_card(run_id, "t_dedup")
    led.set_team_work_current_card(run_id, "t_dedup")
    led.touch_progress("t_dedup")
    led.conn.execute(
        "UPDATE harvest_contracts SET last_progress_at=? WHERE task_id=?",
        (long_ago, "t_dedup"))
    led.conn.commit()
    _rp.write_marker(str(tmp_path), "t_dedup", started_at=long_ago)
    sent = []
    with patch.dict(os.environ, {"API_SERVER_KEY": "testkey1234567890"}), \
         patch.object(_rp, "_send_to_main",
                     lambda *a, **k: sent.append(k.get("notify_task"))):
        # 第 1 次: 应发
        _rp._notify_main_of_stuck(led, str(tmp_path), [],
                                  stuck_after_min=30,
                                  active_run_id=run_id,
                                  current_card_id="t_dedup")
        assert sent == ["t_dedup"], f"第 1 次应发: {sent}"
        # 验证 last_notified_at 已被写入
        cur = led.conn.execute(
            "SELECT last_notified_at FROM harvest_contracts "
            "WHERE task_id='t_dedup'").fetchone()
        assert cur["last_notified_at"] is not None, \
            f"修后必须写 last_notified_at, 实际仍 NULL: {cur}"
        # 第 2 次: 同一窗口内 should_notify 应当返回 False → 不发
        _rp._notify_main_of_stuck(led, str(tmp_path), [],
                                  stuck_after_min=30,
                                  active_run_id=run_id,
                                  current_card_id="t_dedup")
        assert sent == ["t_dedup"], \
            f"第 2 次应被去重挡住, 实际又发了: {sent}"


# ── runner (必须在文件末尾, 确保所有 test_ 已进 globals) ──────────
if __name__ == "__main__":
    import tempfile
    fns = [(k, v) for k, v in list(globals().items()) if k.startswith("test_")]
    fns.sort(key=lambda kv: kv[1].__code__.co_firstlineno)
    passed = 0
    for name, fn in fns:
        try:
            if "tmp_path" in fn.__code__.co_varnames:
                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d))
            else:
                fn()
            print(f"  ✓ {name}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {name}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
