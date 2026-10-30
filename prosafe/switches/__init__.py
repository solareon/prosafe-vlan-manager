from enum import StrEnum

from .general import BaseSwitch
from .gs108ev3 import Switch as DRI_GS108EV3
from .gs116ev2 import Switch as DRI_GS116EV2
from .gs308e import Switch as DRI_GS308E

SWITCH_PORT_COUNT = {
    "gs108ev3": 8,
    "gs116ev2": 16,
    "gs308e": 8,
}

SWITCH_DRIVER = {
    "gs108ev3": DRI_GS108EV3,
    "gs116ev2": DRI_GS116EV2,
    "gs308e": DRI_GS308E,
}


class SwitchModel(StrEnum):
    GS108EV3 = "gs108ev3"
    GS116EV2 = "gs116ev2"
    GS308E = "gs308e"

    @property
    def port_count(self):
        return SWITCH_PORT_COUNT[self.value]

    @property
    def driver(self) -> type[BaseSwitch]:
        return SWITCH_DRIVER[self.value]
