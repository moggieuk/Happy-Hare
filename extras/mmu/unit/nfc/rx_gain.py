"""Discrete receiver-gain tables shared by NFC config and chip drivers."""

# Values are dB -> the chip register encoding. Zero is intentionally absent:
# at the config layer it means "do not write a gain; retain the chip/profile
# default", not a hardware gain code.
RX_GAIN_CODES = {
    'pn532': {
        18: 0b010, 23: 0b011, 33: 0b100,
        38: 0b101, 43: 0b110, 48: 0b111,
    },
    'pn5180': {
        33: 0b00, 40: 0b01, 50: 0b10, 57: 0b11,
    },
    'pn7160': {
        18: 0b000, 26: 0b001, 32: 0b010, 39: 0b011,
        44: 0b100, 51: 0b101, 53: 0b110, 60: 0b111,
    },
    'rc522': {
        18: 0b010, 23: 0b011, 33: 0b100,
        38: 0b101, 43: 0b110, 48: 0b111,
    },
}

RX_GAIN_DB = {
    reader_type: tuple(sorted(codes))
    for reader_type, codes in RX_GAIN_CODES.items()
}
