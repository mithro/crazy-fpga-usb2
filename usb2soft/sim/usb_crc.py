"""USB 2.0 packet arithmetic in plain Python: PIDs, CRC5 (tokens), CRC16 (data), byte builders.

Bit conventions follow USB 2.0 §8.3.5: both CRCs are computed LSB-first over the payload bits
with all-ones initial value; the complemented remainder is appended so that the receiver's
residual is constant (0x0C for CRC5, 0x800D for CRC16). Reflected implementations with
``init = xorout = all ones`` give the same result; the CRC catalogue check values for "123456789"
are 0x19 (CRC-5/USB) and 0xB4C8 (CRC-16/USB).
"""

__all__ = ["PID", "crc5", "crc16", "token", "sof", "data", "handshake", "split_token",
           "CRC5_RESIDUAL", "CRC16_RESIDUAL"]


class PID:
    OUT, IN, SOF, SETUP = 0b0001, 0b1001, 0b0101, 0b1101
    DATA0, DATA1, DATA2, MDATA = 0b0011, 0b1011, 0b0111, 0b1111
    ACK, NAK, STALL, NYET = 0b0010, 0b1010, 0b1110, 0b0110
    PRE, ERR, SPLIT, PING = 0b1100, 0b1100, 0b1000, 0b0100

    @staticmethod
    def byte(pid):
        """PID nibble with its complement in the upper nibble, as sent on the wire."""
        return (pid & 0xF) | ((~pid & 0xF) << 4)


CRC5_RESIDUAL = 0x0C
CRC16_RESIDUAL = 0x800D


def _crc_bits(bits, *, width, poly):
    """Bitwise CRC (LSB-first data), init all ones, returns the *complemented* remainder in the
    bit order in which it is transmitted (bit 0 first)."""
    crc = (1 << width) - 1
    top = 1 << (width - 1)
    for b in bits:
        fb = ((crc >> (width - 1)) & 1) ^ (b & 1)
        crc = ((crc << 1) & ((1 << width) - 1))
        if fb:
            crc ^= poly
    crc ^= (1 << width) - 1
    # remainder is transmitted MSB first; reverse so bit 0 of the result is the first wire bit
    out = 0
    for i in range(width):
        if (crc >> (width - 1 - i)) & 1:
            out |= 1 << i
    return out


def crc5(value, nbits=11):
    """CRC5 of an ``nbits``-bit field (LSB first), returned so that ``value | crc << nbits`` is
    the little-endian token body."""
    return _crc_bits([(value >> i) & 1 for i in range(nbits)], width=5, poly=0x05)


def crc16(payload):
    """CRC16 of a byte string, returned as the 16-bit value whose low byte is sent first."""
    bits = [(byte >> i) & 1 for byte in payload for i in range(8)]
    return _crc_bits(bits, width=16, poly=0x8005)


def token(pid, address, endpoint):
    body = (address & 0x7F) | ((endpoint & 0xF) << 7)
    body |= crc5(body) << 11
    return bytes([PID.byte(pid), body & 0xFF, body >> 8])


def sof(frame):
    body = frame & 0x7FF
    body |= crc5(body) << 11
    return bytes([PID.byte(PID.SOF), body & 0xFF, body >> 8])


def data(pid, payload):
    payload = bytes(payload)
    c = crc16(payload)
    return bytes([PID.byte(pid)]) + payload + bytes([c & 0xFF, c >> 8])


def handshake(pid):
    return bytes([PID.byte(pid)])


def split_token(packet):
    """(pid, address, endpoint, crc_ok) of a 3-byte token."""
    body = packet[1] | (packet[2] << 8)
    field = body & 0x7FF
    return packet[0] & 0xF, field & 0x7F, (field >> 7) & 0xF, crc5(field) == (body >> 11)
