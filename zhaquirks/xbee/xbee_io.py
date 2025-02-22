"""Allows for direct control of an xbee's digital pins.

Reading pins should work with any coordinator (Untested)
writing pins will only work with an xbee as the coordinator as
it requires zigpy_xbee.

The xbee must be configured via XCTU to send samples to the coordinator,
DH and DL to the coordiator's address (0). and each pin must be configured
to act as a digital input.

Either configure reporting on state change by setting the appropriate bit
mask on IC or set IR to a value greater than zero to send perodic reports
every x milliseconds, I recommend the later, since this will ensure
the xbee stays alive in Home Assistant.
"""

import logging
import struct
import zigpy.types as t
from zigpy.quirks import CustomDevice, CustomCluster
from zigpy.profiles import zha
from zigpy.zcl.clusters.general import OnOff, BinaryInput

from zigpy.zcl import foundation

_LOGGER = logging.getLogger(__name__)

XBEE_PROFILE_ID = 0xC105
XBEE_IO_CLUSTER = 0x92
XBEE_REMOTE_AT = 0x17
XBEE_SRC_ENDPOINT = 0xe8
XBEE_DST_ENDPOINT = 0xe8
DIO_APPLY_CHANGES = 0x02
DIO_PIN_HIGH = 0x05
DIO_PIN_LOW = 0x04
ON_OFF_CMD = 0x0000


class IOSample(bytes):
    """Parse an XBee IO sample report."""

    # pylint: disable=R0201
    def serialize(self):
        """Serialize an IO Sample Report, Not implemented."""
        _LOGGER.debug("Serialize not implemented.")

    @classmethod
    def deserialize(cls, data):
        """Deserialize an xbee IO sample report.

        xbee digital sample format
        Digital mask byte 0,1
        Analog mask byte 3
        Digital samples byte 4, 5
        Analog Sample, 2 bytes per
        """
        digital_mask = data[0:2]
        analog_mask = data[2:3]
        digital_sample = data[3:5]
        num_bits = 13
        digital_pins = [
            (int.from_bytes(digital_mask, byteorder='big') >> bit) & 1
            for bit in range(num_bits - 1, -1, -1)]
        digital_pins = list(reversed(digital_pins))
        analog_pins = [
            (int.from_bytes(analog_mask, byteorder='big') >> bit) & 1
            for bit in range(8 - 1, -1, -1)]
        analog_pins = list(reversed(analog_pins))
        digital_samples = [
            (int.from_bytes(digital_sample, byteorder='big') >> bit) & 1
            for bit in range(num_bits - 1, -1, -1)]
        digital_samples = list(reversed(digital_samples))
        sample_index = 0
        analog_samples = []
        for apin in analog_pins:
            if apin == 1:
                analog_samples.append(
                    int.from_bytes(data[5+sample_index:7+sample_index],
                                   byteorder='big'))
                sample_index += 1
            else:
                analog_samples.append(0)

        return {
            'digital_pins': digital_pins,
            'analog_pins': analog_pins,
            'digital_samples': digital_samples,
            'analog_samples': analog_samples}, b''

# 4 AO lines
# 10 digital
# Discovered endpoint information: <SimpleDescriptor endpoint=232 profile=49413
# device_type=1 device_version=0 input_clusters=[] output_clusters=[]>


ENDPOINT_MAP = {
    0: 0xd0,
    1: 0xd1,
    2: 0xd2,
    3: 0xd3,
    4: 0xd4,
    5: 0xd5,
    10: 0xda,
    11: 0xdb,
    12: 0xdc,
}


class XBeeOnOff(CustomCluster, OnOff):
    """XBee on/off cluster."""

    ep_id_2_pin = {
        0xd0: 'D0',
        0xd1: 'D1',
        0xd2: 'D2',
        0xd3: 'D3',
        0xd4: 'D4',
        0xd5: 'D5',
        0xda: 'P0',
        0xdb: 'P1',
        0xdc: 'P2',
    }

    async def command(self, command, *args,
                      manufacturer=None, expect_reply=True):
        """Xbee change pin state command, requires zigpy_xbee."""
        pin_name = self.ep_id_2_pin.get(self._endpoint.endpoint_id)
        if command not in [0, 1] or pin_name is None:
            return super().command(command, *args)
        if command == 0:
            pin_cmd = DIO_PIN_LOW
        else:
            pin_cmd = DIO_PIN_HIGH
        await self._endpoint.device.remote_at(pin_name, pin_cmd)
        return 0, foundation.Status.SUCCESS


class XbeeSensor(CustomDevice):
    """XBee Sensor."""

    def remote_at(self, command, *args, **kwargs):
        """Remote at command."""
        if hasattr(self._application, 'remote_at_command'):
            return self._application.remote_at_command(
                self.nwk,
                command,
                *args,
                apply_changes=True,
                encryption=True,
                **kwargs
            )
        _LOGGER.warning("Remote At Command not supported by this coordinator")

    class DigitalIOCluster(CustomCluster, BinaryInput):
        """Digital IO Cluster for the XBee."""

        cluster_id = XBEE_IO_CLUSTER

        def handle_cluster_general_request(self, tsn, command_id, args):
            """Handle the cluster general request.

            Update the digital pin states
            """
            if command_id == ON_OFF_CMD:
                values = args[0]
                if 'digital_pins' in values and 'digital_samples' in values:
                    # Update digital inputs
                    active_pins = [i for i, x in enumerate(
                        values['digital_pins']) if x == 1]
                    for pin in active_pins:
                        # pylint: disable=W0212
                        self._endpoint.device.__getitem__(
                            ENDPOINT_MAP[pin]).__getattr__(
                                OnOff.ep_attribute)._update_attribute(
                                    ON_OFF_CMD, values['digital_samples'][pin])
            else:
                super().handle_cluster_general_request(tsn, command_id, args)

        def deserialize(self, tsn, frame_type, is_reply, command_id, data):
            """Deserialize."""
            if frame_type == 1:
                # Cluster command
                if is_reply:
                    commands = self.client_commands
                else:
                    commands = self.server_commands

                try:
                    schema = commands[command_id][1]
                    is_reply = commands[command_id][2]
                except KeyError:
                    data = struct.pack(
                        '>i',
                        tsn)[-1:] + struct.pack('>i', command_id)[-1:] + data
                    new_command_id = ON_OFF_CMD
                    try:
                        schema = commands[new_command_id][1]
                        is_reply = commands[new_command_id][2]
                    except KeyError:
                        _LOGGER.warning(
                            "Unknown cluster-specific command %s", command_id)
                        return tsn, command_id + 256, is_reply, data
                    value, data = t.deserialize(data, schema)
                    return tsn, new_command_id, is_reply, value
                # Bad hack to differentiate foundation vs cluster
                command_id = command_id + 256
            else:
                # General command
                try:
                    schema = foundation.COMMANDS[command_id][1]
                    is_reply = foundation.COMMANDS[command_id][2]
                except KeyError:
                    _LOGGER.warning(
                        "Unknown foundation command %s", command_id)
                    return tsn, command_id, is_reply, data

            value, data = t.deserialize(data, schema)
            if data != b'':
                _LOGGER.warning("Data remains after deserializing ZCL frame")
            return tsn, command_id, is_reply, value

        attributes = {0x0055: ('present_value', t.Bool)}
        client_commands = {
            0x0000: ('io_sample', (IOSample,), False),
        }
        server_commands = {
            0x0000: ('io_sample', (IOSample,), False),
        }

    signature = {
        232: {
            'profile_id': XBEE_PROFILE_ID,
            'device_type': zha.DeviceType.ON_OFF_SWITCH,
            'input_clusters': [
            ],
            'output_clusters': [
            ],
        },
        230: {
            'profile_id': XBEE_PROFILE_ID,
            'device_type': zha.DeviceType.ON_OFF_SWITCH,
            'input_clusters': [
            ],
            'output_clusters': [
            ],
        },
    }
    replacement = {
        'endpoints': {
            232: {
                'manufacturer': 'XBEE',
                'model': 'xbee.io',
                'input_clusters': [
                    DigitalIOCluster,
                ],
                'output_clusters': [
                ],
            },
            0xd0: {
                'manufacturer': 'XBEE',
                'model': 'AD0/DIO0/Commissioning',
                'device_type': zha.DeviceType.LEVEL_CONTROL_SWITCH,
                'profile_id': XBEE_PROFILE_ID,
                'input_clusters': [
                    XBeeOnOff,
                ],
                'output_clusters': [
                ],
            },
            0xd1: {
                'manufacturer': 'XBEE',
                'model': 'AD1/DIO1/SPI_nATTN',
                'device_type': zha.DeviceType.LEVEL_CONTROL_SWITCH,
                'profile_id': XBEE_PROFILE_ID,
                'input_clusters': [
                    XBeeOnOff,
                ],
                'output_clusters': [
                ],
            },
            0xd2: {
                'manufacturer': 'XBEE',
                'model': 'AD2/DIO2/SPI_CLK',
                'device_type': zha.DeviceType.LEVEL_CONTROL_SWITCH,
                'profile_id': XBEE_PROFILE_ID,
                'input_clusters': [
                    XBeeOnOff,
                ],
                'output_clusters': [
                ],
            },
            0xd3: {
                'manufacturer': 'XBEE',
                'model': 'AD3/DIO3',
                'device_type': zha.DeviceType.LEVEL_CONTROL_SWITCH,
                'profile_id': XBEE_PROFILE_ID,
                'input_clusters': [
                    XBeeOnOff,
                ],
                'output_clusters': [
                ],
            },
            0xd4: {
                'manufacturer': 'XBEE',
                'model': 'DIO4/SPI_MOSI',
                'device_type': zha.DeviceType.LEVEL_CONTROL_SWITCH,
                'profile_id': XBEE_PROFILE_ID,
                'input_clusters': [
                    XBeeOnOff,
                ],
                'output_clusters': [
                ],
            },
            0xd5: {
                'manufacturer': 'XBEE',
                'model': 'DIO5/Assoc',
                'device_type': zha.DeviceType.LEVEL_CONTROL_SWITCH,
                'profile_id': XBEE_PROFILE_ID,
                'input_clusters': [
                    XBeeOnOff,
                ],
                'output_clusters': [
                ],
            },
            0xda: {
                'manufacturer': 'XBEE',
                'model': 'DIO10/PWM0',
                'device_type': zha.DeviceType.LEVEL_CONTROL_SWITCH,
                'profile_id': XBEE_PROFILE_ID,
                'input_clusters': [
                    XBeeOnOff,
                ],
                'output_clusters': [
                ],
            },
            0xdb: {
                'manufacturer': 'XBEE',
                'model': 'DIO11/PWM1',
                'device_type': zha.DeviceType.LEVEL_CONTROL_SWITCH,
                'profile_id': XBEE_PROFILE_ID,
                'input_clusters': [
                    XBeeOnOff,
                ],
                'output_clusters': [
                ],
            },
            0xdc: {
                'manufacturer': 'XBEE',
                'model': 'DIO12/SPI_MISO',
                'device_type': zha.DeviceType.LEVEL_CONTROL_SWITCH,
                'profile_id': XBEE_PROFILE_ID,
                'input_clusters': [
                    XBeeOnOff,
                ],
                'output_clusters': [
                ],
            },
        },
    }
