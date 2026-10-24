"""Relay 三态模型测试 (2026-10-04, 用户模型「发送成功确认收到=已通知,
发送中=等待确认, 绝不反复发」)。

三态 (notify_confirmed_at + last_notified_at 组合):
  待发送     = confirmed_at IS NULL AND (last_notified_at IS NULL
               OR last_notified_at < now - inflight_s)
  等待确认   = confirmed_at IS NULL AND last_notified_at 在途未超时
  已确认送达 = confirmed_at IS NOT NULL

锁住的行为:
  1. claim_for_relay 首次抢到 (待发送→等待确认)
  2. 在途未超时时二次 claim 抢不到 (杜绝重复起线程 → 429 雪崩根因)
  3. confirm_delivered 后永不再 claim (已确认送达)
  4. set_worker_report 内容变化 → 清 confirmed_at → 重新可 claim (回流)
  5. set_worker_report 内容不变 → 保留 confirmed_at → 仍不可 claim
  6. 在途超时 (进程崩溃兜底) → 重新可 claim
"""
import sqlite3
import tempfile
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mw.ledger import Ledger

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
results = []


def check(name, fn):
    try:
        fn()
        print(f"  {PASS} {name}")
        results.append(True)
    except AssertionError as e:
        print(f"  {FAIL} {name}: {e}")
        results.append(False)
    except Exception as e:
        print(f"  {FAIL} {name}: {type(e).__name__}: {e}")
        results.append(False)


def fresh_ledger(task_id="t_relay1"):
    d = tempfile.mkdtemp()
    led = Ledger(os.path.join(d, "ledger.db"))
    led.put_contract(task_id, {"required_files": ["x.py"]})
    led.set_worker_report(task_id, {"verdict": "COMPLETE", "evidence": ["a"]})
    return led, task_id


def test_claim_first_time():
    led, tid = fresh_ledger()
    assert led.claim_for_relay(tid) is True, "首次 claim 应成功 (待发送→等待确认)"
    led.close()


def test_claim_inflight_blocked():
    led, tid = fresh_ledger()
    assert led.claim_for_relay(tid) is True
    # 在途未超时, 二次 claim 必须抢不到
    assert led.claim_for_relay(tid) is False, "在途未超时时二次 claim 应失败"
    led.close()


def test_confirm_blocks_forever():
    led, tid = fresh_ledger()
    assert led.claim_for_relay(tid) is True
    led.confirm_delivered(tid)
    # 已确认送达, 即使 inflight_s=0 也不该再 claim
    assert led.claim_for_relay(tid, inflight_s=0) is False, \
        "已确认送达后永不再 claim"
    led.close()


def test_content_change_reopens():
    led, tid = fresh_ledger()
    assert led.claim_for_relay(tid) is True
    led.confirm_delivered(tid)
    # 报告内容变化 → 清 confirmed_at → 重新可 claim (回流/重结算)
    led.set_worker_report(tid, {"verdict": "NEED_CLARIFICATION",
                               "question": "改方向?"})
    assert led.claim_for_relay(tid) is True, \
        "报告内容变化后应重新可 claim"
    led.close()


def test_same_content_stays_closed():
    led, tid = fresh_ledger()
    assert led.claim_for_relay(tid) is True
    led.confirm_delivered(tid)
    # 同内容重复落账 → 保留 confirmed_at → 仍不可 claim
    led.set_worker_report(tid, {"verdict": "COMPLETE", "evidence": ["a"]})
    assert led.claim_for_relay(tid) is False, \
        "同内容重复落账不应重开 relay"
    led.close()


def test_inflight_timeout_reopens():
    led, tid = fresh_ledger()
    assert led.claim_for_relay(tid) is True
    # 模拟进程崩溃: 把 last_notified_at 拨到很久以前
    led.conn.execute(
        "UPDATE harvest_contracts SET last_notified_at=? WHERE task_id=?",
        (time.time() - 9999, tid))
    led.conn.commit()
    # inflight_s=660, 9999s 前 → 超时 → 重新可 claim
    assert led.claim_for_relay(tid, inflight_s=660) is True, \
        "在途超时后应重新可 claim (进程崩溃兜底)"
    led.close()


def test_scan_filter_uses_confirmed_at():
    """relay 扫描 WHERE notify_confirmed_at IS NULL 的语义正确性。"""
    led, tid = fresh_ledger()
    # 先结算到 HARVESTED (relay 只扫 HARVESTED/NEED_CLARIFICATION/BLOCKED)
    led.set_harvest(tid, "HARVESTED", 0, "ok")
    # 未确认 → 应被扫到
    rows = led.conn.execute(
        "SELECT task_id FROM harvest_contracts "
        "WHERE harvest_status IN ('HARVESTED','NEED_CLARIFICATION','BLOCKED') "
        "AND worker_report IS NOT NULL AND notify_confirmed_at IS NULL").fetchall()
    assert any(r["task_id"] == tid for r in rows), "未确认报告应被扫到"
    # claim + 确认 → 不再被扫到
    led.claim_for_relay(tid)
    led.confirm_delivered(tid)
    rows = led.conn.execute(
        "SELECT task_id FROM harvest_contracts "
        "WHERE harvest_status IN ('HARVESTED','NEED_CLARIFICATION','BLOCKED') "
        "AND worker_report IS NOT NULL AND notify_confirmed_at IS NULL").fetchall()
    assert not any(r["task_id"] == tid for r in rows), \
        "已确认送达报告不应再被扫到"
    led.close()


if __name__ == "__main__":
    print("Relay 三态模型测试:")
    check("首次 claim 成功 (待发送→等待确认)", test_claim_first_time)
    check("在途未超时二次 claim 被拒", test_claim_inflight_blocked)
    check("confirm 后永不再 claim", test_confirm_blocks_forever)
    check("内容变化重开 relay (回流)", test_content_change_reopens)
    check("同内容重复落账不重开", test_same_content_stays_closed)
    check("在途超时重开 (崩溃兜底)", test_inflight_timeout_reopens)
    check("relay 扫描过滤 confirmed_at", test_scan_filter_uses_confirmed_at)
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)
