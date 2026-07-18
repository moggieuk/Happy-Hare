# PN7160 reader driver for Happy Hare RFID Reader.
#
# This is not the standalone [pn7160] Klipper plugin.  It adapts the PN7160
# NCI controller flow to the small reader interface used by nfc_manager:
# init(), is_alive(), read_target(), read_tag(), and target cleanup helpers.
#
# PN7160 uses the same reader-facing API as PN532 where possible.  The driver
# owns NCI and raw tag commands; tag_handler owns retry windows, payload parsing,
# Spoolman lookups, and Happy Hare side effects.

from .log import logger


PN7160_I2C_ADDRESS = 0x28

NCI_CORE_RESET_CLEAR_CONFIG_CMD = [0x20, 0x00, 0x01, 0x01]
NCI_CORE_RESET_KEEP_CONFIG_CMD = [0x20, 0x00, 0x01, 0x00]
NCI_CORE_INIT_CMD_PN7160 = [0x20, 0x01, 0x02, 0x00, 0x00]
NCI_RF_DISCOVER_MAP_RW_CMD = [
    0x21, 0x00, 0x13, 0x06,
    0x01, 0x01, 0x01,
    0x02, 0x01, 0x01,
    0x03, 0x01, 0x01,
    0x04, 0x01, 0x02,
    0x06, 0x01, 0x01,
    0x80, 0x01, 0x80,
]
NCI_RF_DISCOVER_NFCA_NFCV_CMD = [
    0x21, 0x03, 0x05, 0x02,
    0x00, 0x01,  # NFC-A poll
    0x06, 0x01,  # NFC-V / ISO15693 poll
]
NCI_RF_DEACTIVATE_IDLE_CMD = [0x21, 0x06, 0x01, 0x00]
PN7160_RF_DEACTIVATE_GUARD_TIME = 0.025

NCI_GID_CORE = 0x00
NCI_GID_RF = 0x01
NCI_MT_DATA = 0x00
NCI_MT_RSP = 0x02
NCI_MT_NTF = 0x03
NCI_CORE_CONN_CREDITS_OID = 0x06

NCI_PROT_ISODEP = 0x04
NCI_PROT_ISO15693 = 0x06
NCI_PROT_MIFARE = 0x80
NCI_INTF_FRAME = 0x01
NCI_INTF_ISODEP = 0x02
NCI_INTF_TAGCMD = 0x80

NCI_MODE_PASSIVE_NFCA = 0x00
NCI_MODE_PASSIVE_NFCV = 0x06

NCI_MAX_FRAME = 64

NCI_STATUS_OK = 0x00
NCI_STATUS_DISCOVERY_ALREADY_STARTED = 0xA0
NCI_STATUS_DISCOVERY_TARGET_ACTIVATION_FAILED = 0xA1
NCI_STATUS_DISCOVERY_TEAR_DOWN = 0xA2

MIFARE_CMD_READ = 0x30
MIFARE_EXT_RAW_DATA = 0x10
MIFARE_EXT_AUTH = 0x40
MIFARE_EXT_AUTH_EMBEDDED_KEY = 0x10
MIFARE_EXT_AUTH_KEY_B = 0x80

ISO15693_CMD_READ_SINGLE_BLOCK = 0x20
ISO15693_CMD_READ_MULTIPLE_BLOCKS = 0x23
ISO15693_FLAG_HIGH_DATA_RATE = 0x02
ISO15693_FLAG_ADDRESS = 0x20
ISO15693_DEFAULT_START_BLOCK = 0
ISO15693_DEFAULT_END_BLOCK = 79
ISO15693_DEFAULT_BLOCKS_PER_READ = 4


class PN7160Error(Exception):
    pass


class PN7160NoTag(PN7160Error):
    pass


class PN7160I2CStatusError(PN7160Error):
    def __init__(self, status, response=None, label=None):
        self.status = status
        self.response = [] if response is None else response
        self.label = label
        label_text = "" if label is None else " label=%s" % (label,)
        PN7160Error.__init__(
            self, "I2C%s status=%s response=%s"
            % (label_text, status, _hex(self.response)))


def _hex(data, sep=' '):
    return sep.join("%02X" % (b & 0xff,) for b in data)


def _message_type(frame):
    return (frame[0] >> 5) & 0x07


def _gid(frame):
    return frame[0] & 0x0f


def _oid(frame):
    return frame[1] & 0x3f


def _oid_name(gid, oid):
    names = {
        (NCI_GID_CORE, 0x00): "CORE_RESET",
        (NCI_GID_CORE, 0x01): "CORE_INIT",
        (NCI_GID_RF, 0x00): "RF_DISCOVER_MAP",
        (NCI_GID_RF, 0x03): "RF_DISCOVER",
        (NCI_GID_RF, 0x04): "RF_DISCOVER_SELECT",
        (NCI_GID_RF, 0x05): "RF_INTF_ACTIVATED",
        (NCI_GID_RF, 0x06): "RF_DEACTIVATE",
    }
    return names.get((gid, oid), "OID0x%02X" % (oid,))


def _status_name(status):
    names = {
        NCI_STATUS_OK: "OK",
        0x01: "REJECTED",
        0x03: "FAILED",
        0x04: "NOT_INITIALIZED",
        0x05: "SYNTAX_ERROR",
        0x06: "SEMANTIC_ERROR",
        0x09: "INVALID_PARAM",
        0x0A: "MESSAGE_SIZE_EXCEEDED",
        NCI_STATUS_DISCOVERY_ALREADY_STARTED: "DISCOVERY_ALREADY_STARTED",
        NCI_STATUS_DISCOVERY_TARGET_ACTIVATION_FAILED:
            "DISCOVERY_TARGET_ACTIVATION_FAILED",
        NCI_STATUS_DISCOVERY_TEAR_DOWN: "DISCOVERY_TEAR_DOWN",
    }
    return names.get(status, "0x%02X" % (status,))


def _frame_summary(frame):
    if len(frame) < 3:
        return "short len=%d data=%s" % (len(frame), _hex(frame))
    text = "%s len=%d payload_len=%d raw=%s" % (
        _oid_name(_gid(frame), _oid(frame)), len(frame), frame[2], _hex(frame))
    if _message_type(frame) == NCI_MT_RSP and len(frame) >= 4:
        text += " status=%s" % (_status_name(frame[3]),)
    return text


class PN7160Handler:
    """Minimal PN7160 NCI state machine.

    PN7160 is an NFC Controller Interface (NCI) device.  Unlike PN532, it is
    not a simple command/ACK module, so reads are driven by NCI response and
    notification frames.  The handler owns those protocol details; the public
    PN7160Driver below converts successful activations into Happy Hare
    target_info dictionaries.
    """

    def __init__(self, config, i2c, ven_pin=None, irq_pin=None,
                 response_delay=0.020, nci_poll_interval=0.250,
                 read_timeout=0.500, raw_log=False, debug=False,
                 ven_pre_high_time=0.010,
                 ven_low_time=0.010, ven_post_high_time=0.100,
                 init_retries=3, init_retry_delay=0.500,
                 no_irq_read_delay=0.100,
                 ntag_data_delay=0.005,
                 ntag_read_retries=2,
                 ntag_retry_delay=0.025):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.i2c = i2c
        self.ven = None
        self.irq_enabled = False
        self.irq_state = None
        self.irq_event_time = None
        self.response_delay = response_delay
        self.nci_poll_interval = nci_poll_interval
        self.read_timeout = read_timeout
        self.raw_log = raw_log
        self.debug = debug
        self.ven_pre_high_time = ven_pre_high_time
        self.ven_low_time = ven_low_time
        self.ven_post_high_time = ven_post_high_time
        self.init_retries = init_retries
        self.init_retry_delay = init_retry_delay
        self.no_irq_read_delay = no_irq_read_delay
        self.ntag_data_delay = ntag_data_delay
        self.ntag_read_retries = ntag_read_retries
        self.ntag_retry_delay = ntag_retry_delay
        self.initialized = False
        self.core_info_lines = []
        try:
            self.i2c_address = i2c.get_i2c_address()
        except Exception:
            self.i2c_address = PN7160_I2C_ADDRESS

        if ven_pin:
            ppins = self.printer.lookup_object("pins")
            self.ven = ppins.setup_pin("digital_out", ven_pin)
            self.ven.setup_start_value(1, 1)
            self.ven.setup_max_duration(0.0)

        if irq_pin:
            try:
                buttons = self.printer.load_object(config, "buttons")
                buttons.register_buttons([irq_pin], self._irq_callback)
                self.irq_enabled = True
                logger.info("PN7160 IRQ pin '%s' registered", irq_pin)
            except Exception as e:
                logger.exception(
                    "PN7160 IRQ registration failed; falling back to delays")
                logger.error("PN7160 IRQ registration error details: %s", e)

        self._debug(
            "handler ready: addr=0x%02X irq=%s ven=%s no_irq=%s"
            " response_delay=%.3f nci_poll_interval=%.3f"
            " read_timeout=%.3f no_irq_read_delay=%.3f"
            " ntag_data_delay=%.3f ntag_read_retries=%d"
            " ntag_retry_delay=%.3f"
            % (self.i2c_address, self.irq_enabled, self.ven is not None,
               self.no_irq_mode, self.response_delay, self.nci_poll_interval,
               self.read_timeout, self.no_irq_read_delay,
               self.ntag_data_delay, self.ntag_read_retries,
               self.ntag_retry_delay))

    @property
    def no_irq_mode(self):
        return not self.irq_enabled

    def _debug(self, msg):
        if self.debug:
            logger.info("PN7160 debug: %s", msg)

    def _core_info(self, msg):
        self.core_info_lines.append("PN7160 info: " + msg)
        logger.info("PN7160 info: %s", msg)

    def _pause(self, seconds):
        self.reactor.pause(self.reactor.monotonic() + seconds)

    def _irq_callback(self, eventtime, state):
        prev_state = self.irq_state
        self.irq_state = state
        if state == 1 and (prev_state == 0 or prev_state is None):
            self.irq_event_time = eventtime
            self._debug("IRQ rising edge at %.6f" % eventtime)

    def _wait_for_irq(self, start_time, timeout, accept_current=True):
        if not self.irq_enabled:
            return False
        end_time = self.reactor.monotonic() + timeout
        while self.reactor.monotonic() < end_time:
            if accept_current and self.irq_state:
                return True
            if (self.irq_event_time is not None
                    and self.irq_event_time >= start_time):
                return True
            self._pause(min(self.response_delay, 0.010))
        return False

    def _wait_for_irq_release(self, start_time, timeout=0.050):
        if not self.irq_enabled:
            return False
        end_time = self.reactor.monotonic() + timeout
        while self.reactor.monotonic() < end_time:
            if self.irq_state == 0:
                return True
            self._pause(min(self.response_delay, 0.010))
        return False

    def _log_frame(self, direction, frame):
        if self.raw_log:
            logger.info("PN7160 %s %s", direction, _hex(frame))

    def hardware_reset(self):
        if self.ven is None:
            self._debug("hardware reset skipped: no ven_pin configured")
            return
        mcu = self.ven.get_mcu()
        schedule_margin = 0.050
        start_time = mcu.estimated_print_time(
            self.reactor.monotonic()) + schedule_margin
        low_time = start_time + self.ven_pre_high_time
        high_time = low_time + self.ven_low_time
        self.ven.set_digital(start_time, 1)
        self.ven.set_digital(low_time, 0)
        self.ven.set_digital(high_time, 1)
        self._pause(schedule_margin + self.ven_pre_high_time
                    + self.ven_low_time + self.ven_post_high_time)
        self.initialized = False

    def _i2c_write_safe(self, data, label=None):
        if getattr(self.i2c, "i2c_transfer_cmd", None) is None:
            try:
                self.i2c.i2c_write(data, retry=False)
            except TypeError:
                self.i2c.i2c_write(data)
            return
        params = self.i2c.i2c_transfer_cmd.send(
            [self.i2c.oid, data, 0], retry=False)
        status = params.get("i2c_bus_status", "SUCCESS")
        response = list(bytearray(params.get("response", [])))
        if status != "SUCCESS":
            raise PN7160I2CStatusError(status, response, label=label)

    def _i2c_transfer_safe(self, write, read_len, label=None):
        if getattr(self.i2c, "i2c_transfer_cmd", None) is None:
            params = self.i2c.i2c_read(write, read_len, retry=False)
            return "SUCCESS", list(bytearray(params.get("response", [])))
        params = self.i2c.i2c_transfer_cmd.send(
            [self.i2c.oid, write, read_len], retry=False)
        status = params.get("i2c_bus_status", "SUCCESS")
        response = list(bytearray(params.get("response", [])))
        if status != "SUCCESS":
            raise PN7160I2CStatusError(status, response, label=label)
        return status, response

    def write_frame(self, frame, label=None):
        self._debug("write %s len=%d data=%s"
                    % (label or "NCI", len(frame), _hex(frame)))
        self._log_frame(">>", frame)
        self._i2c_write_safe(frame, label=label)

    def _read_exact(self, length, label=None):
        status, data = self._i2c_transfer_safe([], length, label=label)
        if len(data) != length:
            raise PN7160Error(
                "short I2C read: expected %d bytes, got %d"
                % (length, len(data)))
        return data

    def read_frame_once(self):
        read_start = self.reactor.monotonic()
        header = self._read_exact(3, label="nci_header")
        payload_len = header[2]
        if payload_len > NCI_MAX_FRAME - 3:
            raise PN7160Error("NCI payload too large: %d" % payload_len)
        payload = self._read_exact(payload_len, label="nci_payload") if payload_len else []
        frame = header + payload
        self._debug("read %s" % _frame_summary(frame))
        self._log_frame("<<", frame)
        self._wait_for_irq_release(read_start)
        return frame

    def read_optional_frame(self, timeout=0.050):
        if self.irq_enabled:
            start_time = self.reactor.monotonic()
            if not self._wait_for_irq(start_time, timeout):
                return None
            return self.read_frame_once()
        return self.read_frame_once()

    def wait_frame(self, timeout=None, poll_interval=None, irq_after=None,
                   accept_current_irq=True, label=None):
        timeout = self.read_timeout if timeout is None else timeout
        poll_interval = (self.nci_poll_interval if poll_interval is None
                         else poll_interval)
        start_time = (self.reactor.monotonic() if irq_after is None
                      else irq_after)
        end_time = start_time + timeout
        last_error = None
        attempts = 0
        while self.reactor.monotonic() < end_time:
            if self.irq_enabled:
                remaining = max(0.0, end_time - self.reactor.monotonic())
                if not self._wait_for_irq(
                        start_time, min(poll_interval, remaining),
                        accept_current=accept_current_irq):
                    continue
            else:
                delay = min(self.no_irq_read_delay,
                            max(0.0, end_time - self.reactor.monotonic()))
                if delay > 0.0:
                    self._pause(delay)
            try:
                attempts += 1
                return self.read_frame_once()
            except Exception as e:
                last_error = e
                self._debug("wait frame attempt %d failed: %s"
                            % (attempts, e))
                self._pause(poll_interval)
        raise PN7160Error("timeout waiting for NCI frame%s: %s"
                          % ("" if label is None else " " + label,
                             last_error))

    def command(self, frame, expected_gid, expected_oid, timeout=1.0,
                allow_extra_ntf=True, allowed_statuses=None):
        if allowed_statuses is None:
            allowed_statuses = (NCI_STATUS_OK,)
        label = _oid_name(expected_gid, expected_oid)
        write_start = self.reactor.monotonic()
        self.write_frame(frame, label=label)
        self._pause(self.response_delay)
        end_time = write_start + timeout
        extra = []
        last_error = None
        while self.reactor.monotonic() < end_time:
            try:
                rx = self.wait_frame(
                    timeout=max(0.001, end_time - self.reactor.monotonic()),
                    poll_interval=self.response_delay,
                    irq_after=write_start,
                    accept_current_irq=False,
                    label=label)
            except Exception as e:
                last_error = e
                self._pause(self.response_delay)
                continue
            if (_message_type(rx) == NCI_MT_RSP
                    and _gid(rx) == expected_gid and _oid(rx) == expected_oid):
                if len(rx) >= 4 and rx[3] not in allowed_statuses:
                    raise PN7160Error("NCI command failed: %s" % _hex(rx))
                return rx, extra
            if allow_extra_ntf:
                extra.append(rx)
                continue
            raise PN7160Error("unexpected NCI frame: %s"
                              % _frame_summary(rx))
        raise PN7160Error("timeout waiting for response: %s" % last_error)

    def wait_data_frame(self, timeout=0.100, label=None):
        """Wait for an NCI DATA frame after sending a tag command.

        NTAG READ and ISO15693 block reads travel as data packets once an RF
        interface is active.  CORE_CONN_CREDITS notifications are flow-control
        bookkeeping and are ignored here.
        """
        end_time = self.reactor.monotonic() + timeout
        last_error = None
        while self.reactor.monotonic() < end_time:
            try:
                if self.no_irq_mode:
                    delay = min(
                        self.ntag_data_delay,
                        max(0.0, end_time - self.reactor.monotonic()))
                    if delay > 0.0:
                        self._pause(delay)
                    frame = self.read_frame_once()
                else:
                    frame = self.wait_frame(
                        timeout=max(
                            0.001, end_time - self.reactor.monotonic()),
                        poll_interval=max(0.001, self.ntag_data_delay),
                        label=label)
            except Exception as e:
                last_error = e
                self._debug("data wait read failed: %s" % e)
                continue
            if _message_type(frame) == NCI_MT_DATA:
                return frame
            if (len(frame) >= 6
                    and _message_type(frame) == NCI_MT_NTF
                    and _gid(frame) == NCI_GID_CORE
                    and _oid(frame) == NCI_CORE_CONN_CREDITS_OID):
                self._debug("data credit notification: %s" % _hex(frame))
                continue
            self._debug("data wait ignored frame: %s"
                        % _frame_summary(frame))
        raise PN7160Error("timeout waiting for data frame: %s" % last_error)

    def transceive_data(self, payload, timeout=0.100, label="DATA"):
        frame = [0x00, 0x00, len(payload)] + list(payload)
        self._debug("data transceive %s payload=%s"
                    % (label, _hex(payload)))
        self.write_frame(frame, label=label)
        return self.wait_data_frame(timeout=timeout, label=label)

    def ntag_read_page_once(self, page, timeout=0.100):
        rx = self.transceive_data(
            [MIFARE_CMD_READ, page & 0xff], timeout=timeout,
            label="NTAG_READ_%02X" % (page & 0xff,))
        payload = rx[3:]
        if len(payload) < 16:
            raise PN7160Error("short NTAG page response page=%d data=%s"
                              % (page, _hex(payload)))
        data = list(payload[:16])
        self._debug("NTAG page %d data=%s" % (page, _hex(data)))
        return data, rx

    def ntag_read_page(self, page, timeout=0.100):
        attempts = max(1, self.ntag_read_retries + 1)
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                return self.ntag_read_page_once(page, timeout=timeout)
            except Exception as e:
                last_error = e
                self._debug("NTAG page %d attempt %d/%d failed: %s"
                            % (page, attempt, attempts, e))
                if attempt < attempts and self.ntag_retry_delay > 0.0:
                    self._pause(self.ntag_retry_delay)
        raise PN7160Error("NTAG page %d failed after %d attempts: %s"
                          % (page, attempts, last_error))

    def mifare_authenticate(self, block_addr, key, uid_bytes,
                            use_key_b=False, timeout=0.500):
        """Authenticate a MIFARE Classic sector on the active TAGCMD session.

        PN7160 exposes MIFARE Classic through NXP's proprietary TAGCMD
        extension, not through the PN532-style over-the-air auth frame.  The
        NFCC receives a compact auth request:

            40 <sector> <key flags> [six-byte key]

        The UID is still accepted for interface compatibility with PN532 and
        the key derivation code, but PN7160 does not send it in this helper
        request.  For Bambu-derived keys we always use the embedded-key form.
        """
        uid = list(uid_bytes or [])[:4]
        key = list(key or [])[:6]
        if len(uid) != 4:
            raise PN7160Error("MIFARE auth requires 4-byte UID")
        if len(key) != 6:
            raise PN7160Error("MIFARE auth requires 6-byte key")
        sector = self._mifare_sector_from_block(block_addr)
        key_flags = MIFARE_EXT_AUTH_EMBEDDED_KEY
        if use_key_b:
            key_flags |= MIFARE_EXT_AUTH_KEY_B
        payload = [MIFARE_EXT_AUTH, sector & 0xff, key_flags] + key
        label = "MIFARE_AUTH_%02X" % (block_addr & 0xff,)
        try:
            rx = self.transceive_data(payload, timeout=timeout, label=label)
        except Exception as e:
            self._debug(
                "MIFARE auth block=%d sector=%d key_%s failed: %s"
                % (block_addr, sector, 'B' if use_key_b else 'A', e))
            return False

        rsp = rx[3:]
        if (len(rsp) >= 2
                and rsp[0] == MIFARE_EXT_AUTH
                and rsp[1] == NCI_STATUS_OK):
            self._debug(
                "MIFARE auth block=%d sector=%d key_%s ok rsp=%s"
                % (block_addr, sector, 'B' if use_key_b else 'A',
                   _hex(rsp)))
            return True
        self._debug(
            "MIFARE auth block=%d sector=%d key_%s rejected rsp=%s"
            % (block_addr, sector, 'B' if use_key_b else 'A', _hex(rsp)))
        return False

    def mifare_read_block(self, block_addr, timeout=0.500):
        """Read one 16-byte MIFARE Classic block after sector auth."""
        rx = self.transceive_data(
            [MIFARE_EXT_RAW_DATA, MIFARE_CMD_READ, block_addr & 0xff],
            timeout=timeout,
            label="MIFARE_READ_%02X" % (block_addr & 0xff,))
        payload = rx[3:]
        if not (len(payload) >= 18
                and payload[0] == MIFARE_EXT_RAW_DATA
                and payload[-1] == NCI_STATUS_OK):
            raise PN7160Error(
                "bad MIFARE block response block=%d data=%s"
                % (block_addr, _hex(payload)))
        data = bytes(payload[1:17])
        self._debug("MIFARE block %d data=%s" % (block_addr, _hex(data)))
        return data

    def _mifare_sector_from_block(self, block_addr):
        """Map an absolute MIFARE Classic block number to sector number."""
        block = int(block_addr) & 0xff
        if block >= 128:
            return 32 + ((block - 128) // 16)
        return block // 4

    def mifare_read_authenticated_blocks(self, sector_keys, sectors,
                                         uid_bytes=None, timeout=0.500,
                                         use_key_b=False):
        """Authenticate sectors and read their data blocks.

        Returns the same shape as PN532Driver so tag_handler and the Bambu
        parser stay reader-agnostic. use_key_b authenticates with Key B
        instead of Key A (e.g. Creality CFS/K1/K2 sector 1, which uses a
        UID-derived Key B).

        PN7160/NCI can leave the active RF/I2C session unhealthy after a failed
        MIFARE Classic auth/read exchange.  Unlike PN532, do not keep probing
        later sectors inside the same session after the first failure; return a
        partial result and let scan-jog perform its bounded outer retry.
        """
        blocks = {}
        auth_failed_sectors = []
        read_failed_blocks = []
        stop_after_failure = False
        for sector in sectors:
            if stop_after_failure:
                break
            # PN7160/NCI authenticates by sector through the NXP MIFARE
            # extension.  Keep the public call block-based to match PN532.
            auth_block = sector * 4
            key = sector_keys[sector] if sector < len(sector_keys) else None
            if key is None:
                continue
            if not self.mifare_authenticate(
                    auth_block, key, uid_bytes, use_key_b=use_key_b,
                    timeout=timeout):
                auth_failed_sectors.append(sector)
                stop_after_failure = True
                break
            for blk_offset in range(3):
                block_addr = sector * 4 + blk_offset
                try:
                    blocks[block_addr] = self.mifare_read_block(
                        block_addr, timeout=timeout)
                except Exception as e:
                    read_failed_blocks.append(block_addr)
                    self._debug("MIFARE block %d read failed: %s"
                                % (block_addr, e))
                    stop_after_failure = True
                    break
        result = {"uid_bytes": bytes(uid_bytes or []), "blocks": blocks}
        if auth_failed_sectors:
            result["auth_failed_sectors"] = auth_failed_sectors
        if read_failed_blocks:
            result["read_failed_blocks"] = read_failed_blocks
        return result

    def ntag_read_user_memory(self, start_page=4, end_page=67,
                              timeout=0.100):
        data = bytearray()
        current_page = start_page
        while current_page <= end_page:
            block, _frame = self.ntag_read_page(
                current_page, timeout=timeout)
            remaining_pages = end_page - current_page + 1
            if remaining_pages >= 4:
                data.extend(block)
            else:
                data.extend(block[:remaining_pages * 4])
            if 0xFE in block:
                self._debug("NTAG terminator found at page %d offset %d"
                            % (current_page, block.index(0xFE)))
                break
            current_page += 4
            if current_page <= end_page and self.ntag_data_delay > 0.0:
                self._pause(self.ntag_data_delay)
        return data

    @staticmethod
    def _ndef_tlv_extent(data):
        i = 0
        data_len = len(data)
        while i < data_len:
            tlv_type = data[i]
            if tlv_type == 0x00:
                i += 1
                continue
            if tlv_type == 0xFE:
                return None
            if i + 1 >= data_len:
                return None
            tlv_len = data[i + 1]
            if tlv_len == 0xFF:
                if i + 3 >= data_len:
                    return None
                tlv_len = (data[i + 2] << 8) | data[i + 3]
                value_start = i + 4
            else:
                value_start = i + 2
            value_end = value_start + tlv_len
            if tlv_type == 0x03:
                return value_end, tlv_len
            i = value_end
        return None

    def ntag_read_ndef_user_memory(self, start_page=4, max_pages=16,
                                   max_ndef_pages=135, timeout=0.100):
        """Read enough Type-2 user memory for tag_handler to parse payloads."""
        max_pages = max(4, int(max_pages))
        max_ndef_pages = max(max_pages, int(max_ndef_pages))
        fallback_bytes = max_pages * 4
        max_ndef_bytes = max_ndef_pages * 4
        target_bytes = min(16, fallback_bytes)
        data = bytearray()
        current_page = start_page
        while len(data) < target_bytes:
            block, _frame = self.ntag_read_page(
                current_page, timeout=timeout)
            data.extend(block)
            extent = self._ndef_tlv_extent(data)
            if extent is not None:
                target_bytes, ndef_len = extent
                if target_bytes > max_ndef_bytes:
                    self._debug(
                        "NTAG NDEF length %d capped to %d bytes"
                        % (ndef_len, max_ndef_bytes))
                    target_bytes = max_ndef_bytes
            elif len(data) >= 16:
                target_bytes = fallback_bytes
            current_page += 4
            if current_page > 255:
                break
            if len(data) < target_bytes and self.ntag_data_delay > 0.0:
                self._pause(self.ntag_data_delay)
        return data[:min(len(data), target_bytes)]

    def iso15693_read_blocks_once(self, tag, start_block, block_count,
                                  timeout=0.100):
        if block_count == 1:
            return self.iso15693_read_single_block_once(
                start_block, timeout=timeout)
        uid_lsb = tag.get('uid_lsb_first')
        if not uid_lsb or len(uid_lsb) != 8:
            raise PN7160Error("ISO15693 addressed read requires UID")
        payload = (
            [ISO15693_FLAG_HIGH_DATA_RATE | ISO15693_FLAG_ADDRESS,
             ISO15693_CMD_READ_MULTIPLE_BLOCKS]
            + list(uid_lsb)
            + [start_block & 0xff, (block_count - 1) & 0xff])
        rx = self.transceive_data(
            payload, timeout=timeout,
            label="ISO15693_READ_%02X_%02X"
            % (start_block & 0xff, block_count & 0xff))
        response = rx[3:]
        expected_len = 1 + block_count * 4
        expected_len_with_status = expected_len + 1
        if len(response) not in (expected_len, expected_len_with_status):
            raise PN7160Error(
                "unexpected ISO15693 blocks %d-%d length %d data=%s"
                % (start_block, start_block + block_count - 1,
                   len(response), _hex(response)))
        if response[0] & 0x01:
            code = response[1] if len(response) > 1 else 0
            raise PN7160Error(
                "ISO15693 blocks %d-%d error response 0x%02X"
                % (start_block, start_block + block_count - 1, code))
        if len(response) == expected_len_with_status and response[-1] != 0x00:
            raise PN7160Error(
                "ISO15693 blocks %d-%d trailing status 0x%02X"
                % (start_block, start_block + block_count - 1,
                   response[-1]))
        return list(response[1:expected_len]), rx

    def iso15693_read_single_block_once(self, block, timeout=0.100):
        payload = [
            ISO15693_FLAG_HIGH_DATA_RATE,
            ISO15693_CMD_READ_SINGLE_BLOCK,
            block & 0xff,
        ]
        rx = self.transceive_data(
            payload, timeout=timeout,
            label="ISO15693_READ_%02X" % (block & 0xff,))
        response = rx[3:]
        if len(response) not in (5, 6):
            raise PN7160Error(
                "unexpected ISO15693 block %d length %d data=%s"
                % (block, len(response), _hex(response)))
        if response[0] & 0x01:
            code = response[1] if len(response) > 1 else 0
            raise PN7160Error(
                "ISO15693 block %d error response 0x%02X" % (block, code))
        if len(response) == 6 and response[-1] != 0x00:
            raise PN7160Error(
                "ISO15693 block %d trailing status 0x%02X"
                % (block, response[-1]))
        return list(response[1:5]), rx

    def iso15693_read_blocks(self, tag, start_block, block_count,
                             timeout=0.100):
        attempts = max(1, self.ntag_read_retries + 1)
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                return self.iso15693_read_blocks_once(
                    tag, start_block, block_count, timeout=timeout)
            except Exception as e:
                last_error = e
                self._debug("ISO15693 blocks %d-%d attempt %d/%d failed: %s"
                            % (start_block, start_block + block_count - 1,
                               attempt, attempts, e))
                if attempt < attempts and self.ntag_retry_delay > 0.0:
                    self._pause(self.ntag_retry_delay)
        raise PN7160Error("ISO15693 blocks %d-%d failed after %d attempts: %s"
                          % (start_block, start_block + block_count - 1,
                             attempts, last_error))

    def iso15693_read_blocks_with_fallback(self, tag, start_block,
                                           block_count, timeout=0.100):
        try:
            return self.iso15693_read_blocks(
                tag, start_block, block_count, timeout=timeout)
        except Exception:
            if block_count <= 1:
                raise
        data = []
        frames = []
        for block in range(start_block, start_block + block_count):
            block_data, frame = self.iso15693_read_blocks(
                tag, block, 1, timeout=timeout)
            data += block_data
            frames.append(frame)
        return data, frames

    def iso15693_read_user_memory(self, tag,
                                  start_block=ISO15693_DEFAULT_START_BLOCK,
                                  end_block=ISO15693_DEFAULT_END_BLOCK,
                                  batch_size=ISO15693_DEFAULT_BLOCKS_PER_READ,
                                  timeout=0.100):
        data = bytearray()
        block = start_block
        batch_size = max(1, min(int(batch_size), 16))
        while block <= end_block:
            count = min(batch_size, end_block - block + 1)
            block_data, _frame = self.iso15693_read_blocks_with_fallback(
                tag, block, count, timeout=timeout)
            data.extend(block_data)
            expected_len = self._expected_tlv_total_length(data)
            if expected_len and len(data) >= expected_len:
                del data[expected_len:]
                break
            if expected_len is None and self._is_empty_data(block_data):
                break
            block += count
            if block <= end_block and self.ntag_data_delay > 0.0:
                self._pause(self.ntag_data_delay)
        return data

    @staticmethod
    def _is_empty_data(data):
        return bool(data) and all(b == 0x00 for b in data)

    @staticmethod
    def _expected_tlv_total_length(data):
        data = bytes(data)
        offsets = [0]
        if len(data) >= 4 and data[0] in (0xE1, 0xE2):
            offsets.insert(0, 4)
        for offset in offsets:
            total = PN7160Handler._expected_tlv_total_length_at(data, offset)
            if total is not None:
                return total
        return None

    @staticmethod
    def _expected_tlv_total_length_at(data, offset):
        pos = offset
        while pos < len(data):
            tlv_type = data[pos]
            pos += 1
            if tlv_type == 0x00:
                continue
            if tlv_type == 0xFE:
                return pos
            if pos >= len(data):
                return None
            tlv_len = data[pos]
            pos += 1
            if tlv_len == 0xFF:
                if pos + 2 > len(data):
                    return None
                tlv_len = (data[pos] << 8) | data[pos + 1]
                pos += 2
            end = pos + tlv_len
            if end > len(data):
                return None
            if tlv_type == 0x03:
                return end
            pos = end
        return None

    def connect_nci(self, reset=True, keep_config=False):
        last_error = None
        for attempt in range(1, max(1, self.init_retries) + 1):
            try:
                frames = self._connect_nci_once(
                    reset=reset, keep_config=keep_config)
                self.initialized = True
                return frames
            except Exception as e:
                last_error = e
                self.initialized = False
                self._debug("connect attempt %d failed: %s" % (attempt, e))
                if attempt < self.init_retries:
                    self._pause(self.init_retry_delay)
        raise PN7160Error("connect_nci failed: %s" % last_error)

    def _connect_nci_once(self, reset=True, keep_config=False):
        if reset:
            self.hardware_reset()
        reset_cmd = (NCI_CORE_RESET_KEEP_CONFIG_CMD if keep_config
                     else NCI_CORE_RESET_CLEAR_CONFIG_CMD)
        reset_rsp, reset_extra = self.command(
            reset_cmd, NCI_GID_CORE, 0x00, timeout=1.0)
        self._pause(0.020)
        try:
            extra_frame = self.read_optional_frame(timeout=0.050)
            if extra_frame is not None:
                reset_extra.append(extra_frame)
        except Exception:
            pass
        self._summarize_core_startup([reset_rsp] + reset_extra)
        init_rsp, init_extra = self.command(
            NCI_CORE_INIT_CMD_PN7160, NCI_GID_CORE, 0x01, timeout=1.0)
        self._summarize_core_startup([init_rsp] + init_extra)
        return [reset_rsp] + reset_extra + [init_rsp] + init_extra

    def _summarize_core_startup(self, frames):
        for frame in frames:
            if len(frame) < 4 or _gid(frame) != NCI_GID_CORE:
                continue
            if _message_type(frame) == NCI_MT_RSP and _oid(frame) == 0x00:
                self._core_info("CORE_RESET_RSP status=0x%02X raw=%s"
                                % (frame[3], _hex(frame)))
            elif _message_type(frame) == NCI_MT_NTF and _oid(frame) == 0x00:
                self._core_info("CORE_RESET_NTF raw=%s" % _hex(frame))
            elif _message_type(frame) == NCI_MT_RSP and _oid(frame) == 0x01:
                self._core_info("CORE_INIT_RSP status=0x%02X raw=%s"
                                % (frame[3], _hex(frame)))

    def configure_discovery_map(self):
        rsp, extra = self.command(
            NCI_RF_DISCOVER_MAP_RW_CMD, NCI_GID_RF, 0x00, timeout=1.0)
        return [rsp] + extra

    def start_discovery(self):
        rsp, extra = self.command(
            NCI_RF_DISCOVER_NFCA_NFCV_CMD, NCI_GID_RF, 0x03, timeout=1.0,
            allowed_statuses=(
                NCI_STATUS_OK,
                NCI_STATUS_DISCOVERY_ALREADY_STARTED,
                NCI_STATUS_DISCOVERY_TARGET_ACTIVATION_FAILED,
                NCI_STATUS_DISCOVERY_TEAR_DOWN,
            ))
        return [rsp] + extra

    def stop_discovery(self):
        try:
            rsp, extra = self.command(
                NCI_RF_DEACTIVATE_IDLE_CMD, NCI_GID_RF, 0x06, timeout=1.0)
            # NXP PN716x firmware notes for 12.50.10/12.50.11 require at
            # least 25 ms after RF_DEACTIVATE_RSP before a new
            # RF_DISCOVER_CMD.  Without this guard, the NFCC may leave RF on.
            self._pause(PN7160_RF_DEACTIVATE_GUARD_TIME)
            return [rsp] + extra
        except Exception as e:
            self._debug("stop discovery skipped/failed: %s" % e)
            return []

    def wait_for_activation(self, timeout=None):
        timeout = self.read_timeout if timeout is None else timeout
        if self.no_irq_mode:
            return self._wait_for_activation_no_irq(timeout)
        end_time = self.reactor.monotonic() + timeout
        frames = []
        while self.reactor.monotonic() < end_time:
            try:
                frame = self.wait_frame(
                    timeout=min(self.nci_poll_interval,
                                max(0.001, end_time
                                    - self.reactor.monotonic())),
                    poll_interval=self.nci_poll_interval)
            except Exception:
                continue
            frames.append(frame)
            result = self._handle_activation_frame(frame, frames)
            if result is not None:
                return result
        raise PN7160NoTag("no NFC tag found")

    def _wait_for_activation_no_irq(self, timeout):
        frames = []
        if self.no_irq_read_delay > 0.0:
            self._pause(min(self.no_irq_read_delay, timeout))
        try:
            frame = self.read_frame_once()
        except PN7160I2CStatusError:
            raise PN7160NoTag("no NFC tag found")
        frames.append(frame)
        result = self._handle_activation_frame(frame, frames)
        if result is not None:
            return result
        raise PN7160NoTag("no NFC tag found")

    def _handle_activation_frame(self, frame, frames):
        if self._is_activation_ntf(frame):
            tag = self.parse_activation_tag(frame)
            if tag:
                return tag, frame, frames
            return None
        if self._is_discover_ntf(frame):
            select_frames = self.select_discovered_endpoint(frame)
            frames += select_frames
            for select_frame in select_frames:
                if self._is_activation_ntf(select_frame):
                    tag = self.parse_activation_tag(select_frame)
                    if tag:
                        return tag, select_frame, frames
        return None

    def _is_activation_ntf(self, frame):
        return (_message_type(frame) == NCI_MT_NTF
                and _gid(frame) == NCI_GID_RF and _oid(frame) == 0x05)

    def _is_discover_ntf(self, frame):
        return (_message_type(frame) == NCI_MT_NTF
                and _gid(frame) == NCI_GID_RF and _oid(frame) == 0x03)

    def select_discovered_endpoint(self, frame):
        payload = frame[3:]
        if len(payload) < 3:
            raise PN7160Error("RF_DISCOVER_NTF too short: %s" % _hex(frame))
        rf_disc_id = payload[0]
        protocol = payload[1]
        ntf_type = payload[-1]
        if ntf_type == 0x02:
            return []
        interface = self._interface_for_protocol(protocol)
        cmd = [0x21, 0x04, 0x03, rf_disc_id, protocol, interface]
        rsp, extra = self.command(cmd, NCI_GID_RF, 0x04, timeout=1.0)
        return [rsp] + extra

    def _interface_for_protocol(self, protocol):
        if protocol == NCI_PROT_ISODEP:
            return NCI_INTF_ISODEP
        if protocol == NCI_PROT_MIFARE:
            return NCI_INTF_TAGCMD
        return NCI_INTF_FRAME

    def parse_activation_tag(self, frame):
        payload = frame[3:]
        if len(payload) < 7:
            return None
        protocol = payload[2]
        mode_tech = payload[3]
        if mode_tech == NCI_MODE_PASSIVE_NFCA:
            return self._parse_nfca_tag(payload, protocol, mode_tech)
        if mode_tech == NCI_MODE_PASSIVE_NFCV or protocol == NCI_PROT_ISO15693:
            return self._parse_nfcv_tag(payload, protocol, mode_tech)
        return None

    def _parse_nfca_tag(self, payload, protocol, mode_tech):
        params = payload[7:]
        if len(params) < 3:
            return None
        sens_res_bytes = list(params[0:2])
        sens_res = (sens_res_bytes[0] << 8) | sens_res_bytes[1]
        nfcid_len = params[2]
        if nfcid_len <= 0 or nfcid_len > 10:
            return None
        uid_start = 3
        uid_end = uid_start + nfcid_len
        if len(params) < uid_end:
            return None
        sak = 0
        if len(params) > uid_end:
            sel_res_len = params[uid_end]
            sel_res = params[uid_end + 1:uid_end + 1 + sel_res_len]
            if sel_res:
                sak = sel_res[0]
        return {
            'protocol': protocol,
            'protocol_name': 'NFC-A',
            'mode_tech': mode_tech,
            'uid': list(params[uid_start:uid_end]),
            'sens_res': sens_res,
            'sens_res_bytes': sens_res_bytes,
            'sak': sak,
        }

    def _parse_nfcv_tag(self, payload, protocol, mode_tech):
        params = payload[7:]
        if len(params) < 10:
            return None
        uid_lsb_first = list(params[2:10])
        return {
            'protocol': protocol,
            'protocol_name': 'ISO15693',
            'mode_tech': mode_tech,
            'uid': list(reversed(uid_lsb_first)),
            'uid_lsb_first': uid_lsb_first,
            'afi': params[0],
            'dsfid': params[1],
        }


class PN7160Driver:
    """Happy Hare reader adapter for PN7160.

    The public methods intentionally mirror PN532Driver where the tag type
    allows it.  More specialized capabilities, such as MIFARE Classic
    authenticated sector reads, should be added as explicit methods later.
    """

    def __init__(self, config, i2c, gate, debug=2, sleep_fn=None):
        self._gate = gate
        self._debug = debug
        self._transport_name = 'PN7160'
        self._alive = False
        self._needs_full_setup = True
        self._discovery_active = False
        self._clear_current_card()

        # Advanced PN7160 tuning options are intentionally hidden from the
        # default config templates.  Users can still override them in a specific
        # [nfc_gate laneN] section during hardware bring-up.
        raw_log = config.getboolean('raw_log', False)
        pn7160_debug = config.getboolean('pn7160_debug', False)
        handler_debug = debug >= 4 or pn7160_debug
        self._handler = PN7160Handler(
            config, i2c,
            ven_pin=config.get('ven_pin', None),
            irq_pin=config.get('irq_pin', None),
            response_delay=config.getfloat(
                'response_delay', 0.020, minval=0.0),
            nci_poll_interval=config.getfloat(
                'nci_poll_interval', 0.250, minval=0.0),
            read_timeout=config.getfloat(
                'read_timeout', 0.500, minval=0.0),
            raw_log=raw_log,
            debug=handler_debug,
            ven_pre_high_time=config.getfloat(
                'ven_pre_high_time', 0.010, minval=0.0),
            ven_low_time=config.getfloat(
                'ven_low_time', 0.010, minval=0.0),
            ven_post_high_time=config.getfloat(
                'ven_post_high_time', 0.100, minval=0.0),
            init_retries=config.getint('init_retries', 3, minval=1),
            init_retry_delay=config.getfloat(
                'init_retry_delay', 0.500, minval=0.0),
            no_irq_read_delay=config.getfloat(
                'no_irq_read_delay', 0.100, minval=0.0),
            ntag_data_delay=config.getfloat(
                'ntag_data_delay', 0.005, minval=0.0),
            ntag_read_retries=config.getint(
                'ntag_read_retries', 2, minval=0),
            ntag_retry_delay=config.getfloat(
                'ntag_retry_delay', 0.025, minval=0.0))

    def _clear_current_card(self):
        self.current_target = None
        self.current_uid = None
        self.current_uid_hex = ''
        self.current_target_info = None

    def _set_current_card(self, target_info):
        self.current_target_info = dict(target_info)
        self.current_target = target_info.get('target')
        self.current_uid = list(target_info.get('uid_bytes') or [])
        self.current_uid_hex = target_info.get('uid', _hex(self.current_uid, ''))

    def init(self):
        self._setup_for_read(full=True)
        self._alive = True
        # Klipper calls init() during startup as a health check.  Keep the next
        # real read conservative: it should still run full setup before
        # starting RF discovery.
        self._needs_full_setup = True

    def is_alive(self):
        return bool(self._alive and self._handler.initialized)

    def _setup_for_read(self, full=None):
        """Prepare PN7160 for one read operation.

        PN532 can stay initialized after SAMConfiguration.  PN7160 is different:
        each read starts a small NCI setup sequence before RF discovery.  The
        first read, or any read after an error, uses a full setup that clears
        config and writes the discovery map.  Consecutive reads may use the
        cheaper keep-config setup.
        """
        if full is None:
            full = self._needs_full_setup
        if full:
            self._handler.connect_nci(reset=True, keep_config=False)
            self._handler.configure_discovery_map()
        else:
            self._handler.connect_nci(reset=False, keep_config=True)
        self._alive = True
        self._needs_full_setup = False

    def read_tag(self, timeout=None):
        target_info = self.read_target(timeout=timeout)
        if target_info is None:
            return None
        uid = target_info.get('uid')
        self._release_current_target(reason="uid_read_complete")
        return uid

    def read_target(self, timeout=None):
        try:
            self._setup_for_read()
            self._handler.start_discovery()
            self._discovery_active = True
            tag, _activation, _frames = self._handler.wait_for_activation(
                timeout=timeout)
            target_info = self._target_info_from_tag(tag)
            self._set_current_card(target_info)
            return target_info
        except PN7160NoTag:
            self._clear_current_card()
            return None
        except Exception as e:
            self._alive = False
            self._handler.initialized = False
            self._needs_full_setup = True
            self._clear_current_card()
            logger.warning("PN7160 read_target gate %s failed: %s",
                           self._gate, e)
            return None
        finally:
            if self.current_target_info is None:
                self._stop_discovery()

    def _release_current_target(self, reason="manual"):
        self._stop_discovery(reason=reason)
        self._clear_current_card()

    def _stop_discovery(self, reason="read_complete"):
        if not self._discovery_active:
            return
        try:
            self._handler.stop_discovery()
        except Exception as e:
            if self._debug >= 4:
                logger.debug("PN7160 stop discovery failed (%s): %s",
                             reason, e)
        finally:
            self._discovery_active = False

    def _ensure_active_target(self, timeout=0.500):
        if self.current_target_info is not None and self._discovery_active:
            return self.current_target_info
        target_info = self.read_target(timeout=timeout)
        if target_info is None:
            raise PN7160NoTag("no active PN7160 target")
        return target_info

    def ntag_read_user_memory(self, start_page=4, end_page=67,
                              timeout=0.100):
        """Read raw NTAG/Type-2 user memory like PN532Driver does.

        The tag is already activated by read_target() in the normal
        tag_handler flow.  If called directly, this method performs one
        activation first, then releases RF state after the read.
        """
        try:
            self._ensure_active_target()
            return self._handler.ntag_read_user_memory(
                start_page=start_page, end_page=end_page, timeout=timeout)
        finally:
            self._release_current_target(reason="user_memory_complete")

    def ntag_read_ndef_user_memory(self, start_page=4, max_pages=16,
                                   max_ndef_pages=135, timeout=0.100):
        try:
            self._ensure_active_target()
            return self._handler.ntag_read_ndef_user_memory(
                start_page=start_page, max_pages=max_pages,
                max_ndef_pages=max_ndef_pages, timeout=timeout)
        finally:
            self._release_current_target(reason="ndef_user_memory_complete")

    def iso15693_read_user_memory(self, tag=None,
                                  start_block=ISO15693_DEFAULT_START_BLOCK,
                                  end_block=ISO15693_DEFAULT_END_BLOCK,
                                  batch_size=ISO15693_DEFAULT_BLOCKS_PER_READ,
                                  timeout=0.100):
        try:
            target_info = self._ensure_active_target()
            tag = target_info if tag is None else tag
            return self._handler.iso15693_read_user_memory(
                tag, start_block=start_block, end_block=end_block,
                batch_size=batch_size, timeout=timeout)
        finally:
            self._release_current_target(reason="iso15693_user_memory_complete")

    def mifare_read_authenticated_blocks(self, sector_keys, sectors,
                                         uid_bytes=None, timeout=0.500,
                                         use_key_b=False):
        """Authenticate and read MIFARE Classic blocks like PN532Driver.

        The target must remain active for the whole auth/read sequence because
        MIFARE Classic authentication state is tied to the current RF session.
        """
        try:
            target_info = self._ensure_active_target()
            if uid_bytes is None:
                uid_bytes = target_info.get('uid_bytes') or self.current_uid
            return self._handler.mifare_read_authenticated_blocks(
                sector_keys, sectors, uid_bytes=uid_bytes, timeout=timeout,
                use_key_b=use_key_b)
        finally:
            self._release_current_target(reason="mifare_read_complete")

    def _target_info_from_tag(self, tag):
        uid_bytes = list(tag.get('uid') or [])
        protocol_name = tag.get('protocol_name', 'unknown')
        if protocol_name == 'ISO15693':
            protocol = 'iso15693_type5'
        else:
            protocol = 'ntag_type2'
        info = {
            'reader': 'pn7160',
            'protocol': protocol,
            'protocol_name': protocol_name,
            'target': 1,
            'tg': 1,
            'uid': _hex(uid_bytes, ''),
            'uid_bytes': uid_bytes,
            'uid_length': len(uid_bytes),
            'sak': int(tag.get('sak', 0) or 0),
            'sens_res': int(tag.get('sens_res', 0) or 0),
            'atqa': int(tag.get('sens_res', 0) or 0),
            'sens_res_bytes': list(tag.get('sens_res_bytes') or []),
        }
        if 'uid_lsb_first' in tag:
            info['uid_lsb_first'] = list(tag.get('uid_lsb_first') or [])
        if 'afi' in tag:
            info['afi'] = tag.get('afi')
        if 'dsfid' in tag:
            info['dsfid'] = tag.get('dsfid')
        return info
