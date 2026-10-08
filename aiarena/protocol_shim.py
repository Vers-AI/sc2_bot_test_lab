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

import aiohttp
from aiohttp import web, WSMsgType
from s2clientprotocol import sc2api_pb2 as pb

LISTEN_PORT = int(os.environ.get("SHIM_LISTEN_PORT", "8080"))
PROXY_URL = os.environ.get("SHIM_PROXY_URL", "ws://proxy_controller:8080/sc2api")
BOT_NAME = os.environ.get("SHIM_BOT_NAME", "Bot")


async def _bot_to_proxy(bot_ws, proxy_ws):
    """Relay bot -> proxy: answer pings locally, inject name on the join."""
    intercepted = False
    async for msg in bot_ws:
        if msg.type == WSMsgType.BINARY:
            data = bytes(msg.data)
            if not intercepted:
                req = pb.Request()
                try:
                    req.ParseFromString(data)
                except Exception:
                    kind = None
                else:
                    kind = req.WhichOneof("request")
                if kind == "ping":
                    # Byte-echo: the client parses this as an empty
                    # ResponsePing and proceeds (proven 2026-10-08).
                    await bot_ws.send_bytes(data)
                    continue
                if kind == "join_game":
                    req.join_game.player_name = BOT_NAME
                    data = req.SerializeToString()
                intercepted = True
            await proxy_ws.send_bytes(data)
        elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
            break


async def _proxy_to_bot(bot_ws, proxy_ws):
    """Relay proxy -> bot: fully transparent."""
    async for msg in proxy_ws:
        if msg.type == WSMsgType.BINARY:
            await bot_ws.send_bytes(bytes(msg.data))
        elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
            break


async def handle(request):
    bot_ws = web.WebSocketResponse(autoping=True)
    await bot_ws.prepare(request)
    session = aiohttp.ClientSession()
    try:
        proxy_ws = await session.ws_connect(PROXY_URL)
    except Exception:
        await bot_ws.close()
        await session.close()
        return bot_ws
    t1 = asyncio.create_task(_bot_to_proxy(bot_ws, proxy_ws))
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
