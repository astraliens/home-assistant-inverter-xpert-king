"""Platform for sensor integration."""
# This file shows the setup for the sensors associated with the cover.
# They are setup in the same way with the call to the async_setup_entry function
# via HA from the module __init__. Each sensor has a device_class, this tells HA how
# to display it in the UI (for know types). The unit_of_measurement property tells HA
# what the unit is, so it can display the correct range. For predefined types (such as
# battery), the unit_of_measurement should match what's expected.
import random

from homeassistant.const import (
    ATTR_VOLTAGE,
    DEVICE_CLASS_BATTERY,
    PERCENTAGE,
    DEVICE_CLASS_CURRENT,
    DEVICE_CLASS_ENERGY,
    DEVICE_CLASS_FREQUENCY,
    DEVICE_CLASS_POWER_FACTOR,
    DEVICE_CLASS_POWER,
    DEVICE_CLASS_TEMPERATURE,
    DEVICE_CLASS_VOLTAGE,
    EntityCategory
)
from homeassistant.helpers.entity import Entity

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
    SensorEntity
)

from .const import DOMAIN
from datetime import timedelta
import time

from homeassistant.components.sensor import STATE_CLASS_TOTAL_INCREASING

from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
import async_timeout
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed
import time


import logging
_LOGGER = logging.getLogger(__name__)
#SCAN_INTERVAL = timedelta(seconds=4)


# See cover.py for more details.
# Note how both entities for each inverter sensor (battry and illuminance) are added at
# the same time to the same list. This way only a single async_add_devices call is
# required.
async def async_setup_entry(hass, config_entry, async_add_entities):
    """Add sensors for passed config_entry in HA."""
    hub = hass.data[DOMAIN][config_entry.entry_id]

    new_devices = []
    for inverter in hub.inverters:
        coordinator = InverterDataCoordinator(hass, hub, inverter)
        await coordinator.async_config_entry_first_refresh()

        coordinator_diag = InverterConfigCoordinator(hass, hub, inverter)
        await coordinator_diag.async_config_entry_first_refresh()        
        available_params=inverter.get_available_params('data')

        for inverter_param in available_params:
            match inverter_param["sensor"]:
                case "voltage":
                    new_devices.append(SensorInverterVoltage(coordinator,inverter,inverter_param))
                case "current":
                    new_devices.append(SensorInverterCurrent(coordinator,inverter,inverter_param))
                case "frequency":
                    new_devices.append(SensorInverterFrequency(coordinator,inverter,inverter_param))
                case "power":
                    new_devices.append(SensorInverterPower(coordinator,inverter,inverter_param))
                case "energy":
                    new_devices.append(SensorInverterEnergy(coordinator,inverter,inverter_param))
                case "energy_total":
                    new_devices.append(SensorInverterEnergyTotal(coordinator,inverter,inverter_param))
                case "percent":
                    new_devices.append(SensorInverterPercent(coordinator,inverter,inverter_param))
                case "battery":
                    new_devices.append(SensorInverterBattery(coordinator,inverter,inverter_param))
                case "temperature":
                    new_devices.append(SensorInverterTemperature(coordinator,inverter,inverter_param))
                case "info":
                    new_devices.append(SensorInverterInfo(coordinator,inverter,inverter_param))

        
        new_devices.append(SensorInverterBatteryCurrent(coordinator,inverter,[]))
        new_devices.append(SensorInverterBatteryPower(coordinator,inverter,[]))
        new_devices.append(SensorInverterBatteryPowerCharging(coordinator,inverter,[]))
        new_devices.append(SensorInverterBatteryPowerDischarge(coordinator,inverter,[]))
        

        available_config_params=inverter.get_available_params('conf')

        for inverter_param in available_config_params:
            inverter_param['diag']=True
            match inverter_param["sensor"]:
                case "voltage":
                    new_devices.append(SensorInverterVoltage(coordinator_diag,inverter,inverter_param))
                case "current":
                    new_devices.append(SensorInverterCurrent(coordinator_diag,inverter,inverter_param))
                case "frequency":
                    new_devices.append(SensorInverterFrequency(coordinator_diag,inverter,inverter_param))
                case "power":
                    new_devices.append(SensorInverterPower(coordinator_diag,inverter,inverter_param))
                case "energy":
                    new_devices.append(SensorInverterEnergy(coordinator_diag,inverter,inverter_param))
                case "energy_total":
                    new_devices.append(SensorInverterEnergyTotal(coordinator_diag,inverter,inverter_param))
                case "percent":
                    new_devices.append(SensorInverterPercent(coordinator_diag,inverter,inverter_param))
                case "battery":
                    new_devices.append(SensorInverterBattery(coordinator_diag,inverter,inverter_param))
                case "temperature":
                    new_devices.append(SensorInverterTemperature(coordinator_diag,inverter,inverter_param))
                case "info":
                    new_devices.append(SensorInverterInfo(coordinator_diag,inverter,inverter_param))

    if new_devices:
        async_add_entities(new_devices)


class InverterDataCoordinator(DataUpdateCoordinator):
    """My custom coordinator."""

    def __init__(self, hass, hub, inverter):
        """Initialize my coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name="Inverter Data Coordinator",
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(seconds=7),
        )
        self.hub = hub
        self.inverter=inverter

    async def _async_update_data(self):
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        try:
            # Note: asyncio.TimeoutError and aiohttp.ClientError are already
            # handled by the data update coordinator.
            async with async_timeout.timeout(30):
                # Grab active context variables to limit data required to be fetched from API
                # Note: using context is not required if there is no need or ability to limit
                # data retrieved from API.
                listening_idx = set(self.async_contexts())
                #return await self.hub.fetch_data(listening_idx)
                return await self.hass.async_add_executor_job(self.hub.inverter_client.GetInverterDataByCommandSync,'data') # nedd to avoid problem with UI freezing during data update process
                #return await self.hub.inverter_client.GetInverterDataByCommand('data')
                
        except Exception as e:
            s = str(e)
            # Raising ConfigEntryAuthFailed will cancel future updates
            # and start a config flow with SOURCE_REAUTH (async_step_reauth)
            _LOGGER.error("-------------------- COORDINATOR DATA UPDATE FAILED ----------------" + s)
            self.inverter.reconnect()
        

class InverterConfigCoordinator(DataUpdateCoordinator):
    """My custom coordinator."""

    def __init__(self, hass, hub, inverter):
        """Initialize my coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name="Inverter Config Coordinator",
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(seconds=600),
        )
        self.hub = hub
        self.inverter=inverter

    async def _async_update_data(self):
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        try:
            # Note: asyncio.TimeoutError and aiohttp.ClientError are already
            # handled by the data update coordinator.
            async with async_timeout.timeout(30):
                # Grab active context variables to limit data required to be fetched from API
                # Note: using context is not required if there is no need or ability to limit
                # data retrieved from API.
                listening_idx = set(self.async_contexts())
                #return await self.hub.fetch_data(listening_idx)
                return await self.hub.inverter_client.GetInverterDataByCommand('conf')
                
        except Exception as e:
            s = str(e)
            # Raising ConfigEntryAuthFailed will cancel future updates
            # and start a config flow with SOURCE_REAUTH (async_step_reauth)
            _LOGGER.error("-------------------- COORDINATOR DATA UPDATE FAILED ----------------" + s)
            self.inverter.reconnect()


# This base class shows the common properties and methods for a sensor as used in this
# example. See each sensor for further details about properties and methods that
# have been overridden.
class SensorBase(CoordinatorEntity, SensorEntity):
    """An entity using CoordinatorEntity.

    The CoordinatorEntity class provides:
      should_poll
      async_update
      async_added_to_hass
      available

    """
    device_class = ""
    state_class = ""

    def __init__(self, coordinator, inverter, inverter_param):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.hub = coordinator.hub
        self.inverter_param = inverter_param
        self._inverter = inverter
        self.param_name=inverter_param['param']

    # To link this entity to the cover device, this property must return an
    # identifiers value matching that used in the cover, but no other information such
    # as name. If name is returned, this entity will then also become a device in the
    # HA UI.

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        test =1
        """
        #self._attr_is_on = self.coordinator.data[self.idx]["text"]
        self._attr_is_on = 1
        self.async_write_ha_state()
        """
    @property
    def device_info(self):
        """Return information to link this entity with the correct device."""
        return {"identifiers": {(DOMAIN, self._inverter.inverter_id)}}

    # This property is important to let HA know if this entity is online or not.
    # If an entity is offline (return False), the UI will refelect this.
    @property
    def available(self) -> bool:
        """Return True if inverter and hub is available."""
        return self._inverter.online and self._inverter.hub.online


class SensorInverter(SensorBase):
    def __init__(self, coordinator, inverter, inverter_param):
        super().__init__(coordinator, inverter, inverter_param)
        self.sensor_name=inverter_param["param"]
        self._attr_unique_id = f"{self._inverter.inverter_id}_{inverter_param['param']}"
        #self._attr_name = f"{self._inverter.name} {self.current_data['text']}"
        self._attr_name = f"{inverter_param['text']}"
        self._state = 0
        self._attr_native_unit_of_measurement = inverter_param["unit"]
        if('diag' in inverter_param and inverter_param['diag']==True):
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    def GetParamDataByName(self, param_name):
        for data_item in self.coordinator.data:
            if(data_item['param']==param_name):
                return data_item

    @property
    def state(self) :
        return self.GetParamDataByName(self.sensor_name)["value"]

    @property
    def entity_registry_enabled_default(self) -> bool:
        disabled_params=['machine_type','output_mode','pv_power_balance','pv_ok_condition_for_parallel']
        """Return if the entity should be enabled when first added to the entity registry."""
        # Hide fast changing status sensors
        if self.param_name in (disabled_params):
            return False
        return True

class SensorInverterVoltage(SensorInverter):
    def __init__(self, coordinator, inverter, inverter_param):
        super().__init__(coordinator, inverter, inverter_param)
        self.state_class = SensorStateClass.MEASUREMENT
        self.device_class = DEVICE_CLASS_VOLTAGE

class SensorInverterCurrent(SensorInverter):
    def __init__(self, coordinator, inverter, inverter_param):
        super().__init__(coordinator, inverter, inverter_param)
        self.state_class = SensorStateClass.MEASUREMENT
        self.device_class = DEVICE_CLASS_CURRENT

class SensorInverterFrequency(SensorInverter):
    def __init__(self, coordinator, inverter, inverter_param):
        super().__init__(coordinator, inverter, inverter_param)
        self.state_class = SensorStateClass.MEASUREMENT
        self.device_class = DEVICE_CLASS_FREQUENCY

class SensorInverterPower(SensorInverter):
    def __init__(self, coordinator, inverter, inverter_param):
        super().__init__(coordinator, inverter, inverter_param)
        self.state_class = SensorStateClass.MEASUREMENT
        self.device_class = DEVICE_CLASS_POWER

class SensorInverterPercent(SensorInverter):
    def __init__(self, coordinator, inverter, inverter_param):
        super().__init__(coordinator, inverter, inverter_param)
        self.state_class = SensorStateClass.MEASUREMENT
        self.device_class = DEVICE_CLASS_POWER
        self._attr_native_unit_of_measurement = PERCENTAGE

class SensorInverterBattery(SensorInverter):
    def __init__(self, coordinator, inverter, inverter_param):
        super().__init__(coordinator, inverter, inverter_param)
        self.state_class = SensorStateClass.MEASUREMENT
        self.device_class = DEVICE_CLASS_BATTERY
        self._attr_native_unit_of_measurement = PERCENTAGE

class SensorInverterTemperature(SensorInverter):
    def __init__(self, coordinator, inverter, inverter_param):
        super().__init__(coordinator, inverter, inverter_param)
        self.state_class = SensorStateClass.MEASUREMENT
        self.device_class = DEVICE_CLASS_TEMPERATURE

class SensorInverterInfo(SensorInverter):
    def __init__(self, coordinator, inverter, inverter_param):
        super().__init__(coordinator, inverter, inverter_param)
        self.state_class = SensorStateClass.MEASUREMENT

class SensorInverterEnergy(SensorInverter):
    def __init__(self, coordinator, inverter, inverter_param):
        super().__init__(coordinator, inverter, inverter_param)
        self.state_class = SensorStateClass.MEASUREMENT
        self.device_class = SensorDeviceClass.ENERGY

class SensorInverterEnergyTotal(SensorInverter):
    def __init__(self, coordinator, inverter, inverter_param):
        super().__init__(coordinator, inverter, inverter_param)
        self.state_class = SensorStateClass.MEASUREMENT
        self.device_class = SensorDeviceClass.ENERGY
        #self.state_class = SensorStateClass.TOTAL_INCREASING # usefull, but untill we solve problem with processing incoming data with zerovalues TOTAL_INCREASING will lead to incorrect data aggregation on HA side
        self.state_class = SensorStateClass.TOTAL


class SensorInverterBatteryCurrent(SensorInverter):
    device_class = DEVICE_CLASS_CURRENT
    state_class=SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, inverter, inverter_param):
        inverter_param={'param':'battery_current','text':'Battery current','unit':'A'}
        super().__init__(coordinator, inverter, inverter_param)
        self._attr_unique_id = f"{self._inverter.inverter_id}_{self.sensor_name}"
        self._state = 0

    @property
    def state(self):
        return self.GetParamDataByName('battery_charging_current')["value"] - self.GetParamDataByName('battery_discharge_current')["value"]

class SensorInverterBatteryPower(SensorInverter):
    device_class = DEVICE_CLASS_POWER
    state_class=SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, inverter, inverter_param):
        inverter_param={'param':'battery_power','text':'Battery power','unit':'W'}
        super().__init__(coordinator, inverter, inverter_param)
        self._attr_unique_id = f"{self._inverter.inverter_id}_{self.sensor_name}"
        self._state = 0

    @property
    def state(self):
        return (self.GetParamDataByName('battery_charging_current')["value"] - self.GetParamDataByName('battery_discharge_current')["value"]) * self.GetParamDataByName('battery_voltage')["value"]

class SensorInverterBatteryPowerCharging(SensorInverter):
    device_class = DEVICE_CLASS_POWER
    state_class=SensorStateClass.MEASUREMENT
    def __init__(self, coordinator, inverter, inverter_param):
        inverter_param={'param':'battery_power_charging','text':'Battery Power Charging','unit':'W'}
        super().__init__(coordinator, inverter, inverter_param)

    @property
    def state(self):
        return self.GetParamDataByName('battery_charging_current')["value"] * self.GetParamDataByName('battery_voltage')["value"]
    
class SensorInverterBatteryPowerDischarge(SensorInverter):
    device_class = DEVICE_CLASS_POWER
    state_class=SensorStateClass.MEASUREMENT
    def __init__(self, coordinator, inverter, inverter_param):
        inverter_param={'param':'battery_power_discharge','text':'Battery Power Discharge','unit':'W'}
        super().__init__(coordinator, inverter, inverter_param)

    @property
    def state(self):
        return self.GetParamDataByName('battery_discharge_current')["value"] * self.GetParamDataByName('battery_voltage')["value"]    

