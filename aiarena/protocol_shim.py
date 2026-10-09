#!/usr/bin/env python3
"""SC2API protocol shim for Churchill-era compiled bots (MicroMachine).

Architecture (forensics, matches 932-958, 2026-10-08):

* Bots connect to the bot_controller's relay listener on port 8080 (the
  --LadderServer argument points at the bot_controller container). The
  relay performs player identification with the proxy. Direct connections
  to proxy_controller:8080 are accepted at socket level but NEVER
  identified: 60s timeout, zero game steps — proven even with
  byte-identical known-good python joins through a pure relay pipe
  (matches 955-957).
* The relay binds port 8080 during start_bot processing (~+2-3s into
  the match). Churchill-era bots hardwire 127.0.0.1:8080 and connect at
  ~+0.1s — before the relay exists.
* Churchill clients also open with a RequestPing and BLOCK until it is
  answered. Nothing on the designed path answers pre-identification
  pings (match 958: bot reached the real relay; ping still unanswered).

This shim therefore:
  1. Binds 127.0.0.1:8080 IMMEDIATELY at container start, winning the
     race against the controller's relay bind. (v5 lost this race: the
     entrypoint's $(hostname -i) command substitution delayed the shim
     past the controller's bind -> EADDRINUSE, match 958. All target
     resolution now happens lazily AFTER binding.)
  2. Answers pre-join Request.ping locally via byte-echo (unblocks the
     Churchill client; proven against the MicroMachine binary).
  3. Injects SHIM_BOT_NAME into the first JoinGame by appending the
     player_name field inside the join submessage at the byte level —
     the bot's frame is never re-serialized.
  4. Relays everything else to the bot_controller's OWN relay on the
     container's eth0 address (resolved lazily), retrying until the
     relay comes up — identification then happens exactly as designed.

Env: SHIM_LISTEN_PORT (8080), SHIM_TARGET_PORT (8080), SHIM_BOT_NAME,
     SHIM_CONNECT_RETRIES (40), SHIM_RETRY_DELAY (0.5), SHIM_JOIN_DELAY (2)
"""
import asyncio
import os
import socket

import aiohttp
from aiohttp import web, WSMsgType
from s2clientprotocol import sc2api_pb2 as pb

LISTEN_PORT = int(os.environ.get("SHIM_LISTEN_PORT", "8080"))
TARGET_PORT = int(os.environ.get("SHIM_TARGET_PORT", "8080"))
# Connect to the proxy BY RESOLVED IP, not DNS name: direct bots
# connect to ws://<proxy-ip>:8080 (from --LadderServer), so their
# HTTP upgrade carries Host: <ip>:8080. A DNS-name connection
# sends Host: proxy_controller:8080 — and the proxy never
# identifies those connections (matches 932-960: every
# DNS-named shim upstream was accepted but never identified,
# while every direct IP connection identified in ~1s).
TARGET_HOST = os.environ.get("SHIM_TARGET_HOST", "proxy_controller")
BOT_NAME = os.environ.get("SHIM_BOT_NAME", "Bot")
CONNECT_RETRIES = int(os.environ.get("SHIM_CONNECT_RETRIES", "40"))
RETRY_DELAY = float(os.environ.get("SHIM_RETRY_DELAY", "0.5"))
JOIN_DELAY = float(os.environ.get("SHIM_JOIN_DELAY", "2"))


def _varint(n: int) -> bytes:
    out = b""
    while n > 0x7F:
        out += bytes([(n & 0x7F) | 0x80])
        n >>= 7
    return out + bytes([n & 0x7F])


def _log(tag, msg):
    print(f"[shim] {tag}: {msg}", flush=True)


def _own_ip() -> str:
    """This container's non-loopback IPv4 (docker resolves our hostname)."""
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"
        finally:
            s.close()


async def _connect_with_retry(session):
    """Connect upstream to the PROXY, by resolved IP (Host-header match).

    The proxy listens on 0.0.0.0:8080 from container start. Direct bots
    connect by IP (from --LadderServer), producing Host: <ip>:8080 in the
    HTTP upgrade. Resolve the DNS name to the same IP and connect by IP
    so the shim's upgrade is indistinguishable from a direct bot's.
    """
    try:
        ip = socket.gethostbyname(TARGET_HOST)
    except Exception as exc:
        _log("conn", f"DNS resolve of {TARGET_HOST} failed: {exc!r}")
        raise
    url = f"ws://{ip}:{TARGET_PORT}/sc2api"
    _log("conn", f"{TARGET_HOST} -> {ip}; upstream {url}")
    for attempt in range(1, CONNECT_RETRIES + 1):
        try:
            ws = await session.ws_connect(url)
            _log("conn", f"upstream established (attempt {attempt})")
            return ws
        except Exception as exc:
            if attempt == 1 or attempt % 10 == 0:
                _log("conn", f"attempt {attempt}/{CONNECT_RETRIES}: {exc!r}")
            await asyncio.sleep(RETRY_DELAY)
    raise ConnectionError(f"upstream {url} never came up")


async def _bot_to_upstream(bot_ws, up_ws, connected_at):
    """Relay bot -> upstream: answer pings locally, inject name on join."""
    intercepted = False
    async for msg in bot_ws:
        if msg.type == WSMsgType.BINARY:
            data = bytes(msg.data)
            if not intercepted:
                try:
                    req = pb.Request()
                    req.ParseFromString(data)
                    kind = req.WhichOneof("request")
                except Exception:
                    kind = None
                if kind == "ping":
                    # Byte-echo: the client parses this as an empty
                    # ResponsePing and proceeds (proven 2026-10-08).
                    await bot_ws.send_bytes(data)
                    _log("bot->", "echoed ping locally")
                    continue
                if kind == "join_game":
                    # Byte-level name injection INSIDE the join
                    # submessage (field 7, tag 0x3A); rebuild the outer
                    # length. Never re-serialize the bot's frame.
                    raw = data
                    if raw and raw[0] == 0x12:
                        idx, length, shift = 1, 0, 0
                        while True:
                            b = raw[idx]; idx += 1
                            length |= (b & 0x7F) << shift; shift += 7
                            if not (b & 0x80):
                                break
                        sub = raw[idx:]
                        name_bytes = BOT_NAME.encode("utf-8")
                        sub = sub + bytes([0x3A]) + _varint(len(name_bytes)) + name_bytes
                        data = bytes([0x12]) + _varint(len(sub)) + sub
                        _log("bot->", f"name injected: {len(raw)}B -> {len(data)}B")
                    elapsed = asyncio.get_event_loop().time() - connected_at
                    if elapsed < JOIN_DELAY:
                        hold = JOIN_DELAY - elapsed
                        _log("bot->", f"holding join {hold:.1f}s")
                        await asyncio.sleep(hold)
                intercepted = True
            await up_ws.send_bytes(data)
            _log("bot->", f"relayed {len(data)}B")
        elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
            break


async def _upstream_to_bot(bot_ws, up_ws):
    """Relay upstream -> bot: fully transparent."""
    async for msg in up_ws:
        if msg.type == WSMsgType.BINARY:
            await bot_ws.send_bytes(bytes(msg.data))
        elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
            break


async def handle(request):
    bot_ws = web.WebSocketResponse(autoping=True)
    await bot_ws.prepare(request)
    connected_at = asyncio.get_event_loop().time()
    session = aiohttp.ClientSession()
    _log("conn", "bot connected; resolving relay target")
    try:
        up_ws = await _connect_with_retry(session)
    except Exception as exc:
        _log("conn", f"giving up: {exc!r}")
        await bot_ws.close()
        await session.close()
        return bot_ws
    t1 = asyncio.create_task(_bot_to_upstream(bot_ws, up_ws, connected_at))
    t2 = asyncio.create_task(_upstream_to_bot(bot_ws, up_ws))
    _, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    for closer in (up_ws.close(), bot_ws.close(), session.close()):
        try:
            await closer
        except Exception:
            pass
    return bot_ws


def main():
    app = web.Application()
    app.router.add_get("/{tail:.*}", handle)
    web.run_app(app, host="127.0.0.1", port=LISTEN_PORT, print=None)


if __name__ == "__main__":
    main()
