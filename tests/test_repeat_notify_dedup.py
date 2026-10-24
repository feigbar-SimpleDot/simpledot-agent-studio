"""重复落账 → 重复转发修复测试 (2026-10-02 qq-login 实测加, 2026-10-04 升级三态模型)。

bug 触发链 (旧):
  1. main 推进链, harvest 写报告 (verdict=COMPLETE) → set_worker_report
     把 last_notified_at 清 NULL → reaper 转发 → mark_notified
  2. 同 task 再次结算(同 verdict, 或重复进入主循环,或 main 转推路径走重),
     set_worker_report 又把 last_notified 清 NULL → reaper 再转发
  3. main 收到 3+ 次同 task 同 verdict 转发, 浪费 turn、污染 chat history

修复 (2026-10-02, 初版):
  1. set_worker_report 只有报告内容(verdict + evidence)变化时才清 NULL。
  2. _send 失败时不再清 NULL, 改为记录 notify_attempts + notify_dead_until
     指数退避(30s, 60s, 120s, 240s, 300s, ...), 最多 8 次后退到死信
     (永久不再尝试),should_notify 看到 dead_until 不越界就当 False。

升级 (2026-10-04, 三态模型 — 修 429 雪崩):
  - relay 去重闸门改由 notify_confirmed_at 承担 (relay 扫描
    WHERE confirmed_at IS NULL, 等同于「待发送」)。
  - should_notify 默认 min_interval_s=0 — 时间闸门只在调用方显式传
    interval 时生效 (stuck_notify 等仍依赖它)。
  - 失败必须回滚 last_notified_at = NULL, 否则 relay 扫描把这一行当
    已送达屏蔽, 退避过后报告也回不来。三态字段语义:
    待发送: notify_confirmed_at IS NULL (无 last_notified_at 占用)
    等待确认: claim_for_relay 占位 + last_notified_at 设为 now
    已确认送达: notify_confirmed_at 置位
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

def main():
    ROOT = Path(__file__).resolve().parent.parent
    BASE = Path("/tmp/mw_smoke_repeat_notify")
    shutil.rmtree(BASE, ignore_errors=True)
    BASE.mkdir(parents=True)
    (BASE / "hermes").mkdir()

    env = dict(os.environ)
    env.update({
        "MW_HOME": str(BASE),
        "MW_HERMES_HOME": str(BASE / "hermes"),
        "MW_REGISTRY": str(ROOT / "tests" / "registry_local.json"),
        "PYTHONPATH": str(ROOT),
    })

    subprocess.run(["hermes", "kanban", "init"],
                   env={**env, "HERMES_HOME": env["MW_HERMES_HOME"]},
                   capture_output=True, check=False)

    PASS, FAIL = [], []
    def check(name, cond, detail=""):
        (PASS if cond else FAIL).append(name)
        print(f"  {'✓' if cond else '✗'} {name} {detail}")


    def _conn():
        sys.path.insert(0, str(ROOT))
        from mw.ledger import Ledger
        led = Ledger(str(BASE / "ledger.db"))
        return led


    def _bootstrap_task(task_id: str = "t_repeat_test"):
        """构造一张 HARVESTED 任务,带 worker_report,模拟 harvest 写完报告。"""
        sys.path.insert(0, str(ROOT))
        from mw.ledger import Ledger
        led = Ledger(str(BASE / "ledger.db"))
        led.put_contract(task_id, {"required_files": ["x.md"],
                                   "check_script": "exit 0",
                                   "timeout_seconds": 60})
        led.conn.execute(
            "UPDATE harvest_contracts SET harvest_status='HARVESTED', "
            "worker_report=? WHERE task_id=?",
            (json.dumps({"verdict": "COMPLETE",
                         "product_workspace": "/share_workspace/projects/test",
                         "evidence": ["exit_code=0", "physical OK"],
                         "_iter": 1}, ensure_ascii=False),
             task_id))
        led.conn.commit()
        led.close()


    # ── T1: 同 verdict 重复落账 → 不清 NULL → reaper 不重发 ─────
    print("== T1 同 verdict 重复 set_worker_report 不清 NULL ==")
    _bootstrap_task("t_repeat_test")
    led = _conn()
    # 标记"已通知"
    led.mark_notified("t_repeat_test")
    # 三态模型 (2026-10-04): should_notify 默认 min_interval_s=0 (无时间闸门),
    # relay 去重改由 notify_confirmed_at 承担 (relay 扫描 WHERE confirmed_at IS NULL)。
    # 本断言用显式 interval 验证「时间闸门能力仍在」(stuck_notify 等调用方依赖它)。
    check("初次标记后 should_notify=False (显式 120s 间隔)",
          led.should_notify("t_repeat_test", min_interval_s=120) is False)
    check("三态: 未确认送达时 relay 扫描会捞起 (confirmed_at IS NULL)",
          led.conn.execute(
              "SELECT notify_confirmed_at FROM harvest_contracts "
              "WHERE task_id='t_repeat_test'").fetchone()["notify_confirmed_at"] is None)

    # 同 verdict 重复 set_worker_report
    report_same = {"verdict": "COMPLETE",
                   "product_workspace": "/share_workspace/projects/test",
                   "evidence": ["exit_code=0", "physical OK"],
                   "_iter": 1}
    led.set_worker_report("t_repeat_test", report_same)

    cur = led.conn.execute(
        "SELECT last_notified_at, worker_report FROM harvest_contracts "
        "WHERE task_id='t_repeat_test'").fetchone()
    check("last_notified_at 没被清空 (同内容)",
          cur["last_notified_at"] is not None,
          f"got {cur['last_notified_at']}")

    # 真正变化后再写 → 标记清楚 NULL (新报告被转发)
    report_diff = {"verdict": "COMPLETE",
                   "product_workspace": "/share_workspace/projects/test",
                   "evidence": ["exit_code=0", "physical OK"],
                   "_iter": 2}  # 唯一的不同
    led.set_worker_report("t_repeat_test", report_diff)
    cur = led.conn.execute(
        "SELECT last_notified_at FROM harvest_contracts "
        "WHERE task_id='t_repeat_test'").fetchone()
    check("内容真变化后 last_notified_at 被清 NULL",
          cur["last_notified_at"] is None,
          f"got {cur['last_notified_at']}")
    led.close()


    # ── T2: 失败重试用指数退避, 不再立即清 NULL ──────────────
    print("== T2 _send 失败用 notify_attempts + dead_until 退避 ==")
    sys.path.insert(0, str(ROOT))
    from mw.reaper import _send_to_main
    from mw.ledger import Ledger

    # 用一个不存在的 gateway 模拟"发送失败"
    # 准备 task, 不预先 mark_notified (因为函数会自动 mark)
    led = _conn()
    led.conn.execute("DELETE FROM harvest_contracts WHERE task_id='t_fail_test'")
    led.put_contract("t_fail_test", {"required_files": ["x.md"],
                                    "check_script": "exit 0",
                                    "timeout_seconds": 60})
    led.conn.execute(
        "UPDATE harvest_contracts SET harvest_status='HARVESTED', "
        "worker_report=? WHERE task_id=?",
        (json.dumps({"verdict": "COMPLETE"}, ensure_ascii=False),
         "t_fail_test"))
    led.conn.commit()
    led.close()

    # 调一次 _send_to_main (用不存在的 gateway → urlopen 失败)
    _send_to_main(sid="api_does_not_exist", msg="test",
                  gw="http://127.0.0.1:1",  # 不会有人接的端口
                  key="deadbeef", db_path=str(BASE / "ledger.db"),
                  notify_task="t_fail_test")
    # 等后台线程跑完
    time.sleep(2)

    led = _conn()
    cur = led.conn.execute(
        "SELECT last_notified_at, notify_attempts, notify_dead_until "
        "FROM harvest_contracts WHERE task_id='t_fail_test'").fetchone()
    check("失败后 notify_attempts >= 1",
          cur["notify_attempts"] >= 1, f"got {cur['notify_attempts']}")
    check("失败后 notify_dead_until 设为未来",
          cur["notify_dead_until"] is not None
          and cur["notify_dead_until"] > time.time(),
          f"got {cur['notify_dead_until']}")
    # 三态模型 (2026-10-04): 发送失败必须把 last_notified_at 回滚成 NULL,
    # 否则 relay 扫描 (WHERE notify_confirmed_at IS NULL) 之外, claim 置的位
    # 会让这行永远扫不回来, 报告永久丢失。退避靠 notify_dead_until 挡重投,
    # 不靠 last_notified_at。
    check("失败后 last_notified_at 回滚 NULL (让退避后可重投)",
          cur["last_notified_at"] is None,
          f"got {cur['last_notified_at']}")
    check("失败后仍未确认送达 (confirmed_at IS NULL)",
          led.conn.execute(
              "SELECT notify_confirmed_at FROM harvest_contracts "
              "WHERE task_id='t_fail_test'").fetchone()["notify_confirmed_at"] is None)

    # 退避期内 should_notify 直接 False
    check("退避期内 should_notify=False",
          led.should_notify("t_fail_test") is False)

    # 清掉 dead_until 后 should_notify 重新可用
    # 注意: last_notified_at 还在 120s 间隔内, 所以同时拨到老
    led.conn.execute(
        "UPDATE harvest_contracts SET notify_dead_until=NULL, "
        "last_notified_at=? WHERE task_id='t_fail_test'",
        (time.time() - 200,))
    led.conn.commit()
    check("dead_until 清空 + last_notified 拨远后 should_notify=True",
          led.should_notify("t_fail_test") is True)
    led.close()


    # ── T3: 模拟 8 次连续失败 → 进死信, 永远不再试 ─────────
    print("== T3 8 次失败进死信 ==")
    led = _conn()
    led.conn.execute(
        "UPDATE harvest_contracts SET notify_attempts=7, last_notified_at=NULL, "
        "worker_report=? WHERE task_id='t_fail_test'",
        (json.dumps({"verdict": "COMPLETE", "_iter": 99},
                    ensure_ascii=False),))
    # 故意 worker_report 不同 → 清 NULL, 触发下轮 reap 转发
    led.conn.commit()

    _send_to_main(sid="api_does_not_exist", msg="test",
                  gw="http://127.0.0.1:1",
                  key="deadbeef", db_path=str(BASE / "ledger.db"),
                  notify_task="t_fail_test")
    time.sleep(2)
    cur = led.conn.execute(
        "SELECT notify_attempts, notify_dead_until FROM harvest_contracts "
        "WHERE task_id='t_fail_test'").fetchone()
    check("8 次失败 attempts=8",
          cur["notify_attempts"] == 8, f"got {cur['notify_attempts']}")
    check("死信 dead_until 远未来",
          cur["notify_dead_until"] is not None
          and cur["notify_dead_until"] > time.time() + 86400,
          f"got {cur['notify_dead_until']}")
    check("死信状态 should_notify=False",
          led.should_notify("t_fail_test") is False)
    led.close()


    # ── T4: 实际重发主路径被 set_worker_report 内容比较守住 ─────
    print("== T4 同 verdict 重复走 produce() 不让 reaper 重发 ==")
    # 模拟你 main 里看到的 3 次同 t.eb257477 转发场景:
    #   每次 set_worker_report 都用相同 verdict + evidence
    led = _conn()
    led.conn.execute("DELETE FROM harvest_contracts WHERE task_id='t_idempotent'")
    led.put_contract("t_idempotent", {"required_files": ["x.md"],
                                      "check_script": "exit 0",
                                      "timeout_seconds": 60})

    # 第一次落账
    report = {"verdict": "COMPLETE",
              "product_workspace": "/share_workspace/projects/test",
              "evidence": ["exit_code=0", "physical OK"]}
    led.set_worker_report("t_idempotent", report)
    # 标记已通知
    led.mark_notified("t_idempotent")

    # 模拟 main 推进链又触发了同 task 的重复落账 (走第二次 harvest)
    report_again = {"verdict": "COMPLETE",
                    "product_workspace": "/share_workspace/projects/test",
                    "evidence": ["exit_code=0", "physical OK"]}  # 完全一样
    led.set_worker_report("t_idempotent", report_again)

    cur = led.conn.execute(
        "SELECT last_notified_at FROM harvest_contracts "
        "WHERE task_id='t_idempotent'").fetchone()
    check("重复落账后 last_notified_at 保留 (不会触发再次发)",
          cur["last_notified_at"] is not None,
          f"got {cur['last_notified_at']}")

    # 第三次内容变 (加新证据,代表报告真实刷新)
    report_new = {"verdict": "COMPLETE",
                  "product_workspace": "/share_workspace/projects/test",
                  "evidence": ["exit_code=0", "physical OK",
                               "additional evidence appended"]}
    led.set_worker_report("t_idempotent", report_new)
    cur = led.conn.execute(
        "SELECT last_notified_at FROM harvest_contracts "
        "WHERE task_id='t_idempotent'").fetchone()
    check("内容真变化后 last_notified_at 被清 (应该重发)",
          cur["last_notified_at"] is None,
          f"got {cur['last_notified_at']}")
    led.close()


    print(f"\n结果: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
