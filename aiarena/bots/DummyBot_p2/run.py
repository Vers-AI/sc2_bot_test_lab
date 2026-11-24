"""Self-contained ladder bot for the test lab (stock arenaclient-bot image).

Join logic adapted from Zozo's proven __init__.py (ran hundreds of matches on
arenaclient v0.8.0 / burnysc2 7.1.3). Uses the image's actual API names
(ConnectionAlreadyClosedError). No external deps beyond python-sc2.
"""
import argparse
import asyncio
import sys

import aiohttp
import sc2
import sc2.main
from sc2.bot_ai import BotAI
from sc2.client import Client
from sc2.data import Race
from sc2.player import Bot
from sc2.protocol import ConnectionAlreadyClosedError as ConnectionAlreadyClosed
from sc2.portconfig import Portconfig


class DummyBotAI(BotAI):
    async def on_step(self, iteration: int) -> None:
        target = self.game_info.map_center
        if self.enemy_start_locations:
            target = self.enemy_start_locations[0]
        elif self.enemy_structures:
            target = self.enemy_structures.first.position
        for unit in self.units.idle:
            unit.attack(target)


def run_ladder_game(bot):
    parser = argparse.ArgumentParser()
    parser.add_argument("--GamePort", type=int, nargs="?")
    parser.add_argument("--StartPort", type=int, nargs="?")
    parser.add_argument("--LadderServer", type=str, nargs="?")
    parser.add_argument("--OpponentId", type=str, nargs="?")
    parser.add_argument("--RealTime", action="store_true")
    args, _ = parser.parse_known_args()

    host = args.LadderServer or "127.0.0.1"
    lan_port = args.StartPort

    ports = [lan_port + p for p in range(1, 6)]
    portconfig = Portconfig()
    portconfig.server = [ports[1], ports[2]]
    portconfig.players = [[ports[3], ports[4]]]

    g = join_ladder_game(host=host, port=args.GamePort, players=[bot],
                         realtime=args.RealTime, portconfig=portconfig)
    result = asyncio.get_event_loop().run_until_complete(g)
    return result, args.OpponentId


async def join_ladder_game(host, port, players, realtime, portconfig):
    ws_url = "ws://{}:{}/sc2api".format(host, port)
    ws_connection = await aiohttp.ClientSession().ws_connect(ws_url, timeout=120)
    client = Client(ws_connection)
    try:
        result = await sc2.main._play_game(players[0], client, realtime, portconfig)
        return result
    except ConnectionAlreadyClosed:
        print("Connection closed before game ended", file=sys.stderr)
        return None


def main():
    if "--LadderServer" in sys.argv:
        print("Starting ladder game (DummyBot)...")
        result, opponentid = run_ladder_game(
            Bot(Race.Terran, DummyBotAI(), "DummyBot")
        )
        print(result, " against opponent ", opponentid)
    else:
        print("Use --LadderServer (lab mode)", file=sys.stderr)


if __name__ == "__main__":
    main()
