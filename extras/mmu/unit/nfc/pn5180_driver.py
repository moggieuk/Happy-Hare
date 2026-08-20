# klippy/extras/mmu/unit/nfc/pn5180_driver.py
#
# PN5180 SPI reader driver for Happy Hare RFID Reader.
#
# This module deliberately owns only PN5180 transport and RF protocol work.
# Gate state, tag parsing, Spoolman, and Happy Hare dispatch remain in the
# shared NFC reader pipeline. Like the other drivers here it is built by
# reader_factory and exposes the common reader interface: init(), is_alive(),
# read_tag(), read_target(), plus the rich-read helpers used by a deep read
# (ntag_read_user_memory, mifare_read_authenticated_blocks,
# iso15693_read_user_memory) and _release_current_target().
#
# Wiring
# ──────
# PN5180 is SPI (mode 0) and needs two GPIOs beyond the SPI bus itself:
#   cs_pin     - SPI chip select (consumed by MCU_SPI_from_config)
#   busy_pin   - BUSY, active high; every transaction waits for it to go low
#   reset_pin  - RST, active low; pulsed for the hardware reset in init()
# Both extra pins are mandatory. BUSY replaces the fixed inter-command delay
# the other drivers rely on, and RST is the only way back from a wedged chip.
#
# Reactor / threading notes:
#   As with the other drivers, all methods run on the Klipper reactor thread and
#   are NOT safe to call from a background OS thread.  spi_transfer() waits for
#   the MCU response on a reactor completion, and _busy_asserted() queries BUSY
#   through the MCU the same way; both waits are greenlet-based (reactor.pause)
#   and so bound to the reactor thread.
#   No method waits for a tag to arrive - read_target() is single-shot and the
#   repeated polling lives in the manager's reactor timer.  The waits here are
#   bounded BUSY/IRQ polls (rf_poll_interval ticks, capped by busy_timeout /
#   rf_timeout) plus short fixed delays, all yielded through _sleep() so the
#   reactor keeps servicing timers and moves while a read is in progress.
#   Unlike the RC522 driver, sleep_fn defaults to reactor.pause here, so an
#   instance built without one is still reactor-friendly.

from .log import logger
from .rx_gain import RX_GAIN_CODES


SYSTEM_CONFIG = 0x00
IRQ_STATUS = 0x02
IRQ_CLEAR = 0x03
CRC_RX_CONFIG = 0x12
RX_STATUS = 0x13
CRC_TX_CONFIG = 0x19
RF_STATUS = 0x1D
RF_CONTROL_RX = 0x22

PRODUCT_VERSION = 0x10
FIRMWARE_VERSION = 0x12
EEPROM_VERSION = 0x14

CMD_WRITE_REGISTER = 0x00
CMD_WRITE_REGISTER_OR_MASK = 0x01
CMD_WRITE_REGISTER_AND_MASK = 0x02
CMD_READ_REGISTER = 0x04
CMD_READ_EEPROM = 0x07
CMD_SEND_DATA = 0x09
CMD_READ_DATA = 0x0A
CMD_MFC_AUTHENTICATE = 0x0C
CMD_LOAD_RF_CONFIG = 0x11
CMD_RF_ON = 0x16
CMD_RF_OFF = 0x17

RX_IRQ_STAT = 1 << 0
IDLE_IRQ_STAT = 1 << 2
TX_RFOFF_IRQ_STAT = 1 << 8
TX_RFON_IRQ_STAT = 1 << 9
GENERAL_ERROR_IRQ_STAT = 1 << 17

RX_BYTES_RECEIVED_MASK = 0x1FF
MAX_READ_DATA_LEN = 255
TRANSCEIVE_STATE_SHIFT = 24
TRANSCEIVE_STATE_MASK = 0x07
TRANSCEIVE_STATE_WAIT_TRANSMIT = 1
TRANSCEIVE_STATE_IDLE = 0

MIFARE_CMD_READ = 0x30
MIFARE_CMD_AUTH_A = 0x60
MIFARE_CMD_AUTH_B = 0x61

ISO15693_CMD_INVENTORY = 0x01
ISO15693_CMD_READ_SINGLE_BLOCK = 0x20
ISO15693_CMD_READ_MULTIPLE_BLOCKS = 0x23
ISO15693_FLAG_HIGH_DATA_RATE = 0x02
ISO15693_FLAG_INVENTORY = 0x04
ISO15693_FLAG_ADDRESS = 0x20
ISO15693_FLAG_ONE_SLOT = 0x20

DEFAULT_NTAG_START_PAGE = 4
DEFAULT_NTAG_END_PAGE = 67
DEFAULT_ISO15693_START_BLOCK = 0
DEFAULT_ISO15693_END_BLOCK = 79
RESET_SCHEDULE_DELAY = 0.100
RESET_LOW_TIME = 0.100
RESET_BOOT_DELAY = 0.200
DEFAULT_BUSY_TIMEOUT = 0.100


class PN5180Error(Exception):
    pass


class PN5180RFRecoveryRequired(PN5180Error):
    pass


class PN5180BusyTimeout(PN5180Error):
    pass


class PN5180TypeAIncomplete(PN5180Error):
    pass


class PN5180Core:
    """Low-level PN5180 commands and Type-2/Type-5 RF operations."""

    def __init__(self, config, spi, debug, sleep_fn=None):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.spi = spi
        self.mcu = spi.get_mcu()
        self.debug = debug
        self.rx_gain_code = None
        self._sleep_fn = sleep_fn or self._default_sleep

        self.rf_timeout = config.getfloat(
            'pn5180_rf_timeout', 0.050, above=0.0)
        self.rf_poll_interval = config.getfloat(
            'pn5180_rf_poll_interval', 0.001, minval=0.0001)
        self.busy_timeout = config.getfloat(
            'pn5180_busy_timeout', DEFAULT_BUSY_TIMEOUT, above=0.0)
        self.rf_on_delay = config.getfloat(
            'pn5180_rf_on_delay', 0.050, minval=0.0)
        self.page_read_retries = config.getint(
            'pn5180_page_read_retries', 2, minval=1, maxval=10)

        # RST is active low and is pulsed to recover a wedged chip.  BUSY is
        # active high and replaces the fixed command delay, so every SPI
        # transaction waits only until the PN5180 has actually completed.
        # Neither has a sensible default, so both are required options.
        pins = self.printer.lookup_object('pins')
        self.reset_pin = pins.setup_pin('digital_out', config.get('reset_pin'))
        self.reset_pin.setup_max_duration(0.0)
        self.reset_pin.setup_start_value(1, 1)
        self.busy_pin = pins.setup_pin('endstop', config.get('busy_pin'))

        self.initialized = False
        self.current_uid = None
        self.current_uid_lsb_first = None
        self.current_protocol = None
        self.firmware = None
        self.last_read_stats = {}

    def _default_sleep(self, duration):
        self.reactor.pause(self.reactor.monotonic() + duration)

    def _now(self):
        return self.reactor.monotonic()

    def _sleep(self, duration):
        if duration > 0.0:
            self._sleep_fn(duration)

    def _mcu_print_time(self, eventtime=None):
        return self.mcu.estimated_print_time(eventtime or self._now())

    def _busy_asserted(self):
        return bool(self.busy_pin.query_endstop(self._mcu_print_time()))

    def _wait_busy_low(self, phase, timeout=None):
        deadline = self._now() + (
            self.busy_timeout if timeout is None else timeout)
        while self._busy_asserted():
            if self._now() >= deadline:
                raise PN5180BusyTimeout(
                    'timeout waiting for PN5180 BUSY low (%s)' % phase)
            self._sleep(self.rf_poll_interval)

    def _transceive_command(self, send_data, recv_len=0):
        self._wait_busy_low('before command')
        self.spi.spi_send(list(send_data))
        self._wait_busy_low('after command')
        if not recv_len:
            return []
        self._wait_busy_low('before response')
        result = self.spi.spi_transfer([0xFF] * recv_len)
        self._wait_busy_low('after response')
        response = result.get('response') if isinstance(result, dict) else None
        if response is None:
            raise PN5180Error('SPI transfer returned no response')
        return list(bytearray(response))

    @staticmethod
    def _u32_to_le(value):
        return [(value >> shift) & 0xFF for shift in (0, 8, 16, 24)]

    @staticmethod
    def _le_to_u32(data):
        if len(data) != 4:
            raise PN5180Error('short register response')
        return sum((data[index] & 0xFF) << (8 * index)
                   for index in range(4))

    def write_register(self, reg, value):
        self._transceive_command(
            [CMD_WRITE_REGISTER, reg] + self._u32_to_le(value))

    def write_register_or_mask(self, reg, mask):
        self._transceive_command(
            [CMD_WRITE_REGISTER_OR_MASK, reg] + self._u32_to_le(mask))

    def write_register_and_mask(self, reg, mask):
        self._transceive_command(
            [CMD_WRITE_REGISTER_AND_MASK, reg] + self._u32_to_le(mask))

    def read_register(self, reg):
        return self._le_to_u32(
            self._transceive_command([CMD_READ_REGISTER, reg], 4))

    def read_eeprom(self, addr, length):
        return self._transceive_command([CMD_READ_EEPROM, addr, length], length)

    def clear_irq_status(self, mask=0xFFFFFFFF):
        self.write_register(IRQ_CLEAR, mask)

    def load_rf_config(self, tx_conf, rx_conf):
        self._transceive_command([CMD_LOAD_RF_CONFIG, tx_conf, rx_conf])
        # LOAD_RF_CONFIG replaces RF_CONTROL_RX, so a static override must be
        # restored for every protocol profile (Type A and ISO15693), not merely
        # after the initial hardware reset.
        if self.rx_gain_code is not None:
            self._write_rx_gain(self.rx_gain_code)

    def _write_rx_gain(self, code):
        value = (self.read_register(RF_CONTROL_RX) & ~0x03) | code
        self.write_register(RF_CONTROL_RX, value)
        return value

    def set_rx_gain(self, code):
        self.rx_gain_code = code
        return self._write_rx_gain(code)

    def _wait_irq(self, mask, timeout=None, raise_on_error=True):
        deadline = self._now() + (self.rf_timeout if timeout is None else timeout)
        while self._now() < deadline:
            irq = self.read_register(IRQ_STATUS)
            if irq & mask:
                return True
            if irq & GENERAL_ERROR_IRQ_STAT:
                if raise_on_error:
                    raise PN5180Error('PN5180 general error IRQ')
                return False
            self._sleep(self.rf_poll_interval)
        return False

    def _wait_transceive_state(self, expected, timeout=0.050):
        deadline = self._now() + timeout
        while self._now() < deadline:
            status = self.read_register(RF_STATUS)
            state = (status >> TRANSCEIVE_STATE_SHIFT) & TRANSCEIVE_STATE_MASK
            if state == expected:
                return True
            self._sleep(self.rf_poll_interval)
        return False

    def rf_on(self):
        self.clear_irq_status(TX_RFON_IRQ_STAT)
        self._transceive_command([CMD_RF_ON, 0x00])
        irq_seen = self._wait_irq(
            TX_RFON_IRQ_STAT, timeout=0.500, raise_on_error=False)
        self.clear_irq_status(TX_RFON_IRQ_STAT)
        self._sleep(self.rf_on_delay)
        return irq_seen

    def rf_off(self):
        self.clear_irq_status(TX_RFOFF_IRQ_STAT)
        self._transceive_command([CMD_RF_OFF, 0x00])
        self._wait_irq(TX_RFOFF_IRQ_STAT, timeout=0.500, raise_on_error=False)
        self.clear_irq_status(TX_RFOFF_IRQ_STAT)

    def send_data(self, data, valid_bits=0):
        if len(data) > 260:
            raise PN5180Error('PN5180 SEND_DATA payload too large')
        self.write_register_and_mask(SYSTEM_CONFIG, 0xFFFFFFF8)
        self.write_register_or_mask(SYSTEM_CONFIG, 0x00000003)
        self.clear_irq_status()
        if not self._wait_transceive_state(TRANSCEIVE_STATE_WAIT_TRANSMIT):
            raise PN5180Error(
                'PN5180 did not enter WaitTransmit before SEND_DATA')
        self._transceive_command([CMD_SEND_DATA, valid_bits] + list(data))

    def read_data(self, length):
        if length <= 0 or length > MAX_READ_DATA_LEN:
            raise PN5180Error('invalid READ_DATA length %d' % length)
        return self._transceive_command([CMD_READ_DATA, 0x00], length)

    def rx_bytes_received(self):
        return self.read_register(RX_STATUS) & RX_BYTES_RECEIVED_MASK

    @staticmethod
    def _suspicious_bytes(data):
        return (not data or all(byte == 0x00 for byte in data)
                or all(byte == 0xFF for byte in data))

    def _clear_target(self):
        self.current_uid = None
        self.current_uid_lsb_first = None
        self.current_protocol = None

    def hardware_reset(self):
        eventtime = self._now()
        print_time = self._mcu_print_time(eventtime) + RESET_SCHEDULE_DELAY
        self.reset_pin.set_digital(print_time, 0)
        self.reset_pin.set_digital(print_time + RESET_LOW_TIME, 1)
        self.reactor.pause(
            eventtime + RESET_SCHEDULE_DELAY + RESET_LOW_TIME + RESET_BOOT_DELAY)
        self._wait_irq(IDLE_IRQ_STAT, timeout=1.000, raise_on_error=False)
        self.clear_irq_status()

    def check_communication(self):
        product = self.read_eeprom(PRODUCT_VERSION, 2)
        firmware = self.read_eeprom(FIRMWARE_VERSION, 2)
        eeprom = self.read_eeprom(EEPROM_VERSION, 2)
        if (self._suspicious_bytes(product) or self._suspicious_bytes(firmware)
                or self._suspicious_bytes(eeprom)):
            raise PN5180Error(
                'invalid EEPROM response; check SPI MISO, CS, power, and reset')
        registers = [self.read_register(SYSTEM_CONFIG),
                     self.read_register(IRQ_STATUS),
                     self.read_register(RX_STATUS),
                     self.read_register(RF_STATUS)]
        if (all(value == 0x00000000 for value in registers)
                or all(value == 0xFFFFFFFF for value in registers)):
            raise PN5180Error(
                'invalid PN5180 health registers: %s' %
                ', '.join('0x%08X' % value for value in registers))
        self.firmware = list(firmware)
        return True

    def initialize(self):
        self.initialized = False
        self._clear_target()
        self.hardware_reset()
        self.check_communication()
        self.setup_type_a_rf()
        self.initialized = True
        return True

    def is_alive(self):
        try:
            return self.check_communication()
        except Exception:
            return False

    def recover_rf_state(self):
        try:
            self.rf_off()
        except Exception:
            pass
        self.write_register_and_mask(SYSTEM_CONFIG, 0xFFFFFFF8)
        self.clear_irq_status()
        self._clear_target()

    def setup_type_a_rf(self):
        try:
            self.rf_off()
        except Exception:
            pass
        self.write_register_and_mask(SYSTEM_CONFIG, 0xFFFFFFF8)
        self._wait_transceive_state(TRANSCEIVE_STATE_IDLE)
        self.clear_irq_status()
        self.load_rf_config(0x00, 0x80)
        if not self.rf_on():
            raise PN5180RFRecoveryRequired(
                'RF on transition timed out during Type-A setup')
        self.write_register_or_mask(SYSTEM_CONFIG, 0x00000003)

    def setup_iso15693_rf(self):
        try:
            self.rf_off()
        except Exception:
            pass
        self.write_register_and_mask(SYSTEM_CONFIG, 0xFFFFFFF8)
        self._wait_transceive_state(TRANSCEIVE_STATE_IDLE)
        self.clear_irq_status()
        self.load_rf_config(0x0D, 0x8D)
        if not self.rf_on():
            raise PN5180RFRecoveryRequired(
                'RF on transition timed out during ISO15693 setup')
        self.write_register_or_mask(SYSTEM_CONFIG, 0x00000003)

    @staticmethod
    def _bcc_ok(data):
        return len(data) == 5 and (data[0] ^ data[1] ^ data[2] ^ data[3]) == data[4]

    @staticmethod
    def _type_a_incomplete(stage):
        raise PN5180TypeAIncomplete(
            'Type-A activation incomplete at %s' % stage)

    def activate_type_a(self):
        self.setup_type_a_rf()
        self.write_register_and_mask(SYSTEM_CONFIG, 0xFFFFFFBF)
        self.write_register_and_mask(CRC_RX_CONFIG, 0xFFFFFFFE)
        self.write_register_and_mask(CRC_TX_CONFIG, 0xFFFFFFFE)
        self.send_data([0x52], valid_bits=0x07)
        if not self._wait_irq(RX_IRQ_STAT, raise_on_error=False):
            return None
        atqa = self.read_data(2)
        if len(atqa) != 2 or self._suspicious_bytes(atqa):
            self._type_a_incomplete('ATQA')

        self.send_data([0x93, 0x20])
        if not self._wait_irq(RX_IRQ_STAT, raise_on_error=False):
            self._type_a_incomplete('CL1 anticollision')
        cascade_1 = self.read_data(5)
        if not self._bcc_ok(cascade_1) or self._suspicious_bytes(cascade_1):
            self._type_a_incomplete('CL1 anticollision data')

        self.write_register_or_mask(CRC_RX_CONFIG, 0x01)
        self.write_register_or_mask(CRC_TX_CONFIG, 0x01)
        self.send_data([0x93, 0x70] + cascade_1)
        if not self._wait_irq(RX_IRQ_STAT, raise_on_error=False):
            self._type_a_incomplete('CL1 select')
        sak = self.read_data(1)
        if len(sak) != 1 or sak[0] in (0xFF, 0x7F, 0x80):
            self._type_a_incomplete('CL1 SAK')

        uid = cascade_1[1:4] if cascade_1[0] == 0x88 else cascade_1[:4]
        if cascade_1[0] == 0x88:
            self.write_register_and_mask(CRC_RX_CONFIG, 0xFFFFFFFE)
            self.write_register_and_mask(CRC_TX_CONFIG, 0xFFFFFFFE)
            self.send_data([0x95, 0x20])
            if not self._wait_irq(RX_IRQ_STAT, raise_on_error=False):
                self._type_a_incomplete('CL2 anticollision')
            cascade_2 = self.read_data(5)
            if not self._bcc_ok(cascade_2) or self._suspicious_bytes(cascade_2):
                self._type_a_incomplete('CL2 anticollision data')
            self.write_register_or_mask(CRC_RX_CONFIG, 0x01)
            self.write_register_or_mask(CRC_TX_CONFIG, 0x01)
            self.send_data([0x95, 0x70] + cascade_2)
            if not self._wait_irq(RX_IRQ_STAT, raise_on_error=False):
                self._type_a_incomplete('CL2 select')
            sak_2 = self.read_data(1)
            if len(sak_2) != 1:
                self._type_a_incomplete('CL2 SAK')
            sak = sak_2
            uid += cascade_2[:4]
        if self._suspicious_bytes(uid):
            self._type_a_incomplete('UID')
        self.current_uid = list(uid)
        self.current_protocol = 'iso14443a'
        return {'uid': list(uid), 'atqa_bytes': list(atqa), 'sak': sak[0]}

    def iso15693_inventory(self):
        self.setup_iso15693_rf()
        flags = (ISO15693_FLAG_HIGH_DATA_RATE | ISO15693_FLAG_INVENTORY
                 | ISO15693_FLAG_ONE_SLOT)
        self.send_data([flags, ISO15693_CMD_INVENTORY, 0x00])
        if not self._wait_irq(RX_IRQ_STAT, raise_on_error=False):
            return None
        length = self.rx_bytes_received()
        if length < 10 or length == RX_BYTES_RECEIVED_MASK:
            return None
        data = self.read_data(length)
        if len(data) < 10 or data[0] & 0x01:
            return None
        uid_lsb_first = data[2:10]
        uid = list(reversed(uid_lsb_first))
        self.current_uid = uid
        self.current_uid_lsb_first = list(uid_lsb_first)
        self.current_protocol = 'iso15693_type5'
        return {'uid': uid, 'uid_lsb_first': list(uid_lsb_first),
                'dsfid': data[1]}

    def iso15693_read_single_block(self, block):
        self.send_data([ISO15693_FLAG_HIGH_DATA_RATE,
                        ISO15693_CMD_READ_SINGLE_BLOCK, block & 0xFF])
        if not self._wait_irq(RX_IRQ_STAT, raise_on_error=False):
            raise PN5180Error('timeout waiting ISO15693 block %d' % block)
        data = self.read_data(self.rx_bytes_received())
        if not data or data[0] & 0x01:
            raise PN5180Error('ISO15693 block %d read failed' % block)
        return bytearray(data[1:])

    def iso15693_read_multiple_blocks(self, start_block, block_count):
        if not self.current_uid_lsb_first:
            raise PN5180Error('ISO15693 addressed read requires a selected UID')
        if block_count < 1 or block_count > 16:
            raise PN5180Error('invalid ISO15693 block count %d' % block_count)
        self.send_data(
            [ISO15693_FLAG_HIGH_DATA_RATE | ISO15693_FLAG_ADDRESS,
             ISO15693_CMD_READ_MULTIPLE_BLOCKS]
            + self.current_uid_lsb_first
            + [start_block & 0xFF, (block_count - 1) & 0xFF])
        if not self._wait_irq(RX_IRQ_STAT, raise_on_error=False):
            raise PN5180Error('timeout waiting ISO15693 blocks')
        data = self.read_data(self.rx_bytes_received())
        expected_length = 1 + 4 * block_count
        if len(data) != expected_length or data[0] & 0x01:
            raise PN5180Error('ISO15693 multiple block read failed')
        return bytearray(data[1:])

    def iso15693_read_user_memory(self, start_block=DEFAULT_ISO15693_START_BLOCK,
                                  end_block=DEFAULT_ISO15693_END_BLOCK,
                                  batch_size=8):
        result = bytearray()
        batch_size = max(1, min(int(batch_size), 16))
        block = start_block
        while block <= end_block:
            count = min(batch_size, end_block - block + 1)
            data = (self.iso15693_read_multiple_blocks(block, count)
                    if count > 1 else self.iso15693_read_single_block(block))
            result.extend(data)
            if not data or all(value == 0x00 for value in data):
                break
            expected_length = self._expected_tlv_total_length(result)
            if expected_length is not None and len(result) >= expected_length:
                return result[:expected_length]
            block += count
        return result

    def ntag_read_page(self, page):
        last_error = None
        for attempt in range(self.page_read_retries):
            try:
                self.send_data([MIFARE_CMD_READ, page & 0xFF])
                if not self._wait_irq(RX_IRQ_STAT, raise_on_error=False):
                    raise PN5180Error('timeout waiting NTAG page %d' % page)
                data = self.read_data(self.rx_bytes_received())
                if len(data) != 16:
                    raise PN5180Error('invalid NTAG page %d response' % page)
                return bytearray(data)
            except Exception as error:
                last_error = error
                if attempt + 1 < self.page_read_retries:
                    self._sleep(0.010)
                    if self.activate_type_a() is None:
                        break
        raise PN5180Error('NTAG page %d failed: %s' % (page, last_error))

    def ntag_read_user_memory(self, start_page=DEFAULT_NTAG_START_PAGE,
                              end_page=DEFAULT_NTAG_END_PAGE):
        result = bytearray()
        page = start_page
        while page <= end_page:
            data = self.ntag_read_page(page)
            remaining = end_page - page + 1
            copied = data[:min(remaining, 4) * 4]
            result.extend(copied)
            expected_length = self._expected_tlv_total_length(result)
            if expected_length is not None and len(result) >= expected_length:
                return result[:expected_length]
            if copied and all(value == 0x00 for value in copied):
                break
            page += 4
        return result

    def mifare_authenticate(self, block_addr, key, uid_bytes,
                            use_key_b=False):
        """Authenticate a MIFARE Classic sector with the PN5180 Crypto1 engine."""
        key = list(key or [])
        uid = list(uid_bytes or [])
        if len(key) != 6 or len(uid) < 4:
            return False
        auth_cmd = MIFARE_CMD_AUTH_B if use_key_b else MIFARE_CMD_AUTH_A
        response = self._transceive_command(
            [CMD_MFC_AUTHENTICATE] + key + [auth_cmd, block_addr & 0xFF]
            + uid[-4:], 1)
        if len(response) == 1 and response[0] == 0x00:
            return True

        # A rejected Crypto1 handshake leaves the RF state unsuitable for the
        # next plain or encrypted frame. Clear it before trying another sector.
        self.write_register_and_mask(SYSTEM_CONFIG, 0xFFFFFFBF)
        self.write_register_and_mask(SYSTEM_CONFIG, 0xFFFFFFF8)
        self.clear_irq_status()
        return False

    def mifare_read_block(self, block_addr):
        """Read one authenticated MIFARE Classic data block."""
        self.send_data([MIFARE_CMD_READ, block_addr & 0xFF])
        deadline = self._now() + self.rf_timeout
        length = 0
        while self._now() < deadline:
            length = self.rx_bytes_received()
            if length:
                break
            self._sleep(self.rf_poll_interval)
        if length != 16:
            return None
        data = self.read_data(length)
        return bytes(data) if len(data) == 16 else None

    @staticmethod
    def _expected_tlv_total_length(data):
        data = bytes(data)
        offsets = [0]
        if len(data) >= 4 and data[0] in (0xE1, 0xE2):
            offsets.insert(0, 4)
        for offset in offsets:
            position = offset
            while position < len(data):
                tlv_type = data[position]
                position += 1
                if tlv_type == 0x00:
                    continue
                if tlv_type == 0xFE:
                    return position
                if position >= len(data):
                    break
                length = data[position]
                position += 1
                if length == 0xFF:
                    if position + 2 > len(data):
                        break
                    length = (data[position] << 8) | data[position + 1]
                    position += 2
                end = position + length
                if tlv_type == 0x03:
                    return end + 1 if end < len(data) and data[end] == 0xFE else end
                position = end
        return None


class PN5180Driver:
    """Adapter exposing PN5180 through the NFC reader driver contract."""

    def __init__(self, config, spi, name, debug=2, sleep_fn=None):
        self._name = name
        self._debug = debug
        self._format = str(config.get('pn5180_tag_format', 'auto')).strip().lower()
        if self._format not in ('auto', 'ntag', 'iso15693'):
            raise config.error(
                "pn5180_tag_format must be 'auto', 'ntag', or 'iso15693'")
        self._blocks_per_read = config.getint(
            'pn5180_iso15693_blocks_per_read', 8, minval=1, maxval=16)
        self._core = PN5180Core(config, spi, debug, sleep_fn=sleep_fn)
        self.current_target_info = None
        self.current_uid = None

    def init(self):
        # initialize() raises on failure, so reaching the log means success
        self._core.initialize()
        firmware = self._core.firmware or []
        logger.info('[%s pn5180] init OK firmware=%s', self._name,
                    '.'.join(str(value) for value in reversed(firmware)) or 'unknown')

    def is_alive(self):
        return self._core.initialized and self._core.is_alive()

    def set_rx_gain(self, db):
        """Set and retain RF_CONTROL_RX.RX_GAIN across protocol loads."""
        code = RX_GAIN_CODES['pn5180'].get(db)
        if code is None:
            return False
        value = self._core.set_rx_gain(code)
        logger.info('[%s pn5180] rx_gain set to %d dB (RF_CONTROL_RX=0x%08X)',
                    self._name, db, value)
        return True

    def _clear_current_card(self):
        self.current_target_info = None
        self.current_uid = None
        self._core._clear_target()

    def _reset_after_rf_fault(self, error):
        logger.warning(
            '[%s pn5180] transport/RF fault (%s); forcing reset',
            self._name, error)
        self._clear_current_card()
        try:
            self._core.initialize()
        except Exception as reset_error:
            logger.warning('[%s pn5180] RF reset failed: %s',
                           self._name, reset_error)
        return None

    def _release_current_target(self, reason='manual'):
        try:
            self._core.recover_rf_state()
        except PN5180BusyTimeout as error:
            self._reset_after_rf_fault(error)
            return
        except Exception as error:
            if self._debug >= 4:
                logger.info('[%s pn5180] release failed (%s): %s',
                            self._name, reason, error)
        self._clear_current_card()

    def _set_current_card(self, target):
        self.current_target_info = target
        self.current_uid = list(target.get('uid_bytes') or [])

    @staticmethod
    def _hex(uid_bytes):
        return ''.join('%02X' % (value & 0xFF) for value in uid_bytes)

    def _type_a_target(self):
        tag = self._core.activate_type_a()
        if tag is None:
            return None
        uid = list(tag['uid'])
        sak = int(tag['sak']) & 0xFF
        if sak in (0xFF, 0x7F, 0x80):
            return None
        if sak & 0x08:
            protocol = 'mifare_classic'
            protocol_name = 'ISO14443A_MIFARE_CLASSIC'
        elif sak == 0x00:
            protocol = 'iso14443a'
            protocol_name = 'ISO14443A'
        else:
            protocol = 'uid_only'
            protocol_name = 'ISO14443A_UID_ONLY'
        atqa_bytes = list(tag.get('atqa_bytes') or [])
        return {
            'reader': 'pn5180', 'protocol': protocol,
            'protocol_name': protocol_name, 'target': 1, 'tg': 1,
            'uid': self._hex(uid), 'uid_bytes': uid, 'uid_length': len(uid),
            'sak': sak,
            'atqa': ((atqa_bytes[0] << 8) | atqa_bytes[1]
                     if len(atqa_bytes) == 2 else 0),
            'atqa_bytes': atqa_bytes,
        }

    def _type_5_target(self):
        tag = self._core.iso15693_inventory()
        if tag is None:
            return None
        uid = list(tag['uid'])
        return {
            'reader': 'pn5180', 'protocol': 'iso15693_type5',
            'protocol_name': 'ISO15693', 'target': 1, 'tg': 1,
            'uid': self._hex(uid), 'uid_bytes': uid, 'uid_length': len(uid),
            'uid_lsb_first': list(tag['uid_lsb_first']), 'dsfid': tag.get('dsfid'),
        }

    def _read_target_once(self):
        if not self._core.initialized:
            return None
        self._clear_current_card()
        protocols = ('ntag', 'iso15693') if self._format == 'auto' else (self._format,)
        for protocol in protocols:
            target = self._type_a_target() if protocol == 'ntag' else self._type_5_target()
            if target is not None:
                self._set_current_card(target)
                return target
        return None

    def read_target(self, timeout=None):
        original_timeout = self._core.rf_timeout
        if timeout is not None:
            self._core.rf_timeout = min(
                original_timeout, max(0.001, float(timeout)))
        try:
            try:
                return self._read_target_once()
            except PN5180TypeAIncomplete:
                self._clear_current_card()
                return None
            except PN5180BusyTimeout as error:
                return self._reset_after_rf_fault(error)
            except PN5180RFRecoveryRequired as error:
                return self._reset_after_rf_fault(error)
            except Exception as error:
                if self._debug >= 3:
                    # info, not warning: this falls through to the recovery read
                    # below, which warns for itself if it cannot recover. A warning
                    # that only appears at debug 3 is neither a warning nor a trace.
                    logger.info('[%s pn5180] target read failed: %s',
                                self._name, error)
                self._clear_current_card()

            # A no-tag response is normal and never reaches this path. For an
            # actual transport/RF error, reset RF first and only pulse RST when
            # the PN5180 health check confirms communication was lost.
            try:
                self._core.recover_rf_state()
                if not self._core.is_alive():
                    self._core.initialize()
                return self._read_target_once()
            except Exception as recovery_error:
                logger.warning(
                    '[%s pn5180] recovery read failed: %s',
                    self._name, recovery_error)
                self._clear_current_card()
                return None
        finally:
            self._core.rf_timeout = original_timeout

    # ─────────────────────────────────────────────────────────────────────────
    # Non-blocking presence probe (homing) - deliberately NOT implemented
    # ─────────────────────────────────────────────────────────────────────────
    #
    # PN532/RC522/PN7160 expose probe_start/probe_poll/probe_stop so a homing move
    # can tick a presence check without blocking the reactor. This driver does not,
    # so MmuNfcReader falls back to its blocking shim: one bounded read_target()
    # per (slower) tick, with no per-tick release.
    #
    # Why not here: _read_target_once() walks a tuple of protocols ('ntag',
    # 'iso15693'), each a complete RF activation sequence gated on busy_pin
    # transitions via _wait_busy_low(). Splitting that into a resumable state
    # machine means restructuring both protocol paths and the busy handshake - a
    # much larger change than the PN532/RC522 splits, and one that can't be
    # validated without the hardware in hand.
    #
    # The shim is a reasonable stand-in here: rf_timeout already defaults to 0.050,
    # so a tick is bounded at roughly that, and dropping the per-tick release
    # removes the bulk of the old cost. If this reader is used for tag homing and
    # accuracy matters, implementing the contract here is the remaining work -
    # _busy_asserted() is already a non-blocking readiness predicate to build on.

    def read_tag(self, timeout=None):
        target = self.read_target(timeout=timeout)
        if target is None:
            return None
        uid = target.get('uid')
        self._release_current_target(reason='uid_read_complete')
        return uid

    def _ensure_target(self, protocol):
        if (self.current_target_info is not None
                and self.current_target_info.get('protocol') == protocol):
            return self.current_target_info
        target = self.read_target()
        if target is None or target.get('protocol') != protocol:
            raise PN5180Error('no active PN5180 %s target' % protocol)
        return target

    def ntag_read_user_memory(self, start_page=4, end_page=67):
        try:
            self._ensure_target('iso14443a')
            return self._core.ntag_read_user_memory(start_page, end_page)
        finally:
            self._release_current_target(reason='ntag_user_memory_complete')

    def mifare_read_authenticated_blocks(self, sector_keys, sectors,
                                         uid_bytes=None, use_key_b=False):
        """Read authenticated MIFARE Classic blocks for the shared tag parser."""
        blocks = {}
        auth_failed_sectors = []
        read_failed_blocks = []
        try:
            self._ensure_target('mifare_classic')
            uid = list(uid_bytes or self.current_uid or [])
            if len(uid) < 4:
                return {
                    'uid_bytes': bytes(uid), 'blocks': blocks,
                    'auth_failed_sectors': list(sectors),
                }
            for sector in sectors:
                auth_block = sector * 4
                key = sector_keys[sector] if sector < len(sector_keys) else None
                if key is None:
                    continue
                if not self._core.mifare_authenticate(
                        auth_block, key, uid, use_key_b=use_key_b):
                    auth_failed_sectors.append(sector)
                    if self._debug >= 3:
                        logger.info(
                            '[%s pn5180] MIFARE sector %d auth failed',
                            self._name, sector)
                    continue
                for block_offset in range(3):
                    block_addr = sector * 4 + block_offset
                    data = self._core.mifare_read_block(block_addr)
                    if data is not None:
                        blocks[block_addr] = data
                    else:
                        read_failed_blocks.append(block_addr)
                        if self._debug >= 3:
                            logger.info(
                                '[%s pn5180] MIFARE block %d read failed',
                                self._name, block_addr)
            result = {'uid_bytes': bytes(uid), 'blocks': blocks}
            if auth_failed_sectors:
                result['auth_failed_sectors'] = auth_failed_sectors
            if read_failed_blocks:
                result['read_failed_blocks'] = read_failed_blocks
            return result
        finally:
            self._release_current_target(reason='mifare_read_complete')

    def iso15693_read_user_memory(self, tag=None, start_block=0,
                                  end_block=DEFAULT_ISO15693_END_BLOCK,
                                  batch_size=None):
        try:
            self._ensure_target('iso15693_type5')
            return self._core.iso15693_read_user_memory(
                start_block=start_block, end_block=end_block,
                batch_size=self._blocks_per_read if batch_size is None else batch_size)
        finally:
            self._release_current_target(reason='iso15693_user_memory_complete')
