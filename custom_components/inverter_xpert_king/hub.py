"""A demonstration 'hub' that connects several devices."""
from __future__ import annotations
from .inverter.inverterclient import InverterClient

# In a real implementation, this would be in an external library that's on PyPI.
# The PyPI package needs to be included in the `requirements` section of manifest.json
# See https://developers.home-assistant.io/docs/creating_integration_manifest
# for more information.
# This hub always returns 3 inverters.
import asyncio
import random

from homeassistant.core import HomeAssistant
import time
import logging
_LOGGER = logging.getLogger(__name__)

class Hub:
    manufacturer = "Inverter"

    def __init__(self, hass: HomeAssistant, host: str, port: int) -> None:
        """Init hub."""
        self._host = host
        self._port = port
        self._hass = hass
        self.inverter_client=InverterClient(host,port)
        self._name = "Inverter " + host
        self._id = host.lower()
        self.manufacturer=self.inverter_client.get_model2()
        self.inverters = [
            Inverter(f"{self._id}_1", f"{self._name}", self),
            #Inverter(f"{self._id}_2", f"{self._name} 2", self),
            #Inverter(f"{self._id}_3", f"{self._name} 3", self),
        ]
        self.online = True

    @property
    def hub_id(self) -> str:
        """ID for hub."""
        return self._id

    async def test_connection(self) -> bool:
        """Test connectivity with inverter"""
        #await asyncio.sleep(1)
        return self.inverter_client.get_mode()


class Inverter:
    """inverter (device for HA)."""

    def __init__(self, inverterid: str, name: str, hub: Hub) -> None:
        """Init inverter."""
        self._id = inverterid
        self.hub = hub
        self.name = name
        self._callbacks = set()
        self._loop = asyncio.get_event_loop()
        self.model=self.hub.inverter_client.get_model()
        self.firmware_version=self.hub.inverter_client.get_cpu_firmware_version() + ' / ' + self.hub.inverter_client.get_panel_firmware_version()
        self.last_current_state_min_interval=1 # min allowed interval in seconds
        self.last_current_state=[]
        self.last_current_state_time=0
        
        self.last_current_conf_min_interval=600 # min allowed interval in seconds
        self.last_current_conf_time=0
        self.last_current_conf=[]

        self.last_current_diag_min_interval=600 # min allowed interval in seconds
        self.last_current_diag_time=0

        self.update_data()
        self.update_conf_data()

    @property
    def inverter_id(self) -> str:
        """Return ID for inverter."""
        return self._id

    @property
    def position(self):
        """Return position for inverter."""
        return self._current_position

    async def set_position(self, position: int) -> None:
        """
        Set cover to the given position.

        State is announced a random number of seconds later.
        """
        self._target_position = position

        # Update the moving status, and broadcast the update
        self.moving = position - 50
        await self.publish_updates()

        self._loop.create_task(self.delayed_update())

    async def delayed_update(self) -> None:
        """Publish updates, with a random delay to emulate interaction with device."""
        await asyncio.sleep(20)
        self.moving = 0
        await self.publish_updates()

    def register_callback(self, callback: Callable[[], None]) -> None:
        """Register callback, called when Inverter changes state."""
        self._callbacks.add(callback)

    def remove_callback(self, callback: Callable[[], None]) -> None:
        """Remove previously registered callback."""
        self._callbacks.discard(callback)

    # In a real implementation, this library would call it's call backs when it was
    # notified of any state changeds for the relevant device.
    async def publish_updates(self) -> None:
        """Schedule call all registered callbacks."""
        self._current_position = self._target_position
        for callback in self._callbacks:
            callback()

    def update_data(self):
        cur_time=time.time()
        if(self.last_current_state_time<cur_time-self.last_current_state_min_interval):
            self.last_current_state_time=time.time()
            self.last_current_state=self.hub.inverter_client.get_current_state()
            self.update_conf_data()
            
    def update_conf_data(self):
        cur_time=time.time()
        if(self.last_current_conf_time<cur_time-self.last_current_conf_min_interval):
            self.last_current_conf_time=time.time()
            self.last_current_conf=self.hub.inverter_client.get_current_conf()

    def get_state_param(self,param):
        for val in self.last_current_state:
            if(val['param'] == param):
                return val

    def get_available_params(self):
        res=[]
        for val in self.last_current_state:
            res.append({'param':val['param'],'sensor':val['sensor']})
        return res
    
    def get_conf_param(self,param):
        for val in self.last_current_conf:
            if(val['param'] == param):
                return val

    def get_available_conf_params(self):
        res=[]
        for val in self.last_current_conf:
            res.append({'param':val['param'],'sensor':val['sensor']})
        return res

    @property
    def online(self) -> float:
        """Inverter is online."""
        #  Returns True if online,
        # False if offline.
        self.update_data()
        return self.hub.inverter_client.connected

    @property
    def battery_level(self) -> int:
        return self.get_state_param('battery_capacity')

