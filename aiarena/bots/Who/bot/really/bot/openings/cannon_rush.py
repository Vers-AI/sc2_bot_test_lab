from ares import AresBot
from ares.behaviors.macro import AutoSupply, BuildWorkers, MacroPlan
from cython_extensions import cy_closest_to, cy_distance_to_squared
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2
from sc2.units import Units

from bot.combat.probe_proxy_builder import ProbeProxyBuilder
from bot.openings.opening_base import OpeningBase

# Classic cannon rush order: pylon -> forge -> cannons at the enemy
# natural, then more pylons and cannons as the rush takes hold.
# Modeled on PROXY_ZEALOT_PLAN (bot/consts.py).
CANNON_RUSH_PLAN: list[tuple[UnitTypeId, int]] = [
    (UnitTypeId.PYLON, 1),
    (UnitTypeId.FORGE, 1),
    (UnitTypeId.PHOTONCANNON, 2),
    (UnitTypeId.PYLON, 1),
    (UnitTypeId.PHOTONCANNON, 2),
    (UnitTypeId.PYLON, 1),
    (UnitTypeId.PHOTONCANNON, 2),
]


class CannonRush(OpeningBase):
    """Cannon rush at the enemy natural (test build; see who README:
    the bot exists so other bots can practice against gimmicks).

    Opening module for the ``CannonRush`` yml build. Modeled closely on
    ProxyZealot: a probe is assigned to the enemy natural and walks the
    CANNON_RUSH_PLAN (pylon, forge, cannons) via ProbeProxyBuilder.
    """

    probe_proxy_builder: ProbeProxyBuilder
    _proxy_complete: bool
    _proxy_location: Point2
    _max_proxy_workers: int
    _primary_builder_tag: int

    async def on_start(self, ai: AresBot) -> None:
        await super().on_start(ai)
        self.probe_proxy_builder = ProbeProxyBuilder(ai, ai.config, ai.mediator)
        self._max_proxy_workers = 1
        self._proxy_complete = False
        self._proxy_location = self.ai.mediator.get_enemy_nat
        self._primary_builder_tag = 0

    async def on_step(self) -> None:
        if self._proxy_complete:
            self._max_proxy_workers = 1
        elif self.ai.time > 30.0:
            self._max_proxy_workers = 2

        if proxy_probes := self._handle_proxy_probe_assignment(
            self._max_proxy_workers, self._proxy_location
        ):
            await self._handle_cannon_construction(proxy_probes)

        macro_plan: MacroPlan = MacroPlan()
        if self.ai.time > 110.0:
            macro_plan.add(AutoSupply(self.ai.start_location))
        macro_plan.add(BuildWorkers(15))
        self.ai.register_behavior(macro_plan)

    async def _handle_cannon_construction(self, proxy_probes: Units) -> None:
        if not self._primary_builder_tag and proxy_probes:
            self._primary_builder_tag = cy_closest_to(
                self._proxy_location, proxy_probes
            ).tag

        next_item_to_build: UnitTypeId | None = self._next_build_target(
            CANNON_RUSH_PLAN, self._proxy_location
        )
        if not next_item_to_build:
            self._proxy_complete = True
        build_location: Point2 | None = None
        if building_worker := self.ai.unit_tag_dict.get(self._primary_builder_tag):
            if (
                next_item_to_build
                and self.ai.tech_requirement_progress(next_item_to_build) >= 0.99
                and cy_distance_to_squared(
                    building_worker.position, self._proxy_location
                )
                < 144.0
            ):
                build_location = await self.ai.find_placement(
                    building=next_item_to_build, near=self._proxy_location
                )

        self.probe_proxy_builder.execute(
            proxy_probes,
            target=self._proxy_location,
            primary_builder_tag=self._primary_builder_tag,
            next_item_to_build=next_item_to_build,
            build_location=build_location,
        )

    def on_unit_destroyed(self, unit_tag: int) -> None:
        if unit_tag == self._primary_builder_tag:
            self._primary_builder_tag = 0
