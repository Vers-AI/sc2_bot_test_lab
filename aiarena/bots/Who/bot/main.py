import os
import sys
from typing import Optional, Any

import importlib
from ares import AresBot
from sc2.data import Race

RACE_TO_BOT_NAME = {
    Race.Protoss: "really",
    Race.Terran: "why",
    Race.Zerg: "what",
}


class MyBot(AresBot):
    def __init__(self, game_step_override: Optional[int] = None):
        """Initiate custom bot

        Parameters
        ----------
        game_step_override :
            If provided, set the game_step to this value regardless of how it was
            specified elsewhere
        """
        super().__init__(game_step_override)

        self.opening_handler: Optional[Any] = None
        self.opening_chat_tag: bool = False
        self._switched_to_prevent_tie: bool = False
        self.injured_general_unit_to_repairing_scvs: dict[int, set[int]] = dict()
        self._terran_bunker_finder_activated: bool = False
        self._dino_tag: bool = False
        self._on_gas: bool = True
        self._switched_due_to_worker_rush: bool = False

    async def on_step(self, iteration: int) -> None:
        await super(MyBot, self).on_step(iteration)

    async def on_start(self) -> None:
        bot_name: str = RACE_TO_BOT_NAME[self.race]
        path = os.path.join(os.path.dirname(__file__), bot_name)
        if path not in sys.path:
            sys.path.insert(0, path)

        module_path = f"bot.{bot_name}.bot.main"

        module = importlib.import_module(module_path)
        MyBot = getattr(module, "MyBot")

        self.__class__ = MyBot

        await self.on_start()


    """
    Can use `python-sc2` hooks as usual, but make a call the inherited method in the superclass
    Examples:
    """
    # async def on_start(self) -> None:
    #     await super(MyBot, self).on_start()
    #
    #     # on_start logic here ...
    #
    # async def on_end(self, game_result: Result) -> None:
    #     await super(MyBot, self).on_end(game_result)
    #
    #     # custom on_end logic here ...
    #
    # async def on_building_construction_complete(self, unit: Unit) -> None:
    #     await super(MyBot, self).on_building_construction_complete(unit)
    #
    #     # custom on_building_construction_complete logic here ...
    #
    # async def on_unit_created(self, unit: Unit) -> None:
    #     await super(MyBot, self).on_unit_created(unit)
    #
    #     # custom on_unit_created logic here ...
    #
    # async def on_unit_destroyed(self, unit_tag: int) -> None:
    #     await super(MyBot, self).on_unit_destroyed(unit_tag)
    #
    #     # custom on_unit_destroyed logic here ...
    #
    # async def on_unit_took_damage(self, unit: Unit, amount_damage_taken: float) -> None:
    #     await super(MyBot, self).on_unit_took_damage(unit, amount_damage_taken)
    #
    #     # custom on_unit_took_damage logic here ...
