# Home Assistant Inverter Axpert King

Home Assistant Integration for real-time data monitoring / edit settings of Axpert King (also known as Voltronic Axpert III) inverters via RS232-TCP Gateway

Requires **Home Assistant 2025.8.0** or newer.

## Features

- Read live values and config params from inverter
- Change inverter params (battery charge-discharge voltage / battery charge current / other inverter settings) (only if advanced mode ON)
- Battery profiles
- Add energy sensors for integration with energy usage
- Tracks daily energy consumption
- Sync inverter date and time

## Todo

- Add support for RS232-serial connection

## Notes

Due to low baud rate in inverter this integration can be slow.

> [!CAUTION] 
> **DO NOT** activate **advanced settings** if you don't know how to setup battery voltages / charge current or anything else.  
> Changing setting without understanding can lead to **inverter or battery damage.**
> Any changes you are making to settings of your inverter with this integration you are doing on your own decision. There is no warranty if it will work correctly.
> ![image](https://raw.githubusercontent.com/astraliens/home-assistant-inverter-xpert-king/main/images/integration_inverter_settings.png)

## Battery profiles

A profile is a named set of battery settings. Add, edit or delete them under **Configure**. Apply a profile from the **Battery - Profile** entity on the device card.

Leave a voltage field empty, or leave a current on **Do not change**, to keep the value that is already on the inverter. Only the fields you fill in are written.

Voltage changes are accepted only while the inverter battery type is **User define**. The integration never changes battery type for you — switch it yourself first if your profile includes voltages.

Applying a profile **writes voltages and currents to the inverter**. Same caution as advanced settings: record the current battery values before the first apply, and do not use this if you don't know which setting can be applied to your battery. Incorrect settings can damage your battery or inverter.

![Add battery profile](https://raw.githubusercontent.com/astraliens/home-assistant-inverter-xpert-king/main/images/integration_inverter_profile_add.png)

## Connecting Inverter to gateway and HA

To connect inverter to HA you need any RS232->TCP gateway (I've used HI-FLYING HF5142B, coz prefer wires, but you can use any other or try to connect it with esp32 / 8266 bridging it's serial to TCP socket). 8P8C connector should be plugged to PC port on inverter side and DB9 connector should be plugged in RS232->TCP gateway.
On gateway side you should configure TCP server to forward data from Serial port, it should be tcp server without authorisation (we will add auth in future if gateway will support it), you can choose any port to listen.

#### Serial params:
- Baud rate: 2400
- Data bit: 8
- Stop bit: 1
- Parity: None

![inverter_connection](https://raw.githubusercontent.com/astraliens/home-assistant-inverter-xpert-king/main/images/inverter_connection.jpg)
![HF5142B_connection](https://raw.githubusercontent.com/astraliens/home-assistant-inverter-xpert-king/main/images/HF5142B_connection.jpg)
![HF5142B_TCP_Server](https://raw.githubusercontent.com/astraliens/home-assistant-inverter-xpert-king/main/images/HF5142B_TCP_Server.jpg)

After connecting inverter you need to add `home-assistant-inverter-xpert-king` integration using HACS to your HA. In your HA go to *Main Menu -> HACS -> Integrations*, in top right corner press 3 dots and click to "Custom Repositories". Add repository `https://github.com/astraliens/home-assistant-inverter-xpert-king` and category `Integration`. After this step close modal add repository window and press "Explore & Download Repositories" blue button at the bottom right corner of screen and search for `Inverter Axpert King` integration. Press on it and in right bottom corner of screen press "Download" button. 
After this you can simply add it like regular integration, specifying IP and port of gateway where inverter connected. In several seconds integration get data from inverter, create all found sensors and will update them constantly.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=astraliens&repository=home-assistant-inverter-xpert-king&category=integration)

For those, who lost cable or connecting inverter directly to device serial port pinout of connectors shown on photo:
![rs232_db9_pinout](https://raw.githubusercontent.com/astraliens/home-assistant-inverter-xpert-king/main/images/rs232_db9_pinout.png)

## Integration screenshots

| ![image](https://raw.githubusercontent.com/astraliens/home-assistant-inverter-xpert-king/main/images/integration_inverter_config.png) | ![image](https://raw.githubusercontent.com/astraliens/home-assistant-inverter-xpert-king/main/images/integration_inverter_diag.png) | ![image](https://raw.githubusercontent.com/astraliens/home-assistant-inverter-xpert-king/main/images/integration_inverter_sensors.png) |
| :-----------------------------------------------------------------------------------------------------------------------------------: | :---------------------------------------------------------------------------------------------------------------------------------: | :------------------------------------------------------------------------------------------------------------------------------------: |

## Date and Time sync

This integration can sync your inverter date and time, but keep in mind that it will set date and time received from HA server, so before sync ensure you HA server date and time actual.

## Energy consumption monitoring

You can add `Total energy all time` sensor to your energy consumption monitoring to calculate your spents

## Donations

You can say thanks by donating for buying pizza at:

<a href="https://www.buymeacoffee.com/astraliens" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/default-orange.png" alt="Buy Me A Pizza" height="41" width="174"></a>
