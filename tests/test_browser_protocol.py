# -*- coding: utf-8 -*-
"""
浏览器本机桥协议/策略/连接测试（P6）
==================================
运行：python tests/test_browser_protocol.py

覆盖红线（交接包 T34）：错 token 断开、未配对/未连接拒绝下发、
危险 URL（chrome://、支付页、扩展商店）拒绝、密码/验证码/最终提交拒绝、
能力开关默认关、配对码一次性、指令往返、坏消息断开。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from browser.protocol import (BrowserAction, HelloMessage, generate_pair_code,
                              make_command, normalize_pair_code,
                              parse_inbound, ProtocolError)
from browser.policy import BrowserPolicy, is_forbidden_url, is_sensitive_element
from browser.bridge import BrowserBridge, HOST
import websockets

# ---------------- protocol 纯函数 ----------------

def test_pair_code_normalize():
    code = generate_pair_code()
    # 带横杠、小写、空格都能归一
    assert normalize_pair_code(code.lower()) == normalize_pair_code(code)
    assert normalize_pair_code(" ab2c-33xx ") == "AB2C33XX"
    # 不含易混字符
    assert not set(normalize_pair_code(code)) & set("0O1IL")


def test_parse_inbound_ok():
    h = parse_inbound({"type": "hello", "pair_code": "ABCD-1234"})
    assert isinstance(h, HelloMessage) and h.pair_code == "ABCD1234"
    r = parse_inbound(json.dumps({"type": "result", "session": "s",
                                  "cmd_id": "c", "ok": True}))
    assert r.ok is True


def test_parse_inbound_rejects_bad():
    for bad in ["notjson", {}, {"type": "rm_rf"},
                {"type": "hello"},                      # 缺配对码和会话
                {"type": "result", "session": "s", "cmd_id": "c",
                 "ok": True, "evil": 1}]:               # extra forbid
        try:
            parse_inbound(bad)
            raise AssertionError(f"没拦住: {bad}")
        except ProtocolError:
            pass


def test_make_command_whitelist():
    pkt = make_command("s", "c", BrowserAction.CLICK, "#ok")
    assert pkt["action"] == "click"
    try:
        make_command("s", "c", "eval_script")
        raise AssertionError("陌生动作没拦")
    except ProtocolError:
        pass


def test_hello_reconnect_flag():
    first = HelloMessage(type="hello", pair_code="ABCD1234")
    assert not first.is_reconnect()
    back = HelloMessage(type="hello", session="xyz")
    assert back.is_reconnect()


# ---------------- policy 策略 ----------------

def test_forbidden_urls():
    bad = ["chrome://settings", "edge://extensions", "file:///C:/a",
           "https://chromewebstore.google.com/",
           "https://www.alipay.com/", "https://bank.example.com/pay",
           "https://shop.com/order/confirm"]
    for u in bad:
        forbidden, why = is_forbidden_url(u)
        assert forbidden, f"应禁止 {u}"
        assert why
    ok = ["https://example.com/article", "http://blog.test.cn/post/1"]
    for u in ok:
        assert not is_forbidden_url(u)[0], u


def test_forbidden_urls_with_path_in_blacklist():
    """黑名单里带路径的条目也必须能命中。

    真实踩过的坑：清单里有 "chrome.google.com/webstore"，但匹配时只拿
    hostname 去 `in`——hostname 永远不含 "/"，这条规则等于永不生效，
    扩展商店页实际没被拦住。
    """
    bad = ["https://chrome.google.com/webstore/detail/xxx",
           "chrome.google.com/webstore",
           "https://chrome.google.com/webstore/category/extensions"]
    for u in bad:
        forbidden, why = is_forbidden_url(u)
        assert forbidden, f"应禁止 {u}"
        assert "商店" in why
    # 同一域名下的普通页面不该被误伤
    assert not is_forbidden_url("https://chrome.google.com/something-else")[0]


def test_sensitive_elements():
    assert is_sensitive_element(BrowserAction.TYPE_TEXT, "input",
                                {"selector": "input[name=password]"})[0]
    assert is_sensitive_element(BrowserAction.TYPE_TEXT, "code",
                                {"label": "短信验证码"})[0]
    assert is_sensitive_element(BrowserAction.CLICK, "确认付款")[0]
    assert is_sensitive_element(BrowserAction.CLICK, "上传",
                                {"selector": "type=file"})[0]
    # 普通按钮/普通输入框放行
    assert not is_sensitive_element(BrowserAction.CLICK, "搜索")[0]
    assert not is_sensitive_element(BrowserAction.TYPE_TEXT, "搜索框",
                                    {"selector": "input[name=q]"})[0]


def test_policy_capability_default_off():
    p = BrowserPolicy({})   # 全默认关
    ok, why = p.check(BrowserAction.SCROLL, "https://x.com")
    assert not ok and "设置里打开" in why
    # 打开滚动能力 + 普通页面 -> 放行
    p2 = BrowserPolicy({"allow_scroll": True})
    assert p2.check(BrowserAction.SCROLL, "https://x.com")[0]
    # 开了能力但页面是支付页 -> 仍拒
    p3 = BrowserPolicy({"allow_click": True})
    assert not p3.check(BrowserAction.CLICK, "https://pay.alipay.com/x")[0]


def test_policy_open_url_only_http():
    p = BrowserPolicy({"allow_navigate": True})
    assert p.check(BrowserAction.OPEN_URL, "https://example.com")[0]
    assert not p.check(BrowserAction.OPEN_URL, "javascript:alert(1)")[0]
    assert not p.check(BrowserAction.OPEN_URL, "chrome://settings")[0]


# ---------------- bridge 真实本机连接（async） ----------------

def _run(coro):
    return asyncio.run(coro)


async def _pair_and_command():
    bridge = BrowserBridge(lambda: {"port": 8797})
    pretty = await bridge.start_pairing()
    code = normalize_pair_code(pretty)
    uri = f"ws://{HOST}:{bridge.port}"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"type": "hello", "pair_code": code}))
        ack = json.loads(await ws.recv())
        assert ack["ok"] and ack["session"]
        session = ack["session"]
        assert bridge.is_connected

        async def worker():
            pkt = json.loads(await ws.recv())
            assert pkt["type"] == "command"
            await ws.send(json.dumps({"type": "result", "session": session,
                                      "cmd_id": pkt["cmd_id"], "ok": True,
                                      "data": {"ok": 1}}))
        t = asyncio.create_task(worker())
        res = await bridge.send_command(BrowserAction.SCROLL, "", {"dir": "down"})
        await t
        assert res["ok"] and res["data"] == {"ok": 1}
    await bridge.unpair()


def test_bridge_pair_and_command():
    _run(_pair_and_command())


async def _wrong_pair_code():
    bridge = BrowserBridge(lambda: {"port": 8798})
    await bridge.start_pairing()
    try:
        async with websockets.connect(f"ws://{HOST}:{bridge.port}") as ws:
            await ws.send(json.dumps({"type": "hello", "pair_code": "WRONG-0000"}))
            ack = json.loads(await ws.recv())
            assert not ack["ok"]
    finally:
        await bridge.unpair()


def test_bridge_wrong_pair_code_rejected():
    _run(_wrong_pair_code())


async def _bad_session_dropped():
    bridge = BrowserBridge(lambda: {"port": 8796})
    pretty = await bridge.start_pairing()
    code = normalize_pair_code(pretty)
    async with websockets.connect(f"ws://{HOST}:{bridge.port}") as ws:
        await ws.send(json.dumps({"type": "hello", "pair_code": code}))
        await ws.recv()
        await ws.send(json.dumps({"type": "ping", "session": "forged"}))
        dropped = False
        try:
            await asyncio.wait_for(ws.recv(), timeout=3)
        except Exception:
            dropped = True
        assert dropped
    await bridge.unpair()


def test_bridge_bad_session_dropped():
    _run(_bad_session_dropped())


async def _not_connected():
    bridge = BrowserBridge(lambda: {"port": 8795})
    res = await bridge.send_command(BrowserAction.CLICK, "#x")
    assert not res["ok"] and "没连接" in res["error"]


def test_bridge_command_without_connection_rejected():
    _run(_not_connected())


async def _pair_code_one_time():
    bridge = BrowserBridge(lambda: {"port": 8794})
    pretty = await bridge.start_pairing()
    code = normalize_pair_code(pretty)
    async with websockets.connect(f"ws://{HOST}:{bridge.port}") as ws:
        await ws.send(json.dumps({"type": "hello", "pair_code": code}))
        ack = json.loads(await ws.recv())
        assert ack["ok"]
    # 配对码用完即焚：再拿同一个码来必须失败（需要重新生成）
    await asyncio.sleep(0.2)
    try:
        async with websockets.connect(f"ws://{HOST}:{bridge.port}") as ws2:
            await ws2.send(json.dumps({"type": "hello", "pair_code": code}))
            ack2 = json.loads(await ws2.recv())
            assert not ack2["ok"]
    finally:
        await bridge.unpair()


def test_bridge_pair_code_one_time():
    _run(_pair_code_one_time())


# ---------------- planner 自然语言规划（P6 补） ----------------
from browser.planner import BrowserPlanner, looks_like_browser_action, parse_plan


def test_browser_gate():
    assert looks_like_browser_action("往下滑动一下网页")
    assert looks_like_browser_action("帮我打开百度")
    assert looks_like_browser_action("读一下这个网页讲了什么")
    assert looks_like_browser_action("打开 github.com")
    assert not looks_like_browser_action("今天天气怎么样")
    assert not looks_like_browser_action("给我讲个笑话")


def test_parse_plan_scroll():
    p1 = parse_plan('{"action":"scroll","args":{"dir":"up"}}')
    assert p1 and p1.action == "scroll" and p1.args["dir"] == "up"
    p2 = parse_plan('{"action":"scroll"}')          # 缺 dir 默认 down
    assert p2 and p2.args["dir"] == "down"


def test_parse_plan_open_url_and_none():
    p1 = parse_plan('{"action":"open_url","target":"baidu.com"}')
    assert p1 and p1.target == "https://baidu.com"   # 裸域名补协议
    assert parse_plan('{"action":"none"}') is None
    assert parse_plan('{"action":"hack"}') is None   # 非法动作


def test_browser_planner_fake_llm():
    async def fake_scroll(system, user):
        return '{"action":"scroll","args":{"dir":"down"},"reason":"想往下看"}'
    cfg = {"allow_scroll": True}
    pl = _run(BrowserPlanner(lambda: cfg).plan(fake_scroll, "往下滑网页"))
    assert pl and pl.action == "scroll"
    # gate 不命中 → None（不调模型）
    pl2 = _run(BrowserPlanner(lambda: cfg).plan(fake_scroll, "随便聊聊"))
    assert pl2 is None
    # 能力开关没开 → None
    async def fake_open(system, user):
        return '{"action":"open_url","target":"x.com"}'
    pl3 = _run(BrowserPlanner(lambda: {"allow_navigate": False})
               .plan(fake_open, "打开x.com"))
    assert pl3 is None


# ---------------- 运行器 ----------------
def _run_all():
    tests = [(n, fn) for n, fn in sorted(globals().items())
             if n.startswith("test_") and callable(fn)]
    failed = 0
    for n, fn in tests:
        try:
            fn(); print(f"  [PASS] {n}")
        except AssertionError as e:
            failed += 1; print(f"  [FAIL] {n}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback; traceback.print_exc()
            print(f"  [ERR ] {n}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    if failed == 0:
        print("ALL TESTS PASSED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
