# Status-checked I2C transfers for the NFC reader drivers.
#
# Klipper's 'i2c_transfer' MCU command reports i2c_bus_status instead of shutting
# the MCU down on a NACK, but bus.MCU_I2C's own i2c_read()/i2c_write()/i2c_transfer()
# wrappers still invoke_shutdown() on any non-SUCCESS status. So a driver that wants
# to survive a NACK has to send the raw command and read the status itself.
#
# i2c_transfer_cmd is only set in MCU_I2C.build_config(), after the driver has been
# constructed, and stays None when the MCU firmware predates the command. Check
# status_supported() at call time, never once in __init__.
#
# Only the supported path lives here. Each driver keeps its own fallback because
# they deliberately differ (see pn7160_driver._i2c_transfer_safe).
#
# This file may be distributed under the terms of the GNU GPLv3 license.


class I2CStatusError(Exception):
    def __init__(self, status, response=None, label=None):
        self.status = status
        self.response = [] if response is None else response
        self.label = label
        label_text = "" if label is None else " label=%s" % (label,)
        Exception.__init__(
            self, "I2C%s status=%s response=%s"
            % (label_text, status,
               ' '.join("%02X" % (b & 0xff,) for b in self.response)))


def status_supported(i2c):
    """True if the MCU can report an I2C NACK instead of shutting down on one."""
    return getattr(i2c, "i2c_transfer_cmd", None) is not None


def transfer_checked(i2c, write, read_len, label=None, error_cls=I2CStatusError):
    """Send one i2c_transfer and return (status, response). read_len=0 is a write.

    Raises error_cls on any non-SUCCESS bus status. Only call when
    status_supported(i2c) is True.
    """
    params = i2c.i2c_transfer_cmd.send(
        [i2c.oid, list(write), read_len], retry=False)
    status = params.get("i2c_bus_status", "SUCCESS")
    response = list(bytearray(params.get("response", [])))
    if status != "SUCCESS":
        raise error_cls(status, response, label=label)
    return status, response
