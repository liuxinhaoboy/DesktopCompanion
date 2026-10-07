# -*- coding: utf-8 -*-
"""
hands 受限执行器单元测试（P5）
=============================
运行：python tests/test_hands.py

覆盖红线：路径穿越 / 符号链接逃逸 / 确认令牌过期·一次性·不匹配 /
回收站删除与回滚快照 / 进程白名单与受保护进程 / 剪贴板快照恢复 /
审计脱敏 / schema 白名单 / planner 容错 / executor 二次校验。

副作用隔离：进程用替身不起真进程，剪贴板用替身不碰真实剪贴板；
文件操作全部在 tempfile 的 workspace 内，删除走系统回收站（临时文件）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PyQt6.QtWidgets import QApplication  # noqa: E402

_qapp = None


def _get_qapp():
    global _qapp
    if _qapp is None:
        _qapp = QApplication.instance() or QApplication(sys.argv)
    return _qapp


from hands.schema import ActionKind, ActionRequest, PolicyError  # noqa: E402
from hands.policy import PolicyGate, ConfirmationToken  # noqa: E402
from hands.executor import HandsExecutor  # noqa: E402
from hands.processes import ProcessHands, PROTECTED_PROCESSES  # noqa: E402
from hands.clipboard import ClipboardHands  # noqa: E402
from hands import planner as planner_mod  # noqa: E402


# ----------------------------------------------------------------------
# 辅助：搭一个临时 workspace + executor
# ----------------------------------------------------------------------
def _workspace():
    base = Path(tempfile.mkdtemp(prefix="dc_hands_"))
    ws = base / "ws"
    ws.mkdir()
    return base, ws


def _executor(ws, **over):
    cfg = {
        "workspace_root": str(ws), "enable_rollback_snapshot": True,
        "allow_files": True, "allow_clipboard": True, "allow_windows": True,
        "allow_processes": True, "confirm_policy": "always",
        "process_whitelist": ["notepad", "calc"],
    }
    cfg.update(over)
    base = ws.parent
    return HandsExecutor(base, cfg)


def _token(req, ttl=30):
    return ConfirmationToken.issue(req, ttl=ttl)


# ======================================================================
# 1. schema 白名单
# ======================================================================

def test_schema_rejects_unknown_and_dangerous_action():
    for bad in ("run_shell", "exec_powershell", "download_and_run", ""):
        try:
            ActionRequest(action=bad)
            assert False, f"{bad} 应被拒"
        except Exception:  # noqa: BLE001
            pass
    # 合法动作能建
    r = ActionRequest(action=ActionKind.FILE_LIST, target=".")
    assert r.risk in ("low", "medium", "high")


def test_schema_from_model_json_tolerant():
    r = ActionRequest.from_model_json('{"action":"file_mkdir","target":"a"}')
    assert r.action == ActionKind.FILE_MKDIR
    # 裹 markdown / 前后废话由 planner.extract_json 处理；这里直接给坏 JSON
    try:
        ActionRequest.from_model_json("不是json")
        assert False
    except PolicyError:
        pass
    # extra 字段禁止
    try:
        ActionRequest.from_model_json({"action": "file_list", "evil": 1})
        assert False
    except Exception:  # noqa: BLE001
        pass


# ======================================================================
# 2. 路径穿越 / 符号链接
# ======================================================================

def test_path_traversal_blocked():
    _base, ws = _workspace()
    gate = PolicyGate({"workspace_root": str(ws), "allow_files": True})
    ok_inside = gate.resolve_inside("a/b.txt")
    assert ok_inside.is_relative_to(ws.resolve())
    for bad in ("../out.txt", "..\\..\\Windows\\x", str(Path(tempfile.gettempdir()) / "x"),
                "a/../../out"):
        try:
            gate.resolve_inside(bad)
            assert False, f"穿越路径未拦: {bad}"
        except PolicyError:
            pass


def test_no_workspace_blocks_all_file_ops():
    gate = PolicyGate({"workspace_root": "", "allow_files": True})
    try:
        gate.resolve_inside("a.txt")
        assert False
    except PolicyError as e:
        assert "工作目录" in str(e)


def test_symlink_escape_blocked():
    _base, ws = _workspace()
    outside = _base.parent / ("out_" + next(tempfile._get_candidate_names()))  # type: ignore
    outside.mkdir(exist_ok=True)
    link = ws / "link_out"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        print("  [skip] 当前环境无创建符号链接权限")
        return
    # 为什么建完还要再确认一次：某些受限环境（沙箱/无 SeCreateSymbolicLink
    # 权限且被安全软件拦截）里 symlink_to 不抛异常却也没真的建出链接。
    # 链接不存在时路径本来就在工作目录内，闸门放行是正确行为，
    # 此时若继续断言"必须拦下"只会得到一条假失败。
    if not link.is_symlink():
        print("  [skip] 符号链接未真正创建，无法验证逃逸拦截")
        return
    gate = PolicyGate({"workspace_root": str(ws)})
    try:
        gate.resolve_inside("link_out/secret.txt")
        assert False, "符号链接逃逸未拦"
    except PolicyError:
        pass


def test_rename_new_name_cannot_contain_path():
    _base, ws = _workspace()
    ex = _executor(ws)
    for bad_name in ("../x", "a\\b", "a/b", ".."):
        req = ActionRequest(action=ActionKind.FILE_RENAME, target="a.txt",
                            args={"new_name": bad_name})
        res = ex.execute(req, _token(req))
        assert not res.ok, bad_name


# ======================================================================
# 3. 确认令牌
# ======================================================================

def test_token_expiry_match_once():
    req = ActionRequest(action=ActionKind.FILE_DELETE, target="a.txt")
    t = ConfirmationToken.issue(req, ttl=0.05)
    assert t.is_fresh() and t.matches(req)
    time.sleep(0.06)
    assert not t.is_fresh()
    # target 不同不匹配
    other = ActionRequest(action=ActionKind.FILE_DELETE, target="b.txt")
    assert not t.matches(other)
    # args 不同也不匹配（确认 A 不能执行 B）
    a1 = ActionRequest(action=ActionKind.FILE_COPY, target="a", args={"dest": "b"})
    a2 = ActionRequest(action=ActionKind.FILE_COPY, target="a", args={"dest": "c"})
    assert not ConfirmationToken.issue(a1).matches(a2)


def test_executor_requires_token_and_consumes_once():
    _get_qapp()
    _base, ws = _workspace()
    (ws / "d").mkdir()
    ex = _executor(ws)
    req = ActionRequest(action=ActionKind.FILE_LIST, target="d")
    # 无 token 拦
    assert not ex.execute(req).ok
    tok = _token(req)
    assert ex.execute(req, tok).ok
    # 用过一次再用必拦
    again = ex.execute(req, tok)
    assert not again.ok


def test_executor_rejects_stale_token():
    _get_qapp()
    _base, ws = _workspace()
    ex = _executor(ws)
    req = ActionRequest(action=ActionKind.FILE_LIST, target=".")
    stale = _token(req, ttl=0.02)
    time.sleep(0.03)                       # 先等令牌过期，再执行
    res = ex.execute(req, stale)
    assert not res.ok and "失效" in res.message


# ======================================================================
# 4. 文件操作 + 回收站 + 回滚
# ======================================================================

def test_file_list_preview_binary_mkdir():
    _get_qapp()
    _base, ws = _workspace()
    (ws / "t.txt").write_text("一行\n二行", encoding="utf-8")
    (ws / "bin.dat").write_bytes(b"\x00\x01\x02binary")
    ex = _executor(ws)

    r = ex.execute(ActionRequest(action=ActionKind.FILE_LIST, target="."),
                   _token(ActionRequest(action=ActionKind.FILE_LIST, target=".")))
    assert r.ok and r.data["count"] == 2

    pv = ActionRequest(action=ActionKind.FILE_PREVIEW, target="t.txt")
    r = ex.execute(pv, _token(pv)); assert r.ok and "一行" in r.data["text"]

    bad = ActionRequest(action=ActionKind.FILE_PREVIEW, target="bin.dat")
    r = ex.execute(bad, _token(bad)); assert not r.ok and "二进制" in r.message

    mk = ActionRequest(action=ActionKind.FILE_MKDIR, target="new/sub")
    r = ex.execute(mk, _token(mk)); assert r.ok and (ws / "new" / "sub").is_dir()


def test_file_rename_and_rollback():
    _get_qapp()
    _base, ws = _workspace()
    f = ws / "old.txt"; f.write_text("x", encoding="utf-8")
    ex = _executor(ws)
    rn = ActionRequest(action=ActionKind.FILE_RENAME, target="old.txt",
                       args={"new_name": "new.txt"})
    r = ex.execute(rn, _token(rn))
    assert r.ok and (ws / "new.txt").exists() and not f.exists()
    # 用返回的 rollback 撤销
    back = ex.files.undo(r.rollback)
    assert back.ok and f.exists() and not (ws / "new.txt").exists()


def test_file_move_and_rollback():
    _get_qapp()
    _base, ws = _workspace()
    (ws / "a").mkdir(); f = ws / "a" / "f.txt"; f.write_text("m")
    ex = _executor(ws)
    mv = ActionRequest(action=ActionKind.FILE_MOVE, target="a/f.txt", args={"dest": "f2.txt"})
    r = ex.execute(mv, _token(mv))
    assert r.ok and (ws / "f2.txt").exists() and not f.exists()
    assert ex.files.undo(r.rollback).ok and f.exists()


def test_file_copy_refuses_overwrite():
    _get_qapp()
    _base, ws = _workspace()
    (ws / "s.txt").write_text("s")
    (ws / "d.txt").write_text("d")
    ex = _executor(ws)
    cp = ActionRequest(action=ActionKind.FILE_COPY, target="s.txt", args={"dest": "d.txt"})
    r = ex.execute(cp, _token(cp))
    assert not r.ok and "已存在" in r.message
    assert (ws / "d.txt").read_text() == "d"     # 目标没被覆盖


def test_file_delete_goes_to_recycle_bin():
    _get_qapp()
    _base, ws = _workspace()
    f = ws / "gone.txt"; f.write_text("bye")
    ex = _executor(ws)
    dl = ActionRequest(action=ActionKind.FILE_DELETE, target="gone.txt")
    r = ex.execute(dl, _token(dl))
    assert r.ok and r.reversible and r.rollback.get("op") == "trash"
    assert not f.exists()                        # 已从工作目录移走（进回收站）


# ======================================================================
# 5. 进程白名单 / 受保护 / 只能杀自己启动的
# ======================================================================

class _FakeProc:
    def __init__(self, pid, name, owner): self._pid, self._n, self._o = pid, name, owner
    def name(self): return self._n
    def username(self): return self._o
    def terminate(self): self.terminated = True


class _FakePopen:
    def __init__(self, pid): self.pid = pid


def test_process_start_whitelist_and_protected():
    gate = PolicyGate({"allow_processes": True, "process_whitelist": ["notepad"]})
    ph = ProcessHands(gate)
    ph._popen = lambda argv: _FakePopen(1234)
    # 归一化匹配
    r = ph.start(" Notepad.EXE "); assert r.ok and r.data["pid"] == 1234
    # 非白名单拒
    assert not ph.start("cmd").ok
    # 找不到程序
    gate.process_whitelist = {"definitely_not_exist_xyz"}
    assert not ph.start("definitely_not_exist_xyz").ok
    # 受保护进程即使在白名单也拒
    gate.process_whitelist = set(PROTECTED_PROCESSES)
    assert not ph.start("explorer").ok


def test_process_kill_only_self_spawned():
    gate = PolicyGate({"allow_processes": True, "process_whitelist": ["notepad"]})
    ph = ProcessHands(gate)
    # 不是自己启动的 PID 一律拒
    r = ph.kill(99999); assert not r.ok and "不是她启动" in r.message
    # 自己启动的、且进程安全才允许 terminate
    ph._spawned[4321] = "notepad"
    fake = _FakeProc(4321, "notepad.exe", "dell\\user")
    ph._psutil_process = lambda pid: fake
    ProcessHands._current_user = staticmethod(lambda: "dell\\user")
    r = ph.kill(4321); assert r.ok and getattr(fake, "terminated", False)
    # 受保护进程拒
    ph._spawned[1] = "csrss"
    ph._psutil_process = lambda pid: _FakeProc(pid, "csrss.exe", "dell\\user")
    assert not ph.kill(1).ok
    # 别的用户（SYSTEM）拒
    ph._spawned[2] = "x"
    ph._psutil_process = lambda pid: _FakeProc(pid, "x.exe", "nt authority\\system")
    assert not ph.kill(2).ok


# ======================================================================
# 6. 剪贴板快照/恢复（替身，不碰真实剪贴板）
# ======================================================================

def test_clipboard_snapshot_restore():
    cb = ClipboardHands()
    box = {"v": "原始内容"}
    cb._read = lambda: box["v"]
    written = []
    cb._write = lambda t: written.append(t) or box.__setitem__("v", t)
    r = cb.read(); assert r.ok and "原始内容" in r.data["text"]
    w = cb.write("新内容"); assert w.ok
    assert box["v"] == "新内容"                      # 已覆盖
    r = cb.restore(); assert r.ok and box["v"] == "原始内容"  # 快照恢复
    assert cb.restore().ok is False                 # 没更多快照


# ======================================================================
# 7. 审计脱敏
# ======================================================================

def test_audit_redacts_clipboard_and_persists():
    _base, ws = _workspace()
    ex = _executor(ws)
    ex.clipboard._read = lambda: "old"
    ex.clipboard._write = lambda t: None
    secret = "这是剪贴板秘密S3CRET"
    req = ActionRequest(action=ActionKind.CLIP_WRITE, args={"content": secret})
    r = ex.execute(req, _token(req)); assert r.ok
    log = ex.audit.recent()
    raw = json.dumps(log, ensure_ascii=False)
    assert secret not in raw                        # 正文绝不落日志
    entry = [e for e in log if e["action"] == ActionKind.CLIP_WRITE][-1]
    assert entry["target"] == "<剪贴板>" and entry["clip_chars"] == len(secret)
    # 落盘可重新读回：用同一 root 新建审计器应能读到刚才的记录
    from hands.audit import AuditLogger
    reloaded = AuditLogger(_base).recent()
    assert any(e["action"] == ActionKind.CLIP_WRITE for e in reloaded)


def test_audit_blocked_stage_recorded():
    _base, ws = _workspace()
    ex = _executor(ws)
    bad = ActionRequest(action=ActionKind.FILE_DELETE, target="../x")
    ex.execute(bad, _token(bad))
    stages = [e["stage"] for e in ex.audit.recent()]
    assert "blocked" in stages


# ======================================================================
# 8. planner 容错与能力过滤
# ======================================================================

def test_planner_parse_and_capability_filter():
    cfg = {"allow_files": True, "allow_processes": False}
    ok = planner_mod.parse_action(
        '{"action":"file_delete","target":"a","reason":"x"}', cfg)
    assert ok and ok.action == ActionKind.FILE_DELETE
    # none / 垃圾 / 能力关闭 / 危险动作 都返回 None
    assert planner_mod.parse_action('{"action":"none"}', cfg) is None
    assert planner_mod.parse_action("哈哈哈", cfg) is None
    assert planner_mod.parse_action('{"action":"proc_start","target":"x"}', cfg) is None
    assert planner_mod.parse_action('{"action":"run_shell"}', cfg) is None
    # markdown 包裹也能抠出来
    assert planner_mod.extract_json('```json\n{"action":"none"}\n```') == {"action": "none"}


def test_planner_system_prompt_lists_only_allowed():
    sys_p = planner_mod.build_plan_system({"allow_files": True})
    assert "file_list" in sys_p and "proc_start" not in sys_p
    # 全关时可用动作为空
    assert planner_mod.available_actions({"allow_files": False, "allow_clipboard": False,
                                          "allow_windows": False,
                                          "allow_processes": False}) == set()


# ======================================================================
# 9. 能力开关
# ======================================================================

def test_planner_local_gate_classifies():
    # 明显操作意图命中
    for t in ("帮我新建一个文件夹", "把记事本打开", "复制这个文件到D盘",
              "最小化当前窗口", "看看剪贴板里有什么"):
        assert planner_mod.looks_like_action(t), t
    # 普通聊天/情绪倾诉不命中
    for t in ("你好呀", "今天好累安慰我", "你觉得我该怎么办", "讲个笑话", "我喜欢你"):
        assert not planner_mod.looks_like_action(t), t


def test_planner_plan_skips_llm_for_plain_chat():
    """普通聊天必须在本地 gate 就返回，绝不调用规划模型（省额度的核心）。"""
    import asyncio as _aio
    calls = []

    async def fake_llm(sys_p, user):
        calls.append(user)
        return '{"action":"none"}'

    plan = planner_mod.ToolPlanner(lambda: {"allow_files": True}, lambda: ".")
    # 聊天：不调 LLM
    r1 = _aio.run(plan.plan(fake_llm, "今天好累，安慰我一下"))
    assert r1 is None and calls == []
    # 操作：才调 LLM，并正常解析
    async def fake_llm2(sys_p, user):
        calls.append(user)
        return '{"action":"file_mkdir","target":"demo","reason":"x"}'
    r2 = _aio.run(plan.plan(fake_llm2, "帮我在当前目录新建一个文件夹叫demo"))
    assert r2 is not None and r2.action == ActionKind.FILE_MKDIR
    assert len(calls) == 1


def test_capability_switch_blocks_category():
    _base, ws = _workspace()
    ex = _executor(ws, allow_files=False)
    req = ActionRequest(action=ActionKind.FILE_LIST, target=".")
    r = ex.execute(req, _token(req))
    assert not r.ok and "关闭" in r.message


# ----------------------------------------------------------------------
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
