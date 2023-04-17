# Home Assistant Inverter Axpert King
Home Assistant Integration for reading data from Axpert King (also known as Voltronic Axpert III) inverters via RS232-TCP Gateway

## Features

- Get most of live values and current config params from inverter
- Add energy sensors for integration with energy usage
- Tracks daily energy consumption
- Sync inverter date and time

## Todo

- Clear code
- Add options to change config params

## Notes

This code was written for couple of days without any skills in Python and HA architecture. That is first time I've used Python, so anyone is welcome to make this code better.
Due to low baud rate in inverter this integration can slow down your HA, so keep this in mind and if you can help with optimizing - you are welcome.

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

After connecting inverter you need to add `home-assistant-inverter-xpert-king` integration using HACS to your HA. In you HA go to *Main Menu -> HACS -> Integrations*, in top right corner press 3 dots and click to "Custom Repositories". Add repository `https://github.com/astraliens/home-assistant-inverter-xpert-king` and category `Integration`. After this step close modal add repository window and press "Explore & Download Repositories" blue button at the bottom right corner of screen and search for `Inverter Axpert King` integration. Press on it and in right bottom corner of screen press "Download" button. 
After this you can simply add it like regular integration, specifiying IP and port of gateway where inverter connected. In several seconds integration get data from inverter, create all found sensors and will update them constantly.

For those, who lost cable or connecting inverter directly to device serial port pinout of connectors shown on photo:
![rs232_db9_pinout](https://raw.githubusercontent.com/astraliens/home-assistant-inverter-xpert-king/main/images/rs232_db9_pinout.png)


## Date and Time sync

This integratino can sync your inverter date and time, but keep in mind that it will set date and time received from HA server, so before sync ensure you HA server date and time actual.

## Energy consumption monitoring

You can add `Total energy all time` sensor to your energy consumption monitoring to calculate your spents

## Donations

You can say thanks by donating for buying pizza at:

<a href="https://www.buymeacoffee.com/astraliens" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/default-orange.png" alt="Buy Me A Pizza" height="41" width="174"></a>