"""issue #75 B1: /api/sessions/<sid>/messages 代理层窗口化单测。

覆盖 (ARCH-75 §5 + main 裁决 R2/R3 + §8 B1 验收):
  W-1  §5.1 口径: user(非 command)/assistant 带文本 计 1 条; tool /
       assistant 纯 tool_calls / 空文本 assistant 不计
  W-2  R2 (a2): tool_calls/tool 收进所属条目 tool_group, 顶层条目不含
       独立 tool 记录; thinking/reasoning 字段不下发 (AC-11)
  W-3  §5.2 窗口: 首屏尾部锚定 offset = max(0, message_count - W);
       W = clamp(ceil(want/d*SAFETY), 20, 200)
  W-4  触底: 返回数 < W → 到底 (has_more=false, before_cursor=null)
  W-5  自适应倍增: 窗口不足且未触底 → W*2 重试 (最多 3 次)
  W-6  before 游标: "o:<n>" 语义 = 边界, offset = max(0, n - W);
       非法游标 → 400
  W-7  AC-12 衔接: 第 2 批最旧条目 id 紧接第 1 批最旧 (无重复无缺口)
  W-8  message_count 缺失 → 502 显式报错, 不静默给错数据
  W-9  路由级: 不带 want/before 参数 → 不走窗口化 (旧全量行为回退)
  W-10 worker base (/api/w/<name>/...) 形态同样窗口化 (§9 R3)

纯逻辑经 fetch_raw/get_message_count 回调注入, 不联网。

issue #75 s9 M-3: 断言真判定化。check() 只 print 不 raise 的形态导致
pytest 收集下本套件恒绿 (失败仅在 __main__ 的 sys.exit 生效)。现每个
test_* 用 @test 包装: 用例内新增的 FAIL 条目在函数收尾转成真实
assert (pytest 判红), __main__ 直跑路径走 __wrapped__ 保持"全部跑完
再汇总 + exit(1)"的旧口径 —— 双轨兼容。
"""
import sys
from pathlib import Path

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


def _u(i):
    return {"id": f"u{i}", "role": "user", "content": f"问{i}",
            "timestamp": 100.0 + i}


def _a(i):
    return {"id": f"a{i}", "role": "assistant", "content": f"答{i}",
            "timestamp": 100.0 + i}


def _tc(i):
    return {"id": f"tc{i}", "role": "assistant", "content": "",
            "tool_calls": [{"id": f"c{i}", "type": "function",
                            "function": {"name": "terminal",
                                         "arguments": "{}"}}],
            "timestamp": 100.0 + i}


def _tr(i):
    return {"id": f"tr{i}", "role": "tool", "tool_call_id": f"c{i}",
            "content": "ok", "timestamp": 100.0 + i}


def _seq(n_conv, tool_dense=False):
    """造 n_conv 组 (user, [tc,tr], assistant) 原始序列。"""
    out = []
    for i in range(n_conv):
        out.append(_u(i))
        if tool_dense:
            out.append(_tc(i))
            out.append(_tr(i))
        out.append(_a(i))
    return out


# ── W-1 / W-2: 口径 + 回合重组 ────────────────────────────────────────
@_test_case
def test_w1_filter_caliber():
    raw = [_u(0), _tc(1), _tr(1), _a(1),
           {"id": "cmd", "role": "user", "content": "/new",
            "display_kind": "command"},
           {"id": "empty", "role": "assistant", "content": "   "},
           {"id": "think", "role": "assistant", "content": "t",
            "reasoning": "secret-thinking"}]
    entries, idxs = console.group_conversation_turns(raw)
    check("W-1.1 对话条目计数 (command/空文本 assistant 不计)",
          len(entries) == 3, f"got {len(entries)}")
    check("W-1.2 tool 记录不作为顶层条目",
          all(e["role"] in ("user", "assistant") for e in entries))
    check("W-1.3 thinking 字段不下发",
          all("reasoning" not in e and "reasoning_content" not in e
              for e in entries))


@_test_case
def test_w2_tool_group_attached():
    raw = [_u(0), _tc(1), _tr(1), _a(1)]
    entries, _ = console.group_conversation_turns(raw)
    a1 = entries[-1]
    check("W-2.1 tool_calls 收进所属 assistant 条目 tool_group",
          any(it.get("type") == "tool_call" and it.get("name") == "terminal"
              for it in a1["tool_group"]), str(a1["tool_group"]))
    check("W-2.2 tool 结果收进 tool_group",
          any(it.get("type") == "tool_result" and it.get("content") == "ok"
              for it in a1["tool_group"]))
    check("W-2.3 user 条目不带前一批 tool_group",
          entries[0]["tool_group"] == [])


# ── W-3 尾部锚定 ─────────────────────────────────────────────────────
@_test_case
def test_w3_tail_anchor():
    seq = _seq(40)  # 120 条
    calls = []

    def fetch(offset, limit):
        calls.append((offset, limit))
        return seq[offset:offset + limit]

    out = console.build_messages_window(fetch, lambda: len(seq), want=10)
    import math
    W_expect = min(console.MAX_WINDOW,
                   max(console._WINDOW_MIN,
                       math.ceil(10 / console.CONV_RATIO_EST * 1.5)))
    check("W-3.1 首窗 W = clamp(ceil(want/d*safety),20,200)",
          calls[0][1] == W_expect, f"got {calls[0][1]}, expect {W_expect}")
    # s9 M-3 闭环 (W-3.2 = 期望 bug, s4 裁定): 80 条会话 W=85 时期望
    # len(seq)-W = -5 不可达; 代码 max(0, ...) 钳到 0 是正确行为 (§5.2 /
    # 本文件 docstring W-3 行本就写着 max(0, ...))。修期望, 不动代码。
    check("W-3.2 尾部锚定 offset = max(0, message_count - W)",
          calls[0][0] == max(0, len(seq) - W_expect), f"got {calls[0][0]}")
    check("W-3.3 首屏恰得 want 条对话",
          len(out["data"]) == 10, f"got {len(out['data'])}")
    check("W-3.4 返回的是最近一批 (末条 = 最新对话)",
          out["data"][-1]["id"] == "a39", str(out["data"][-1]["id"]))
    check("W-3.5 顶层有 history 元数据",
          isinstance(out.get("history"), dict)
          and "has_more" in out["history"]
          and "before_cursor" in out["history"]
          and "conversation_count" in out["history"])


# ── W-4 触底 ─────────────────────────────────────────────────────────
@_test_case
def test_w4_bottom_reached():
    seq = _seq(3)  # 6 条原始记录 (3 user + 3 assistant) < 任何 W
    out = console.build_messages_window(lambda o, l: seq[o:o + l],
                                        lambda: len(seq), want=10)
    # s9 M-3 闭环 (W-4.1 = 期望 bug): §5.1 口径 user 与带文本 assistant 各
    # 计 1 条对话内容 ⇒ 3 组 = 6 条, 非 3 条 (W-3.4/W-7 同款 id 序列
    # u35,a35,... 已证逐消息计数)。原注释 "9 条" 是 tool_dense 形态的
    # 数字, 本用默认非 tool_dense。修期望, 不动代码。
    check("W-4.1 全会话仅 3 组 → 返回 6 条对话内容",
          len(out["data"]) == 6, f"got {len(out['data'])}")
    check("W-4.2 触底 → has_more=false, before_cursor=null",
          out["history"]["has_more"] is False
          and out["history"]["before_cursor"] is None,
          str(out["history"]))


# ── W-5 自适应倍增 ───────────────────────────────────────────────────
@_test_case
def test_w5_adaptive_expand():
    # tool 密集段: 1 条对话 + 200 条 tool 前缀 → 首窗几乎全是 tool
    dense = [_u(0)] + [_tc(i) for i in range(300)] + [_a(9)]
    calls = []

    def fetch(offset, limit):
        calls.append((offset, limit))
        return dense[offset:offset + limit]

    out = console.build_messages_window(fetch, lambda: len(dense), want=10)
    check("W-5.1 窗口不足时倍增重试 (多次 fetch)",
          len(calls) >= 2, f"calls={calls}")
    check("W-5.2 重试上限 MAX_WINDOW_EXPAND 次",
          len(calls) <= 1 + console.MAX_WINDOW_EXPAND, f"calls={calls}")
    check("W-5.3 窗口上限不超 MAX_WINDOW",
          all(l <= console.MAX_WINDOW for _, l in calls))
    check("W-5.4 仍返回结构化结果 (含已攒到的对话)",
          "data" in out and "history" in out)


# ── W-6 游标语义 ─────────────────────────────────────────────────────
@_test_case
def test_w6_before_cursor():
    seq = _seq(40)
    calls = []

    def fetch(offset, limit):
        calls.append((offset, limit))
        return seq[offset:offset + limit]

    out = console.build_messages_window(fetch, lambda: len(seq),
                                        want=10, before="o:60")
    import math
    W_expect = min(console.MAX_WINDOW,
                   max(console._WINDOW_MIN,
                       math.ceil(10 / console.CONV_RATIO_EST * 1.5)))
    # s9 M-3 闭环 (W-6.1 = 期望 bug, s4 裁定): 60-85 = -25, 代码 max(0,...)
    # 钳到 0 正确 (检查名本身即写着 max(0, 60-W))。修期望, 不动代码。
    check("W-6.1 before=o:60 → offset = max(0, 60-W)",
          calls[0][0] == max(0, 60 - W_expect), f"got {calls[0][0]}")
    bad = console.build_messages_window(fetch, lambda: 10, before="abc")
    check("W-6.2 非法游标 → 400", bad.get("code") == 400, str(bad))
    ok2 = console.build_messages_window(fetch, lambda: len(seq),
                                        want=10, before="60")
    # s9 M-3 闭环 (W-6.3 = 期望 bug, 同 W-6.1 钳位)。修期望, 不动代码。
    check("W-6.3 纯数字游标兼容",
          calls[-1][0] == max(0, 60 - W_expect), str(calls[-1]))


# ── W-7 AC-12 衔接无缺口 ─────────────────────────────────────────────
@_test_case
def test_w7_no_gap_between_batches():
    seq = _seq(40)

    def fetch(offset, limit):
        return seq[offset:offset + limit]

    b1 = console.build_messages_window(fetch, lambda: len(seq), want=10)
    cur = b1["history"]["before_cursor"]
    check("W-7.1 首批 has_more=true 且有游标",
          b1["history"]["has_more"] is True and cur is not None)
    b2 = console.build_messages_window(fetch, lambda: len(seq),
                                       want=10, before=cur)
    ids1 = [e["id"] for e in b1["data"]]
    ids2 = [e["id"] for e in b2["data"]]
    # W-7.2/W-7.3 原为真实产品缺陷 (= B-1, s4 裁定, 不许改期望掩盖):
    # boundary<W 时 fetch limit 越过边界 → 第二批与第一批整体重复。
    # s9 已修产品 (console.py build_messages_window: 游标批 limit 截到
    # 边界), 本两用例在修前源码上复跑必红 (回归锚自证见 FIX-NOTES-75B)。
    check("W-7.2 两批无重叠 (id 集合不相交)",
          not (set(ids1) & set(ids2)), f"{ids1} / {ids2}")
    # 第 1 批最旧条目的前一条对话 = 第 2 批最新条目 (衔接无缺口)
    seq_conv_ids = [m["id"] for m in seq if console._is_conversation_entry(m)]
    idx = seq_conv_ids.index(ids1[0])
    expect_prev = seq_conv_ids[idx - 1] if idx > 0 else None
    check("W-7.3 第 2 批最新 = 第 1 批最旧的前一条对话 (无缺口)",
          ids2 and ids2[-1] == expect_prev,
          f"ids2[-1]={ids2[-1] if ids2 else None}, expect={expect_prev}")
    check("W-7.4 第 2 批同样 want 条", len(ids2) == 10, f"got {len(ids2)}")


# ── W-8 message_count 缺失 ───────────────────────────────────────────
@_test_case
def test_w8_no_message_count():
    out = console.build_messages_window(lambda o, l: [], lambda: None,
                                        want=10)
    check("W-8.1 message_count 缺失 + 无游标 → 502 显式报错",
          out.get("code") == 502, str(out))
    check("W-8.2 有 before 游标时不依赖 message_count",
          "data" in console.build_messages_window(
              lambda o, l: [], lambda: None, want=10, before="o:5"))


# ── W-9/W-10 路由级 (MockHandler, 不绑 server; 起真服归 s5) ──────────
class MockHandler(console.Handler):
    """绕开 BaseHTTPRequestHandler.__init__ (不联网), 只装路由所需属性。"""

    def __init__(self, path):  # noqa: D107
        self.path = path
        self.headers = {}
        self.status = None
        self.sent_headers = []
        self.body = b""
        self.wfile = self

    def send_response(self, code): self.status = code

    def send_header(self, k, v): self.sent_headers.append((k, v))

    def end_headers(self): pass

    def write(self, chunk): self.body += chunk


@_test_case
def test_w9_route_backward_compat():
    h = MockHandler("/api/sessions/sid1/messages")
    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(h.path).query, keep_blank_values=True)
    handled = h._gateway_messages_windowed(
        h.path.split("?")[0], qs)
    check("W-9.1 不带 want/before → 不走窗口化 (回退旧透传)",
          handled is False)
    # 带参数 → 窗口化路径接管; 归因 code 来自上游 gateway
    # (本容器 gateway 返 401; 无 gateway 时为 502) —— 断言"已应答且
    # 状态码是上游真实 code", 不再钉死 502 (issue #80-3 归因细分后,
    # 上游 code 原样透传, 钉死 502 会与修复本身冲突)。
    handled2 = h._gateway_messages_windowed(
        "/api/sessions/sid1/messages", {"want": ["10"]})
    check("W-9.2 带 want → 窗口化路径接管 (状态码 = 上游 gateway code)",
          handled2 is True and h.status in (401, 502),
          f"status={h.status}")
    # issue #80-3 (归因细分) 后首批不再恒报 message_count 缺失:
    # gateway 传输错误 (不可达/认证失败/5xx) 优先透传上游真实 code 与
    # body; 只有 gateway 可达却真的缺 message_count 时才是 502
    # "session message_count unavailable" (W-11.4 覆盖该情形)。
    # 原断言 b"message_count" 在 body 里 → 改断言该路径真实可得的
    # error 归因字段 (A1: 去掉 message_count 条件, 只留 '"error"')。
    check("W-9.3 502 响应体带 error 归因字段",
          b'"error"' in h.body,
          str(h.body[:120]))


@_test_case
def test_w10_worker_base_form():
    # worker 形态路由解析 (不联网部分): 未注册 worker → 404
    h = MockHandler("/api/w/__no_such_worker__/api/sessions/s/messages")
    handled = h._gateway_messages_windowed(
        h.path.split("?")[0], {"want": ["10"]})
    check("W-10.1 worker 形态识别为 /messages 且已应答", handled is True)
    check("W-10.2 未注册 worker → 404 (与 _proxy_to_worker 同口径)",
          h.status == 404, f"status={h.status}")


# ── W-11 s6 缺陷 1: 真实 gateway 响应形状驱动 get_message_count ──────
# 回归背景: gateway GET /api/sessions/<sid> 真实返回
#   {"object": "...", "session": {..., "message_count": 567}}
# 原实现只从顶层读 message_count ⇒ 恒 None ⇒ 窗口化端点恒 502。
# 单测此前用 mock 回调直接注入 message_count, 未覆盖该形状。
@_test_case
def test_w11_real_gateway_shape():
    """路由/集成层: 用真实 gateway 响应形状驱动, 不能只测纯逻辑回调。"""
    real_shape = {"object": "hermes.session",
                  "session": {"id": "sid1", "message_count": 567,
                              "model": "m", "title": "t"}}

    class ShapeHandler(MockHandler):
        """只桩掉网络读取: session detail 返真实嵌套形状。"""

        def _gateway_base_key_for(self, p):
            return ("http://stub", "", p)

        def _gw_fetch_json(self, base, key, path):
            if "/messages" not in path:
                return real_shape, None
            # messages 分页: 造 567 条里的一段 (全 user, 恒够 want)
            import re as _re
            m = _re.search(r"offset=(\d+)&limit=(\d+)", path)
            off = int(m.group(1)) if m else 0
            lim = int(m.group(2)) if m else 20
            return {"data": [{"id": f"m{i}", "role": "user",
                              "content": f"c{i}"}
                             for i in range(off, min(off + lim, 567))]}, None

    # 嵌套形状 (真实): get_message_count 必须取到 567 → 窗口正常应答 200
    h = ShapeHandler("/api/sessions/sid1/messages")
    handled = h._gateway_messages_windowed(
        "/api/sessions/sid1/messages", {"want": ["10"]})
    import json as _json
    body = _json.loads(h.body.decode()) if h.body else {}
    check("W-11.1 真实嵌套形状 → 取到 message_count, 应答 200 (非 502)",
          handled is True and h.status == 200, f"status={h.status}")
    check("W-11.2 返回 10 条对话内容",
          len(body.get("data", [])) == 10, str(len(body.get("data", []))))

    # 顶层扁平形状 (兜底兼容): 仍要能取到
    class FlatHandler(ShapeHandler):
        def _gw_fetch_json(self, base, key, path):
            if "/messages" not in path:
                return {"object": "hermes.session", "message_count": 42}, None
            return super()._gw_fetch_json(base, key, path)

    h2 = FlatHandler("/api/sessions/sid1/messages")
    h2._gateway_messages_windowed("/api/sessions/sid1/messages",
                                  {"want": ["10"]})
    check("W-11.3 顶层扁平形状兜底兼容 → 200",
          h2.status == 200, f"status={h2.status}")

    # 两种形态都缺 → 维持 502 显式报错语义 (不猜 offset=0)
    class NoneHandler(ShapeHandler):
        def _gw_fetch_json(self, base, key, path):
            if "/messages" not in path:
                return {"object": "hermes.session", "session": {"id": "s"}}, None
            return super()._gw_fetch_json(base, key, path)

    h3 = NoneHandler("/api/sessions/sid1/messages")
    h3._gateway_messages_windowed("/api/sessions/sid1/messages",
                                  {"want": ["10"]})
    check("W-11.4 两形态皆缺 → 502 显式报错 (语义不变)",
          h3.status == 502, f"status={h3.status}")


# ── W-13 issue #81-3: 非法 want → 400 (与 before 同口径) ─────────────
# 旧实现把 int() 写在 build_messages_window 的实参里, ValueError 逃出
# 路由函数 → do_GET 兜底 500 + "invalid literal for int()"。
# 修复后: 前置解析, 非法值与非法 before 一样回 400。
@_test_case
def test_w13_bad_want_is_400():
    h = MockHandler("/api/sessions/sid1/messages")
    handled = h._gateway_messages_windowed(
        "/api/sessions/sid1/messages", {"want": ["abc"]})
    check("W-13.1 want=abc → 400 (非 500)", handled is True and h.status == 400,
          f"status={h.status} body={h.body[:120]!r}")
    check("W-13.2 400 响应体带 error 归因字段", b'"error"' in h.body,
          str(h.body[:120]))


@_test_case
def test_w13_blank_want_is_400():
    h = MockHandler("/api/sessions/sid1/messages")
    handled = h._gateway_messages_windowed(
        "/api/sessions/sid1/messages", {"want": [""]})
    check("W-13.3 want= (空串) → 400 (非 500)",
          handled is True and h.status == 400,
          f"status={h.status} body={h.body[:120]!r}")


# ── W-14 issue #81-4: gateway 大错误体不再被截断重解析炸掉 ──────────
# 旧实现把 (code, body) 拼字符串后 partition + json.loads 重解析, body
# 超过 [:200] 即抛 JSONDecodeError 逃到 do_GET 兜底 500 —— 真实状态码
# 与上游错误串双丢。修复后: 元组直传, code 与 body 原样透传。
class _StubGwHandler(MockHandler):
    """桩掉 gateway 读取, 只回固定 (err_code, err_body)。"""

    ERR_CODE = 502
    ERR_BODY = {}

    def _gateway_base_key_for(self, p):
        return ("http://stub", "", p)

    def _gw_fetch_json(self, base, key, path):
        return None, (self.ERR_CODE, self.ERR_BODY)


@_test_case
def test_w14_gateway_error_body_preserved():
    """游标批 (有 before) + gateway 大错误体 → 502 + 上游 error 原串。"""
    long_msg = "X" * 400          # 远超旧的 [:200] 截断阈值
    body = {"error": {"message": long_msg, "type": "gateway_error"}}

    class H(_StubGwHandler):
        ERR_CODE = 502
        ERR_BODY = body

    h = H("/api/sessions/sid1/messages")
    handled = h._gateway_messages_windowed(
        "/api/sessions/sid1/messages", {"want": ["10"], "before": ["o:5"]})
    check("W-14.1 大错误体游标批 → 502 (非 500 JSONDecodeError)",
          handled is True and h.status == 502,
          f"status={h.status} body={h.body[:160]!r}")
    check("W-14.2 响应体含上游 error 原串 (未丢)",
          long_msg.encode() in h.body,
          str(h.body[:160]))


@_test_case
def test_w14_large_body_no_double_encode():
    """上游 body 是 dict 时, 响应的 error 字段是上游 dict 原样, 非字符串化。"""
    body = {"error": {"message": "Y" * 400, "code": "gateway_boom"}}

    class H(_StubGwHandler):
        ERR_CODE = 502
        ERR_BODY = body

    h = H("/api/sessions/sid1/messages")
    h._gateway_messages_windowed(
        "/api/sessions/sid1/messages", {"want": ["10"], "before": ["o:5"]})
    import json as _json
    try:
        parsed = _json.loads(h.body.decode())
    except Exception as e:                     # noqa: BLE001
        check("W-14.3 响应体是合法 JSON", False, f"{type(e).__name__}: {e}")
        return
    check("W-14.3 error 字段是上游 dict 原样 (未双重编码成字符串)",
          isinstance(parsed.get("error"), dict)
          and parsed["error"].get("code") == "gateway_boom",
          str(parsed)[:200])


# ── W-15 issue #80-3: 首批归因细分 (传输错误不再伪装成字段缺失) ──────
@_test_case
def test_w15_first_batch_reports_real_gateway_error():
    """首批 (无 before) + gateway 传输错误 → 上游 code 与 body 透传。"""
    class H(_StubGwHandler):
        ERR_CODE = 401
        ERR_BODY = {"error": {"message": "Invalid gateway API key",
                              "code": "gateway_auth_failed"}}

    h = H("/api/sessions/sid1/messages")
    handled = h._gateway_messages_windowed(
        "/api/sessions/sid1/messages", {"want": ["10"]})
    check("W-15.1 首批传输错误 → 上游 code (401) 原样透传",
          handled is True and h.status == 401,
          f"status={h.status} body={h.body[:160]!r}")
    check("W-15.2 首批不再伪装成 message_count 缺失",
          b"session message_count unavailable" not in h.body,
          str(h.body[:160]))


@_test_case
def test_w15_missing_message_count_still_502():
    """反证: gateway 可达但真的缺 message_count → 仍 502 显式报错。"""

    class OkHandler(MockHandler):
        def _gateway_base_key_for(self, p):
            return ("http://stub", "", p)

        def _gw_fetch_json(self, base, key, path):
            if "/messages" not in path:
                return {"object": "hermes.session",
                        "session": {"id": "s"}}, None
            return {"data": []}, None

    h = OkHandler("/api/sessions/sid1/messages")
    handled = h._gateway_messages_windowed(
        "/api/sessions/sid1/messages", {"want": ["10"]})
    check("W-15.3 gateway 可达但缺 message_count → 502 语义不变",
          handled is True and h.status == 502
          and b"session message_count unavailable" in h.body,
          f"status={h.status} body={h.body[:160]!r}")


# ── W-12 s9 B-1 验收: boundary < W 深翻批次零重叠 (卡点名用例) ────────
# 卡 (issue #75 s9 B-1) 验收线索: "补一个 80 条会话 / W=85 / 两批 的用例,
# 断言第二批与第一批 id 零重叠 —— 必须真 assert, 不能再用 check()。"
# 静态推演 (s4 报告): W=ceil(10/0.178*1.5)=85 > 80 条会话 ⇒ 第二批
# before=o:70 时 offset 钳到 0 而 limit 仍 85 → 越过 boundary 70, 把已展示
# 记录 70..79 原样拉回 = 与首批 100% 重复。修后 limit 截到边界, 零重叠。
@_test_case
def test_w12_boundary_below_window_no_overlap():
    seq = _seq(40)                     # 80 条原始记录, 全是对话条目
    assert len(seq) == 80
    import math
    W = min(console.MAX_WINDOW,
            max(console._WINDOW_MIN,
                math.ceil(10 / console.CONV_RATIO_EST * 1.5)))
    assert W == 85, f"卡口径 W=85, got {W}"
    assert W > len(seq), "用例前提: boundary(80) < W(85)"

    calls = []

    def fetch(offset, limit):
        calls.append((offset, limit))
        return seq[offset:offset + limit]

    b1 = console.build_messages_window(fetch, lambda: len(seq), want=10)
    cur = b1["history"]["before_cursor"]
    ids1 = [e["id"] for e in b1["data"]]
    assert cur is not None and ids1, "首批必须有数据与游标"

    b2 = console.build_messages_window(fetch, lambda: len(seq),
                                       want=10, before=cur)
    ids2 = [e["id"] for e in b2["data"]]
    # 真 assert (非 check()): 第二批与第一批 id 零重叠
    assert not (set(ids1) & set(ids2)), \
        f"B-1 复发: 两批 id 重叠 {sorted(set(ids1) & set(ids2))}"
    # 取数层实证: before 批的 limit 不得越过边界 (≤ cursor 值)
    boundary = int(str(cur)[2:] if str(cur).startswith("o:") else cur)
    for off, lim in calls[1:]:
        assert off + lim <= boundary, \
            f"游标批窗口 [{off},{off+lim}) 越过边界 {boundary}"
    # 衔接无缺口: 第 2 批最新条目紧接第 1 批最旧条目的前一条
    conv_ids = [m["id"] for m in seq
                if console._is_conversation_entry(m)]
    idx = conv_ids.index(ids1[0])
    assert ids2[-1] == conv_ids[idx - 1], \
        f"衔接缺口: ids2[-1]={ids2[-1]}, expect={conv_ids[idx-1]}"


if __name__ == "__main__":
    for fn in [test_w1_filter_caliber, test_w2_tool_group_attached,
               test_w3_tail_anchor, test_w4_bottom_reached,
               test_w5_adaptive_expand, test_w6_before_cursor,
               test_w7_no_gap_between_batches, test_w8_no_message_count,
               test_w9_route_backward_compat, test_w10_worker_base_form,
               test_w11_real_gateway_shape,
               test_w13_bad_want_is_400, test_w13_blank_want_is_400,
               test_w14_gateway_error_body_preserved,
               test_w14_large_body_no_double_encode,
               test_w15_first_batch_reports_real_gateway_error,
               test_w15_missing_message_count_still_502,
               test_w12_boundary_below_window_no_overlap]:
        print(f"== {fn.__name__}")
        # 直跑口径: 走 __wrapped__ 绕过 pytest 化 assert, 保持
        # "全部跑完再汇总 + exit(1)" 旧行为 (M-3 双轨兼容)。
        getattr(fn, "__wrapped__", fn)()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
