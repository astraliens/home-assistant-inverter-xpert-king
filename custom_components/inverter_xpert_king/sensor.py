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

import logging
_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=4)


# See cover.py for more details.
# Note how both entities for each inverter sensor (battry and illuminance) are added at
# the same time to the same list. This way only a single async_add_devices call is
# required.
async def async_setup_entry(hass, config_entry, async_add_entities):
    """Add sensors for passed config_entry in HA."""
    hub = hass.data[DOMAIN][config_entry.entry_id]

    new_devices = []
    for inverter in hub.inverters:
        available_params=inverter.get_available_params()
        for inverter_param in available_params:
            match inverter_param["sensor"]:
                case "voltage":
                    new_devices.append(SensorInverterVoltage(inverter,inverter_param["param"]))
                case "current":
                    new_devices.append(SensorInverterCurrent(inverter,inverter_param["param"]))
                case "frequency":
                    new_devices.append(SensorInverterFrequency(inverter,inverter_param["param"]))
                case "power":
                    new_devices.append(SensorInverterPower(inverter,inverter_param["param"]))
                case "percent":
                    new_devices.append(SensorInverterPercent(inverter,inverter_param["param"]))
                case "battery":
                    new_devices.append(SensorInverterBattery(inverter,inverter_param["param"]))
                case "temperature":
                    new_devices.append(SensorInverterTemperature(inverter,inverter_param["param"]))
                case "info":
                    new_devices.append(SensorInverterInfo(inverter,inverter_param["param"]))

        new_devices.append(SensorInverterBatteryCurrent(inverter))
        new_devices.append(SensorInverterBatteryPower(inverter))
        new_devices.append(SensorInverterEnergyToday(inverter))
        new_devices.append(SensorInverterEnergyTotal(inverter))

        for func_sensor_name in ['get_mode']:
            new_devices.append(SensorInverterInfoFunction(inverter,func_sensor_name))

        cur_time=time.time()
        if(inverter.last_current_diag_time<cur_time-inverter.last_current_diag_min_interval):
            inverter.last_current_diag_time=time.time()
            for diag_sensor_name in ['get_model','get_cpu_firmware_version','get_panel_firmware_version','get_bt_version','get_serial','get_model2']:
                new_devices.append(SensorInverterDiag(inverter,diag_sensor_name))

        available_conf_params=inverter.get_available_conf_params()
        for inverter_conf_param in available_conf_params:
            new_devices.append(SensorInverterConfDiag(inverter,inverter_conf_param["param"]))

    if new_devices:
        async_add_entities(new_devices)



# This base class shows the common properties and methods for a sensor as used in this
# example. See each sensor for further details about properties and methods that
# have been overridden.
class SensorBase(SensorEntity):
    """Base representation of a inverter Sensor."""
    #should_poll = False
    device_class = ""
    state_class = "" # SensorStateClass.MEASUREMENT

    def __init__(self, inverter, param_name):
        """Initialize the sensor."""
        self._inverter = inverter

    # To link this entity to the cover device, this property must return an
    # identifiers value matching that used in the cover, but no other information such
    # as name. If name is returned, this entity will then also become a device in the
    # HA UI.
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

    async def async_added_to_hass(self):
        """Run when this Entity has been added to HA."""
        # Sensors should also register callbacks to HA when their state changes
        self._inverter.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self):
        """Entity being removed from hass."""
        # The opposite of async_added_to_hass. Remove any registered call backs here.
        self._inverter.remove_callback(self.async_write_ha_state)

class SensorInverter(SensorBase):
    device_class = ""
    state_class = SensorStateClass.MEASUREMENT
    def __init__(self, inverter, param_name):
        super().__init__(inverter, param_name)
        self.sensor_name=param_name
        self.current_data=self._inverter.get_state_param(param_name)
        self._attr_unique_id = f"{self._inverter.inverter_id}_{self.current_data['param']}"
        #self._attr_name = f"{self._inverter.name} {self.current_data['text']}"
        self._attr_name = f"{self.current_data['text']}"
        self._state = 0
        self._attr_native_unit_of_measurement = self.current_data["unit"]

    @property
    def state(self):
        return self._inverter.get_state_param(self.sensor_name)["value"]

class SensorInverterVoltage(SensorInverter):
    def __init__(self, inverter, param_name):
        super().__init__(inverter, param_name)
        self.device_class = DEVICE_CLASS_VOLTAGE

class SensorInverterCurrent(SensorInverter):
    def __init__(self, inverter, param_name):
        super().__init__(inverter, param_name)
        self.device_class = DEVICE_CLASS_CURRENT

class SensorInverterFrequency(SensorInverter):
    def __init__(self, inverter, param_name):
        super().__init__(inverter, param_name)
        self.device_class = DEVICE_CLASS_FREQUENCY

class SensorInverterPower(SensorInverter):
    def __init__(self, inverter, param_name):
        super().__init__(inverter, param_name)
        self.device_class = DEVICE_CLASS_POWER

class SensorInverterPercent(SensorInverter):
    def __init__(self, inverter, param_name):
        super().__init__(inverter, param_name)
        self.device_class = DEVICE_CLASS_POWER
        self._attr_native_unit_of_measurement = PERCENTAGE

class SensorInverterBattery(SensorInverter):
    def __init__(self, inverter, param_name):
        super().__init__(inverter, param_name)
        self.device_class = DEVICE_CLASS_BATTERY
        self._attr_native_unit_of_measurement = PERCENTAGE

class SensorInverterTemperature(SensorInverter):
    state_class=SensorStateClass.MEASUREMENT
    device_class = DEVICE_CLASS_TEMPERATURE

    def __init__(self, inverter, param_name):
        super().__init__(inverter, param_name)

class SensorInverterInfo(SensorInverter):
    state_class=''
    device_class = ''

    def __init__(self, inverter, param_name):
        super().__init__(inverter, param_name)


class SensorInverterByFunctionName(SensorBase):
    def __init__(self, inverter, param_name):
        super().__init__(inverter, param_name)
        self.param_name=param_name
        func_inverter = getattr(self._inverter.hub.inverter_client, self.param_name) # get function by param_name from inverter client
        self.current_data=func_inverter(True)
        self.sensor_name=self.current_data['param']
        self._attr_unique_id = f"{self._inverter.inverter_id}_diag_{self.current_data['param']}"
        self._attr_name = f"{self.current_data['text']}"
        self._state = 0
        self._attr_native_unit_of_measurement = self.current_data['unit']

    @property
    def state(self):
        func_inverter = getattr(self._inverter.hub.inverter_client, self.param_name) # get function by param_name from inverter client
        return func_inverter(True)["value"]

class SensorInverterDiag(SensorInverterByFunctionName):
    entity_category = EntityCategory.DIAGNOSTIC
    def __init__(self, inverter, param_name):
        super().__init__(inverter, param_name)


class SensorInverterInfoFunction(SensorInverterByFunctionName):
    def __init__(self, inverter, param_name):
        super().__init__(inverter, param_name)


class SensorInverterConfDiag(SensorBase):
    entity_category = EntityCategory.DIAGNOSTIC
    def __init__(self, inverter, param_name):
        super().__init__(inverter, param_name)
        self.param_name=param_name
        self.current_data=self._inverter.get_conf_param(param_name)
        self._attr_unique_id = f"{self._inverter.inverter_id}_diag_{self.current_data['param']}"
        self._attr_name = f"{self.current_data['text']}"
        self.sensor_name=self.current_data['param']
        self._state = 0
        self._attr_native_unit_of_measurement = self.current_data['unit']

    @property
    def state(self):
        return self._inverter.get_conf_param(self.sensor_name)["value"]

    @property
    def entity_registry_enabled_default(self) -> bool:
        disabled_params=['machine_type','output_mode','pv_power_balance','pv_ok_condition_for_parallel']
        """Return if the entity should be enabled when first added to the entity registry."""
        # Hide fast changing status sensors
        if self.param_name in (disabled_params):
            return False
        return True

class SensorInverterBatteryCurrent(SensorBase):
    device_class = DEVICE_CLASS_CURRENT

    def __init__(self, inverter, param_name='battery_current'):
        super().__init__(inverter, param_name)
        self.sensor_name=param_name
        self.current_data=[]
        self._attr_unique_id = f"{self._inverter.inverter_id}_{self.sensor_name}"
        #self._attr_name = f"{self._inverter.name} Battery current"
        self._attr_name = f"Battery current"
        self._state = 0
        self._attr_native_unit_of_measurement = self._inverter.get_state_param('battery_charging_current')["unit"]

    @property
    def state(self):
        return self._inverter.get_state_param('battery_charging_current')["value"] - self._inverter.get_state_param('battery_discharge_current')["value"]

class SensorInverterBatteryPower(SensorBase):
    device_class = DEVICE_CLASS_POWER

    def __init__(self, inverter, param_name='battery_power'):
        super().__init__(inverter, param_name)
        self.sensor_name=param_name
        self.current_data=[]
        self._attr_unique_id = f"{self._inverter.inverter_id}_{self.sensor_name}"
        #self._attr_name = f"{self._inverter.name} Battery Power"
        self._attr_name = f"Battery Power"
        self._state = 0
        self._attr_native_unit_of_measurement = 'W'

    @property
    def state(self):
        return (self._inverter.get_state_param('battery_charging_current')["value"] - self._inverter.get_state_param('battery_discharge_current')["value"]) * self._inverter.get_state_param('battery_voltage')["value"]

class SensorInverterEnergyToday(SensorBase):
    device_class = SensorDeviceClass.ENERGY
    state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, inverter, param_name='total_energy_today'):
        super().__init__(inverter, param_name)
        self.sensor_name=param_name
        self.current_data=self._inverter.hub.inverter_client.get_energy_today()
        self._attr_unique_id = f"{self._inverter.inverter_id}_{self.current_data['param']}"
        #self._attr_name = f"{self._inverter.name} {self.current_data['text']}"
        self._attr_name = f"{self.current_data['text']}"
        self._state = 0
        self._attr_native_unit_of_measurement = self.current_data['unit']
        #self.last_reset=self.current_data['last_reset']

    @property
    def state(self):
        #self.last_reset=self.current_data['last_reset']
        return self._inverter.hub.inverter_client.get_energy_today()["value"]


class SensorInverterEnergyTotal(SensorBase):
    device_class = SensorDeviceClass.ENERGY
    state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, inverter, param_name='total_output_load_energy'):
        super().__init__(inverter, param_name)
        self.sensor_name=param_name
        self.current_data=self._inverter.hub.inverter_client.get_energy_total()
        self._attr_unique_id = f"{self._inverter.inverter_id}_{self.current_data['param']}"
        #self._attr_name = f"{self._inverter.name} {self.current_data['text']}"
        self._attr_name = f"{self.current_data['text']}"
        self._state = 0
        self._attr_native_unit_of_measurement = self.current_data['unit']

    @property
    def state(self):
        return self._inverter.hub.inverter_client.get_energy_total()["value"]

