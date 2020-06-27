#!/usr/bin/env python3

'''
Connects to an iBBQ device over Bluetooth LE and reports the temperature readings to MQTT.
Requires bluepy from pip or https://github.com/IanHarvey/bluepy
Aiming to make this work on a Pi Zero.

W Cooke - 2020
@8none1
https://github.com/8none1

Thanks to this page for the protocol: https://gist.github.com/uucidl/b9c60b6d36d8080d085a8e3310621d64
If you don't want to run this script as root you need to read: https://github.com/IanHarvey/bluepy/issues/313

Until I add config files or argument parsing then only thing you really need to change is the MQTT server IP address.
You might like the change the topics as well.
Otherwise it should find your thermometer automatically.

You should log your temperatures to InfluxCloud using Telegraf to take the MQTT and send it to Influx.

'''

from bluepy.btle import *
import paho.mqtt.client as mqtt

debug = True

# iBBQ static commands
CREDENTIALS_MESSAGE  = bytearray.fromhex("21 07 06 05 04 03 02 01 b8 22 00 00 00 00 00")
REALTIME_DATA_ENABLE = bytearray.fromhex("0B 01 00 00 00 00")
UNITS_FAHRENHEIT     = bytearray.fromhex("02 01 00 00 00 00")
UNITS_CELSIUS        = bytearray.fromhex("02 00 00 00 00 00")
BATTERY_LEVEL        = bytearray.fromhex("08 24 00 00 00 00")
# iBBQ static service
MAIN_SERVICE         = 0xFFF0 # Service which provides the charactistics
CCCD_UUID            = 0x2902 # We have to write here to enable notifications. bluepy doesn't do this for us. See the "show_all_descriptors" XXX Fix me
# iBBQ static charcteristics
SETTINGS_RESULTS     = 0xFFF1
PAIR_UUID            = 0xFFF2
HISTORY_UUID         = 0xFFF3 # Don't know how this works, here for completeness
REALTIMEDATA_UUID    = 0xFFF4
CMD_UUID             = 0xFFF5
# Static hex little endian ones and zeros
ON                   = bytearray.fromhex("01 00")
OFF                  = bytearray.fromhex("00 00")

def logger(message):
    if debug: print(message)

def send_mqtt(topic, value):
    logger(f"MQTT: Sending value: {value} to topic {topic}")
    mqtt_client.publish(topic, value)

class ScanDelegate(DefaultDelegate):
    def __init__(self):
        DefaultDelegate.__init__(self)
    def handleDiscovery(self, dev, isNewDev, isNewData):
        if isNewDev:
            logger(f"Discovered device {dev.addr}")

class DataDelegate(DefaultDelegate):
    def __init__(self):
        DefaultDelegate.__init__(self)
    def handleNotification(self, cHandle, data):
        if cHandle == 48:
            # this is temperature data!  48 is the handle of the probes characteristic XXX check terminology
            temps = [int.from_bytes(data[i:i+2], "little") for i in range(0,len(data),2)]
            # Note: "0xFF" or 65526 means the probe is not connected and so should be ignored.
            logger(temps)
            for idx, item in enumerate(temps):
                if item != 65526:
                    send_mqtt(f"bbq/temperature/{idx+1}",int(item/10))

        elif cHandle == 37:
            # this is battery data!
            # The first byte is a header and should always be 0x24
            # It looks like the last byte is always zero
            # The other bytes should be for current voltage and max voltage
            # Thanks @sil for the help with the struct
            header, current_voltage, max_voltage,pad = struct.unpack("<BHHB", data)
            if max_voltage == 0: max_voltage = 6580 # XXX check this
            batt_percent = 100 * current_voltage / max_voltage
            send_mqtt("bbq/battery",int(batt_percent))
            logger(batt_percent)

        else:
            logger(f"Unknown data received from handle {cHandle}: {data}")

        
def find_bbq_hwaddr():
    bbqs={}
    scanner = Scanner().withDelegate(ScanDelegate())
    devices = scanner.scan(10.0)

    for dev in devices:
        logger(f"Device {dev.addr}, RSSI={dev.rssi}dB")
        for (adtype, desc, value) in dev.getScanData():
            if desc == "Complete Local Name" and value == "iBBQ":
                bbqs[dev.rssi] = dev
                logger(f"Found iBBQ device {value} at address {dev.addr}. RSSI {dev.rssi}")

    # We should now have a dict of bbq devices, let's sort by rssi and choose the one with the best connection
    if len(bbqs) > 0:
        bbq = bbqs[sorted(bbqs.keys(), reverse=True)[0]].addr
        logger(f"Using hwaddr {bbq}")
        return bbq
    else:
        return None

mqtt_client_ip = "192.168.42.100"
mqtt_client_port = 1883

mqtt_client = mqtt.Client()
mqtt_client.connect(mqtt_client_ip, mqtt_client_port, 60)
mqtt_client.loop_start()

hwid = find_bbq_hwaddr()
if hwid is not None:
    bbq = Peripheral(hwid)
else:
    logger("No iBBQ devices found in range.")
    raise NameError("No devices found")
    

main_service = bbq.getServiceByUUID(MAIN_SERVICE)
bbq.setDelegate(DataDelegate())

# First we have to log in
login_characteristic = main_service.getCharacteristics(PAIR_UUID)[0]
login_characteristic.write(CREDENTIALS_MESSAGE) # Send the magic bytes to login

# Scan the device for all the services.  You don't seem to need to do both
# of these, but you do _have_ to do one of them.  If you don't then the notifications
# don't work and you won't get a temperature reading.  Doing both for the sake of it.
bbq_charateristic = bbq.getCharacteristics()
main_descriptors = main_service.getDescriptors()

# Then we have to enable real time data collection
settings_characteristic = main_service.getCharacteristics(CMD_UUID)[0]
settings_characteristic.write(REALTIME_DATA_ENABLE, withResponse=True)

# And we have to switch on notifications for the realtime characteristic.
# UUID 2902 is the standard descriptor UUID for CCCD which we need to write to in order
# to have data sent to us.  You can switch the services on and off with 0100 and 0000.
# The CCCD descriptor is on the REALTIMEDATA_UUID - which means it controls the data for the probes.
realtime_characteristic = main_service.getCharacteristics(REALTIMEDATA_UUID)[0] # This is where the temperature data lives.
temperature_cccd = realtime_characteristic.getDescriptors(forUUID=CCCD_UUID)[0] # This wasn't in the docs, but was in the source. It still took me all day to work it out.
# Now all we need to do is write a 1 (little endian) to it, and it will start sending data!  Easy when you know how.
temperature_cccd.write(ON)

# Let's see if we can get the battery level out of this thing as well.
# This is supposed to be read on 0xFFF1 SETTINGS_RESULTS.
settings_characteristic.write(BATTERY_LEVEL, withResponse=True)
# Then we need to do the same as before and get the CCCD descriptor and switch on notifications.
# Battery notifications are sent about every 5 mins
settingsresult_characteristic = main_service.getCharacteristics(SETTINGS_RESULTS)[0]
settingsresults_cccd = settingsresult_characteristic.getDescriptors(forUUID=CCCD_UUID)[0]
settingsresults_cccd.write(ON)


try:
    while True:
        if bbq.waitForNotifications(1):
            continue
except KeyboardInterrupt:
    logger("Caught ctrl-c.  Disconnecting from device.")
    bbq.disconnect()
except BTLEDisconnectError:
    logger("Device has gone away..")





