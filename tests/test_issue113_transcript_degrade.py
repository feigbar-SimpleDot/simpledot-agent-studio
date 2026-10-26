"""issue #113 (2026-10-25): harvest transcript 采集降级回归测试。

根因
----
`mw.harvest.export_transcript()` 用 `subprocess.run(["hermes", ...])`
拉 worker transcript。当 hermes CLI 不在 PATH 时 (FileNotFoundError,
OSError 的子类), 异常直接穿透 `harvest()` —— 整条结算链路炸掉:
卡永远落不了账, reaper 只能记一条 `verdict=ERROR`。

这不是「测试环境问题」而是**生产可达**的:
  - 容器里 hermes 装了但未必在 PATH (main_entry.sh 只 export PYTHONPATH);
  - `MW_EXPORT_PREFIX` 指向已卸载的镜像时同理;
  - 宿主裸跑 pytest 时必然命中。

既有的降级口径本来就允许没有 transcript (`doc=None` ⇒ 「导出失败/不完整
(降级为纯物理验收)」), 所以「命令不存在」应当归入降级, 而不是炸链路。

覆盖
----
  T1  CLI 不存在 ⇒ export_transcript 返回 None (不抛)
  T2  CLI 不存在 ⇒ harvest() 仍正常结算 (verdict=COMPLETE)
  T3  CLI 存在且成功 ⇒ transcript 照常解析 (降级不能误伤正常路径)
  T4  CLI 存在但返回非零 ⇒ 照旧返回 None (旧语义不变)
  T5  docker 前缀形态 + docker 不存在 ⇒ 同样降级不抛
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

# 让脚本可以 import mw 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mw.harvest import export_transcript, harvest  # noqa: E402
from mw.ledger import Ledger  # noqa: E402


def _build_ledger(harvest_status: str = "PENDING") -> tuple:
    """建一个临时 ledger, 塞一条 task contract。"""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    led = Ledger(db_path)
    task_id = "t_113_test"
    product_ws = tempfile.mkdtemp(prefix="b113_")
    led.put_contract(task_id, {"required_files": [os.path.join(product_ws, "out.txt")],
                               "timeout_seconds": 1800}, required_passes=None)
    led.conn.execute(
        "UPDATE harvest_contracts SET harvest_status=?, harvest_log='mock', "
        "updated_at=? WHERE task_id=?", (harvest_status, 0.0, task_id))
    led.conn.commit()
    led.upsert_session(task_id, "sess_113", "test", runner_kind="api",
                       workspace=product_ws)
    return led, db_path, task_id, product_ws


def _cleanup(led, db_path, product_ws):
    try:
        led.close()
    except Exception:
        pass
    for p in (db_path, product_ws):
        try:
            os.unlink(p)
        except OSError:
            pass


def test_t1_missing_cli_returns_none_not_raises(tmp_path, monkeypatch):
    """T1: CLI 二进制不存在 ⇒ 返回 None, 不抛 FileNotFoundError。"""
    # 指向一个确定不存在的命令 (PATH 里绝不会有这个名字)
    monkeypatch.setenv("MW_EXPORT_PREFIX", "mw_113_definitely_missing_binary")
    out = export_transcript("sess_113", str(tmp_path), str(tmp_path / "out"))
    assert out is None, f"CLI 缺失时应降级返回 None, 实际={out!r}"


def test_t2_missing_cli_still_settles_task(tmp_path, monkeypatch):
    """T2: CLI 缺失时 harvest() 仍应正常结算 (这是本卡的核心断言)。

    修前: FileNotFoundError 穿透 ⇒ 卡落不了账 (reaper 只记一条 ERROR)。
    """
    monkeypatch.setenv("MW_EXPORT_PREFIX", "mw_113_definitely_missing_binary")
    led, db_path, tid, pws = _build_ledger("PENDING")
    try:
        Path(pws, "out.txt").write_text("done")
        r = harvest(tid, ledger=led, hermes_home="", workspace=pws,
                    spawn_result=None)
        assert r["settled"], f"CLI 缺失不该阻断结算, 实际={r}"
        assert r["verdict"] == "COMPLETE", f"实际={r.get('verdict')}"
        # 结算确实落库了 (卡状态推进到终态)
        row = led.get_contract(tid)
        assert row["harvest_status"] == "HARVESTED", \
            f"实际={row['harvest_status']}"
    finally:
        _cleanup(led, db_path, pws)


def test_t3_existing_cli_still_parsed(tmp_path, monkeypatch):
    """T3: CLI 存在且成功 ⇒ transcript 照常解析 (降级不能误伤正常路径)。"""
    # 造一个真 CLI 桩: `sessions export --format jsonl --session-id X -Y <out>`
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "mw_113_fake_hermes"
    stub.write_text(
        "#!/bin/sh\n"
        "# 桩: 把最后���个参数当作输出路径, 写一份最小 transcript\n"
        "for last; do :; done\n"
        "printf '%s' '{\"messages\": [], \"end_reason\": \"stop\"}' > \"$last\"\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("MW_EXPORT_PREFIX", str(stub))
    out = export_transcript("sess_113", str(tmp_path), str(tmp_path / "out"))
    assert out is not None, "CLI 可用时必须照常拿到 transcript"
    assert out.get("end_reason") == "stop", f"实际={out!r}"


def test_t4_nonzero_exit_returns_none(tmp_path, monkeypatch):
    """T4: CLI 存在但返回非零 ⇒ 照旧返回 None (旧语义不变)。"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "mw_113_failing_hermes"
    stub.write_text("#!/bin/sh\nexit 3\n")
    stub.chmod(0o755)
    monkeypatch.setenv("MW_EXPORT_PREFIX", str(stub))
    out = export_transcript("sess_113", str(tmp_path), str(tmp_path / "out"))
    assert out is None, f"非零退出应返回 None, 实际={out!r}"


def test_t5_docker_prefix_missing_binary_degrades(tmp_path, monkeypatch):
    """T5: 真 docker 前缀形态 + 目标容器/docker 不可用 ⇒ 同样降级不抛。

    审查重要-1: 旧写法前缀首元素是 mw_113_missing_docker, 走的是 else
    (非 docker) 分支, 本批改动的 `if prefix[0]=="docker"` 分支零覆盖。
    改用真 docker 前缀: 容器不存在时 subprocess 返回非零 (或 docker 缺失
    抛 OSError), 两形态都必须降级返回 None。
    """
    monkeypatch.setenv("MW_EXPORT_PREFIX", "docker exec mw_113_no_such_container")
    out = export_transcript("sess_113", str(tmp_path), str(tmp_path / "out"))
    assert out is None, f"docker 形态失败时应降级返回 None, 实际={out!r}"


if __name__ == "__main__":
    sys.exit(pytest_main := __import__("pytest").main([__file__, "-v"]))
