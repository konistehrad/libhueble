import math
import asyncio
from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from rgbxy import Converter, GamutC, get_light_gamut
from struct import pack, unpack
import logging

# model number as an ASCII string
CHAR_MODEL = '00002a24-0000-1000-8000-00805f9b34fb'
# power state (0 or 1)
CHAR_POWER = '932c32bd-0002-47a2-835a-a8d455b859dd'
# brightness (1 to 254)
CHAR_BRIGHTNESS = '932c32bd-0003-47a2-835a-a8d455b859dd'
# temperature (153 to 454)
CHAR_TEMPERATURE = '932c32bd-0004-47a2-835a-a8d455b859dd'
# color (CIE XY coordinates converted to two 16-bit little-endian integers)
CHAR_COLOR = '932c32bd-0005-47a2-835a-a8d455b859dd'
# all of the above characteristics
ALL_CHARS = [CHAR_MODEL, CHAR_POWER, CHAR_BRIGHTNESS, CHAR_TEMPERATURE, CHAR_COLOR]


class Lamp(object):
    """A wrapper for the Philips Hue BLE protocol"""

    @classmethod
    async def discover(cls, timeout: float = 5.0, create_task=asyncio.create_task):
        discovered_lamps = set()
        detected_device_addresses = set()
        detection_callback_tasks = set()

        async def detection_callback_async(device: BLEDevice, _):
            async with BleakClient(device.address, timeout=math.inf) as client:
                services = await client.get_services()
                characteristic_uuids = [characteristic.uuid for service in services for characteristic in
                                        service.characteristics]
                if set(ALL_CHARS) <= set(characteristic_uuids):
                    discovered_lamps.add(device)

        def detection_callback(device: BLEDevice, _):
            if device.address not in detected_device_addresses:
                detected_device_addresses.add(device.address)
                task = create_task(detection_callback_async(device, _))
                detection_callback_tasks.add(task)

        await BleakScanner.discover(timeout, detection_callback=detection_callback)

        for detection_callback_task in detection_callback_tasks:
            detection_callback_task.cancel()

        return [cls(lamp, lamp.name) for lamp in discovered_lamps]

    def __init__(self, address_or_ble_device: str | BLEDevice, name: str = None, create_task=asyncio.create_task):
        self.__create_task = create_task
        self.converter = None
        if isinstance(address_or_ble_device, BLEDevice):
            self.__ble_device = address_or_ble_device
            self.address = address_or_ble_device.address
        else:
            self.__ble_device = None
            self.address = address_or_ble_device
        if name is None and isinstance(address_or_ble_device, BLEDevice):
            self.name = address_or_ble_device.name
        self.client = None
        self.__logger = logging.getLogger('io.github.alex1s.libhueble')

        self.__model = None
        self.__power = None
        self.__brightness = None
        self.__color = None

    @property
    def is_connected(self):
        return self.client and self.client.is_connected and self.__model is not None and self.__power is not None and self.__brightness is not None and self.__color is not None

    async def connect(self):
        # reinitialize BleakClient for every connection to avoid errors
        if self.__ble_device is not None:
            self.client = BleakClient(self.__ble_device)
        else:
            self.client = BleakClient(self.address)
        await self.client.connect()

        # init model
        model_bytes = await self.client.read_gatt_char(CHAR_MODEL)
        model_str = model_bytes.decode('ascii')
        self.__model = model_str
        self.__logger.debug(f'Model initialized to "{self.__model}"')

        try:
            self.converter = Converter(get_light_gamut(self.model))
        except ValueError:
            self.converter = Converter(GamutC)

        # init power
        def power_callback(sender: int, data: bytearray):
            self.__logger.debug(f'Power notification: sender={sender}, data={data}')
            assert data in [b'\x00', b'\x01']
            if data == b'\x01':
                self.__power = True
            else:
                self.__power = False

        await self.client.start_notify(CHAR_POWER, power_callback)
        pwr = await self.client.read_gatt_char(CHAR_POWER)
        power_callback(-1, pwr)

        # init brightness
        def brightness_callback(sender: int, data: bytearray):
            self.__logger.debug(f'Brightness notification: sender={sender}, data={data}')
            assert len(data) == 1
            self.__brightness = data[0]

        await self.client.start_notify(CHAR_BRIGHTNESS, brightness_callback)
        bright = await self.client.read_gatt_char(CHAR_BRIGHTNESS)
        brightness_callback(-1, bright)

        # init color
        def color_callback(sender: int, data: bytearray):
            self.__logger.debug(f'Color notification: sender={sender}, data={data}')
            assert len(data) == 4
            self.__color = unpack('<HH', data)

        await self.client.start_notify(CHAR_COLOR, color_callback)
        color = await self.client.read_gatt_char(CHAR_COLOR)
        color_callback(-1, color)

    async def disconnect(self):
        await self.client.disconnect()
        self.client = None

    @property
    def model(self) -> str:
        """The model string"""
        return self.__model

    def get_power(self):
        return self.__power

    async def set_power(self, on: bool) -> None:
        if self.__power == on:
            return
        await self.client.write_gatt_char(CHAR_POWER,  bytes([1 if on else 0]), response=True)
        self.__power = on

    def get_brightness(self) -> float:
        return self.__brightness / 0xFE

    async def set_brightness(self, value: float) -> None:
        hue_value = round(value * 0xFE)
        self.__logger.debug(f'Rounding brightness {value} to a hue value of {hue_value}.')
        if hue_value == 0:
            await self.set_power(False)
            return
        if hue_value != self.__brightness:
            # yes, we can set the brightness while the lamp is off
            await self.client.write_gatt_char(CHAR_BRIGHTNESS, bytes([hue_value]), True)
            self.__brightness = hue_value
        await self.set_power(True)

    def get_color(self):
        return round(self.__color[0] * 0xFFFF), round(self.__color[1] * 0xFFFF)

    async def set_color(self, color):
        x, y = round(color[0] * 0xFFFF), round(color[1] * 0xFFFF)
        if (x, y) == self.__color:
            return
        data = pack('<HH', x, y)
        await self.client.write_gatt_char(CHAR_COLOR, data, True)
        self.__color = x, y

    async def get_temperature(self):
        """Gets the current color temperature as a float between 0.0 and 1.0"""
        temperature = await self.client.read_gatt_char(CHAR_TEMPERATURE)
        return ((temperature[1] << 8 | temperature[0]) - 153) / 301

    async def set_temperature(self, temperature):
        """Sets the color temperature from a float between 0.0 and 1.0"""
        temperature = max(temperature, 0)
        temperature = min(int(round(temperature * 301)) + 153, 454)
        await self.client.write_gatt_char(CHAR_TEMPERATURE, bytes([temperature & 0xFF, temperature >> 8]), response=True)

    async def get_color_rgb(self):
        """Gets the RGB color as floats between 0.0 and 1.0"""
        x, y = await self.get_color_xy()
        return self.converter.xy_to_rgb(x, y)

    async def set_color_rgb(self, r, g, b):
        """Sets the RGB color from floats between 0.0 and 1.0"""
        x, y = self.converter.rgb_to_xy(r, g, b)
        await self.set_color_xy(x, y)

    def __repr__(self):
        if self.name is None:
            return self.address
        else:
            return f'{self.name} ({self.address})'
