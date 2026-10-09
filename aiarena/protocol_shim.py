#!/usr/bin/env python3
"""SC2API protocol shim for Churchill-era compiled bots (e.g. MicroMachine).

Those bots (a) hardwire localhost:<port> for the ladder connection and
(b) open their ladder session with a RequestPing, blocking until it is
answered. The aiarena proxy does not answer pre-join pings (it waits
for the JoinGame request to identify and register the player), so the
bot deadlocks and the match times out with zero game steps.

This shim sits between the bot and the proxy:

  * listens where the bot connects (default localhost:8080)
  * connects upstream to the real proxy per bot connection
  * answers every Request{ping} locally by echoing the request bytes
    back (the client accepts an empty ResponsePing - proven against
    the MicroMachine 1.18.2 binary, 2026-10-08)
  * on the first non-ping request (the JoinGame), injects
    player_name = SHIM_BOT_NAME so the proxy can identify the player
  * everything else: transparent bidirectional relay

Env:
  SHIM_LISTEN_PORT  port the bot connects to (default 8080)
  SHIM_PROXY_URL    upstream proxy websocket (default ws://proxy_controller:8080/sc2api)
  SHIM_BOT_NAME     player name injected into the join request (default "Bot")
"""
import asyncio
import os
import time

import aiohttp
from aiohttp import web, WSMsgType
from s2clientprotocol import sc2api_pb2 as pb

LISTEN_PORT = int(os.environ.get("SHIM_LISTEN_PORT", "8080"))
PROXY_URL = os.environ.get("SHIM_PROXY_URL", "ws://proxy_controller:8080/sc2api")
BOT_NAME = os.environ.get("SHIM_BOT_NAME", "Bot")
# Hold the first JoinGame relay for this many seconds after the
# bot connects: compiled bots send their join ~0.1s after start,
# while the SC2 process the proxy connects to on their behalf is
# still booting (~5-6s). A join relayed too early is silently
# dropped by the proxy and the bot waits forever (proven: matches
# 944-947, instrumented trace 2026-10-08). Python bots never hit
# this because interpreter startup delays their join naturally.
JOIN_DELAY = float(os.environ.get("SHIM_JOIN_DELAY", "10"))


def _log(tag, msg):
    print(f"[shim] {tag}: {msg}", flush=True)


async def _bot_to_proxy(bot_ws, proxy_ws, connected_at):
    """Relay bot -> proxy: answer pings locally, inject name on the join."""
    intercepted = False
    async for msg in bot_ws:
        _log("bot->", f"msg type={msg.type} len={len(msg.data) if msg.data else 0}")
        if msg.type == WSMsgType.BINARY:
            data = bytes(msg.data)
            if not intercepted:
                req = pb.Request()
                try:
                    req.ParseFromString(data)
                    kind = req.WhichOneof("request")
                    _log("bot->", f"parsed kind={kind}")
                except Exception as exc:
                    kind = None
                    _log("bot->", f"PARSE FAILED: {exc!r}")
                if kind == "ping":
                    # Byte-echo: the client parses this as an empty
                    # ResponsePing and proceeds (proven 2026-10-08).
                    await bot_ws.send_bytes(data)
                    _log("bot->", "echoed ping locally")
                    continue
                if kind == "join_game":
                    req.join_game.player_name = BOT_NAME
                    # Port-scheme translation (proven root cause, matches
                    # 944-948): Churchill-era bots derive ports as
                    #   shared=S, server=[S+1, S+2], client=[[S+3, S+4]]
                    # while the lab's python bots (and thus the SC2
                    # pair) expect
                    #   server=[S+2, S+3], client=[[S+4, S+5]], no shared.
                    # The schemes overlap (S+2/S+3 claimed by both sides
                    # in different roles), the SC2 game link dies, and
                    # the join is never answered. Rewrite Churchill joins
                    # (signature: shared_port != 0) to the python scheme.
                    data = req.SerializeToString()
                    elapsed = time.monotonic() - connected_at
                    if elapsed < JOIN_DELAY:
                        hold = JOIN_DELAY - elapsed
                        _log("bot->", f"holding join {hold:.1f}s (SC2 boot race)")
                        await asyncio.sleep(hold)
                    _log("bot->", f"name injected, relaying join {len(data)}B")
                intercepted = True
            try:
                await proxy_ws.send_bytes(data)
                _log("bot->", f"relayed {len(data)}B upstream")
            except Exception as exc:
                _log("bot->", f"UPSTREAM SEND FAILED: {exc!r}")
                raise
        elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
            break


async def _proxy_to_bot(bot_ws, proxy_ws):
    """Relay proxy -> bot: fully transparent."""
    async for msg in proxy_ws:
        _log("px->", f"msg type={msg.type} len={len(msg.data) if msg.data else 0}")
        if msg.type == WSMsgType.BINARY:
            try:
                await bot_ws.send_bytes(bytes(msg.data))
            except Exception as exc:
                _log("px->", f"BOT SEND FAILED: {exc!r}")
                raise
        elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
            break


async def handle(request):
    bot_ws = web.WebSocketResponse(autoping=True)
    await bot_ws.prepare(request)
    session = aiohttp.ClientSession()
    _log("conn", f"bot connected; opening upstream {PROXY_URL}")
    try:
        proxy_ws = await session.ws_connect(PROXY_URL)
        _log("conn", "upstream established")
    except Exception as exc:
        await bot_ws.close()
        await session.close()
        return bot_ws
    t1 = asyncio.create_task(_bot_to_proxy(bot_ws, proxy_ws, time.monotonic()))
    t2 = asyncio.create_task(_proxy_to_bot(bot_ws, proxy_ws))
    _, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    for closer in (proxy_ws.close(), bot_ws.close(), session.close()):
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
