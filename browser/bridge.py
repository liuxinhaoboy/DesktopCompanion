# -*- coding: utf-8 -*-
"""
bridge —— 只监听 127.0.0.1 的 WebSocket 本机桥（P6）
==================================================
桌宠是 server，浏览器扩展是唯一 client。职责：
  - 生成一次性配对码、校验握手、发/记长期会话 token（支持断线重连）
  - 把一条浏览器指令下发给扩展，并等它回 result（cmd_id 对号，超时失败）
  - 心跳保活、断开感知、主动 unpair

★ 安全：host 写死 127.0.0.1，绝不绑 0.0.0.0（不开放局域网/公网）；
  每条非握手消息都要带对当前会话 token，不对就立刻断开。
桥本身不判断动作安不安全——那是 policy 的事；桥只负责"连得上、传得对"。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Awaitable, Callable

import websockets

from .protocol import (PROTOCOL_VERSION, HelloMessage, ResultMessage,
                       generate_cmd_id, generate_pair_code,
                       generate_session_token, make_command, make_hello_ack,
                       make_pong, normalize_pair_code, parse_inbound)

# 本机回环地址，写死不允许配置成别的（红线）
HOST = "127.0.0.1"
DEFAULT_PORT = 8731
PAIR_CODE_TTL = 300        # 一次性配对码 5 分钟有效
COMMAND_TIMEOUT = 20.0     # 等扩展执行结果最多 20 秒


class BridgeState:
    STOPPED = "stopped"        # 没在监听
    WAITING = "waiting"        # 在监听、等扩展来配对
    CONNECTED = "connected"    # 已和扩展连上


class BrowserBridge:
    def __init__(self, cfg_getter: Callable[[], dict],
                 on_state: Callable[[str], None] | None = None):
        self._cfg_getter = cfg_getter
        self._on_state = on_state
        self._server = None
        self._ws = None                  # 当前已配对连接
        self._session = ""               # 当前会话 token
        self._pair_code = ""             # 待配对的一次性码（normalize 后）
        self._pair_expire = 0.0
        self.port = DEFAULT_PORT
        self.state = BridgeState.STOPPED
        # cmd_id -> Future，等扩展回 result
        self._pending: dict[str, asyncio.Future] = {}

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def _set_state(self, st: str) -> None:
        if st != self.state:
            self.state = st
            if self._on_state:
                try:
                    self._on_state(st)
                except Exception:  # noqa: BLE001 —— UI 回调不能拖垮桥
                    pass

    @property
    def is_connected(self) -> bool:
        return self.state == BridgeState.CONNECTED and self._ws is not None

    def status_text(self) -> str:
        return {
            BridgeState.STOPPED: "● 未开启",
            BridgeState.WAITING: "● 等待浏览器配对…",
            BridgeState.CONNECTED: "● 已连接",
        }.get(self.state, self.state)

    # ------------------------------------------------------------------
    # 配对 / 监听
    # ------------------------------------------------------------------
    async def start_pairing(self) -> str:
        """开始监听并生成一次性配对码，返回给设置页展示。"""
        cfg = self._cfg_getter() or {}
        self.port = int(cfg.get("port", DEFAULT_PORT))
        if self._server is None:
            # 写死回环地址；端口被占用就尝试往后找 3 个
            last_err = None
            for try_port in range(self.port, self.port + 4):
                try:
                    self._server = await websockets.serve(
                        self._handler, HOST, try_port)
                    self.port = try_port
                    break
                except OSError as e:  # 端口占用
                    last_err = e
            if self._server is None:
                raise RuntimeError(f"本机端口被占用，无法启动桥：{last_err}")
        self._pair_code = normalize_pair_code(generate_pair_code())
        self._pair_expire = time.time() + PAIR_CODE_TTL
        self._set_state(BridgeState.WAITING if not self.is_connected
                        else BridgeState.CONNECTED)
        # 还原成带横杠的好读形式展示
        raw = self._pair_code
        pretty = "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))
        return pretty

    async def unpair(self) -> None:
        """断开扩展并停掉监听（设置页"断开"）。"""
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._ws = None
        self._session = ""
        self._pair_code = ""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._set_state(BridgeState.STOPPED)

    async def shutdown(self) -> None:
        """程序退出时清理（等同 unpair，但不依赖 UI）。"""
        await self.unpair()

    # ------------------------------------------------------------------
    # 连接处理
    # ------------------------------------------------------------------
    async def _handler(self, ws) -> None:
        try:
            async for raw in ws:
                try:
                    msg = parse_inbound(raw)
                except Exception as e:  # noqa: BLE001 —— 协议错直接断开
                    await ws.close(code=1008, reason=str(e)[:60])
                    return
                kind = type(msg).__name__
                if kind == "HelloMessage":
                    await self._handle_hello(ws, msg)
                elif kind == "PingMessage":
                    if msg.session != self._session or ws is not self._ws:
                        await ws.close(code=1008, reason="bad session")
                        return
                    await ws.send(json.dumps(make_pong()))
                elif kind == "ResultMessage":
                    if msg.session != self._session or ws is not self._ws:
                        await ws.close(code=1008, reason="bad session")
                        return
                    self._resolve_result(msg)
        except Exception:  # noqa: BLE001 —— 连接异常断开，走清理
            pass
        finally:
            # 当前连接掉了：回到"监听中、等待重连"，server 保留
            if ws is self._ws:
                self._ws = None
                if self._server is not None:
                    self._set_state(BridgeState.WAITING)

    async def _handle_hello(self, ws, msg: HelloMessage) -> None:
        # 1) 已配对扩展断线重连：带对 session 就接回
        if msg.is_reconnect():
            if self._session and msg.session == self._session:
                self._ws = ws
                self._set_state(BridgeState.CONNECTED)
                await ws.send(json.dumps(make_hello_ack(True, self._session)))
                return
            await ws.send(json.dumps(
                make_hello_ack(False, reason="会话已失效，请重新生成配对码")))
            await ws.close(code=1008, reason="bad session")
            return

        # 2) 首次配对：校验一次性码
        if not self._pair_code or time.time() > self._pair_expire:
            await ws.send(json.dumps(
                make_hello_ack(False, reason="配对码不存在或已过期，请重新生成")))
            await ws.close(code=1008, reason="pair expired")
            return
        if msg.pair_code != self._pair_code:
            await ws.send(json.dumps(make_hello_ack(False, reason="配对码不正确")))
            await ws.close(code=1008, reason="bad pair code")
            return
        # 版本兼容检查
        if msg.ext_version < "1.0":
            await ws.send(json.dumps(make_hello_ack(False, reason="扩展版本过低，请更新")))
            await ws.close(code=1008, reason="version")
            return

        # 配对成功：踢掉旧连接，认这个新连接
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._ws = ws
        self._session = generate_session_token()
        self._pair_code = ""                 # 一次性，用完即焚
        self._set_state(BridgeState.CONNECTED)
        await ws.send(json.dumps(make_hello_ack(True, self._session)))

    def _resolve_result(self, msg: ResultMessage) -> None:
        fut = self._pending.pop(msg.cmd_id, None)
        if fut is None or fut.done():
            return
        fut.set_result({"ok": msg.ok, "data": msg.data, "error": msg.error})

    # ------------------------------------------------------------------
    # 下发指令
    # ------------------------------------------------------------------
    async def send_command(self, action: str, target: str = "",
                           args: dict | None = None,
                           timeout: float = COMMAND_TIMEOUT) -> dict:
        """把一条指令发给已连接扩展并等结果。没连上直接报错（不静默降级）。"""
        if not self.is_connected:
            return {"ok": False, "data": {},
                    "error": "浏览器还没连接，请先在设置里配对扩展"}
        cmd_id = generate_cmd_id()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[cmd_id] = fut
        pkt = make_command(self._session, cmd_id, action, target, args)
        try:
            await self._ws.send(json.dumps(pkt))
            res = await asyncio.wait_for(fut, timeout=timeout)
            return res
        except asyncio.TimeoutError:
            self._pending.pop(cmd_id, None)
            return {"ok": False, "data": {}, "error": "浏览器没有在规定时间内回应"}
        except Exception as e:  # noqa: BLE001
            self._pending.pop(cmd_id, None)
            return {"ok": False, "data": {}, "error": f"发送失败：{e}"}

    def session_token(self) -> str:
        return self._session
