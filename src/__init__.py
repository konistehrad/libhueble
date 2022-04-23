from bleak import BleakClient
from rgbxy import Converter, GamutC, get_light_gamut
from struct import pack, unpack

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


class Lamp(object):
    """A wrapper for the Philips Hue BLE protocol"""

    def __init__(self, address, name: str = None):
        self.converter = None
        self.address = address
        self.name = name
        self.client = None

    @property
    def is_connected(self):
        return self.client and self.client.is_connected

    async def connect(self):
        # reinitialize BleakClient for every connection to avoid errors
        self.client = BleakClient(self.address)
        await self.client.connect()

        model = await self.get_model()
        try:
            self.converter = Converter(get_light_gamut(model))
        except ValueError:
            self.converter = Converter(GamutC)

    async def disconnect(self):
        await self.client.disconnect()
        self.client = None

    async def get_model(self):
        """Returns the model string"""
        model = await self.client.read_gatt_char(CHAR_MODEL)
        return model.decode('ascii')

    async def get_power(self):
        """Gets the current power state"""
        power = await self.client.read_gatt_char(CHAR_POWER)
        return bool(power[0])

    async def set_power(self, on):
        """Sets the power state"""
        await self.client.write_gatt_char(CHAR_POWER, bytes([1 if on else 0]), response=True)

    async def get_brightness(self):
        """Gets the current brightness as a float between 0.0 and 1.0"""
        brightness = await self.client.read_gatt_char(CHAR_BRIGHTNESS)
        return brightness[0] / 255

    async def set_brightness(self, brightness):
        """Sets the brightness from a float between 0.0 and 1.0"""
        await self.client.write_gatt_char(CHAR_BRIGHTNESS, bytes([max(min(int(round(brightness * 255)), 254), 1)]), response=True)

    async def get_temperature(self):
        """Gets the current color temperature as a float between 0.0 and 1.0"""
        temperature = await self.client.read_gatt_char(CHAR_TEMPERATURE)
        return ((temperature[1] << 8 | temperature[0]) - 153) / 301

    async def set_temperature(self, temperature):
        """Sets the color temperature from a float between 0.0 and 1.0"""
        temperature = max(temperature, 0)
        temperature = min(int(round(temperature * 301)) + 153, 454)
        await self.client.write_gatt_char(CHAR_TEMPERATURE, bytes([temperature & 0xFF, temperature >> 8]), response=True)

    async def get_color_xy(self):
        """Gets the current XY color coordinates as floats between 0.0 and 1.0"""
        buf = await self.client.read_gatt_char(CHAR_COLOR)
        x, y = unpack('<HH', buf)
        return x / 0xFFFF, y / 0xFFFF

    async def set_color_xy(self, x, y):
        """Sets the XY color coordinates from floats between 0.0 and 1.0"""
        buf = pack('<HH', int(x * 0xFFFF), int(y * 0xFFFF))
        await self.client.write_gatt_char(CHAR_COLOR, buf, response=True)

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
