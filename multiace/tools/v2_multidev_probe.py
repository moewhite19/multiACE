#!/usr/bin/env python3
"""V2 multi-device bus probe.

Opens the V2 CH343 USB-UART, sends DISCOVER_DEVICE (cmd 0) and ASSIGN_DEVICE_ID
(cmd 1) and hexdumps every byte that comes back so we can see how multiple
ACEs sharing one bus identify themselves.

Daisy-chained setup: 1 USB cable -> 1 CH343 converter -> shared UART bus ->
N ACE2 devices. Each device has a 96-bit UID baked in at manufacture, and
responds to DISCOVER_DEVICE. The host then sends ASSIGN_DEVICE_ID(uid, id=N)
to give each one a 1-byte numeric address. Subsequent commands address by
that numeric id - but the wire-level encoding of that addressing is what we
want to learn from this probe.

Usage:
  python3 v2_multidev_probe.py [PORT]    (PORT defaults to /dev/ttyACM0)
  python3 v2_multidev_probe.py /dev/ttyACM0 --listen-only    (no commands sent)
  python3 v2_multidev_probe.py /dev/ttyACM0 --assign         (also try ASSIGN)

What to look at:
  * Each DISCOVER_DEVICE response carries a UID triplet (uid1, uid2, uid3 -
    each uint32). One response per physical device on the bus. If we see two
    responses, the bus genuinely carries two ACEs.
  * The flags byte in each response (= byte index 2 in the inner frame).
    Standard is 0x80 for a response. If we see 0x81 / 0x82 for ACE2 vs
    ACE1, that's device-id encoding in the low nibble of flags.
  * The seq number echoed in each response. If seq encodes addressing, the
    pattern will show.
"""

import argparse
import serial
import sys
import time

BAUD = 230400
PREAMBLE = b'\xff\xaa'
END_MARKER = 0xFE
FLAG_REQUEST = 0x00
FLAG_RESPONSE = 0x80

CMD_DISCOVER_DEVICE = 0
CMD_ASSIGN_DEVICE_ID = 1
CMD_GET_STATUS = 6
CMD_GET_INFO = 7
CMD_GET_FILAMENT_INFO = 13

CMD_NAMES = {0: 'DISCOVER_DEVICE', 1: 'ASSIGN_DEVICE_ID', 5: 'IAP_VERSION',
             6: 'GET_STATUS', 7: 'GET_INFO', 13: 'GET_FILAMENT_INFO'}

def crc16_kermit(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc & 0xFFFF

def varint(v):
    r = bytearray()
    while v > 0x7F:
        r.append((v & 0x7F) | 0x80)
        v >>= 7
    r.append(v & 0x7F)
    return bytes(r)

def pb_uint32(field, value):
    return varint((field << 3) | 0) + varint(value)

def decode_varint(data, pos):
    result, shift = 0, 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
    return result, pos

def pb_decode(data):
    out = {}
    pos = 0
    while pos < len(data):
        try:
            tag, pos = decode_varint(data, pos)
        except Exception:
            break
        fnum, wtype = tag >> 3, tag & 7
        if wtype == 0:
            val, pos = decode_varint(data, pos)
        elif wtype == 2:
            ln, pos = decode_varint(data, pos)
            val = data[pos:pos + ln]
            pos += ln
        else:
            break
        out.setdefault(fnum, []).append((wtype, val))
    return out

def build_packet(cmd, payload=b'', seq=1, flags=0x00):
    inner = bytearray([flags & 0xFF, seq & 0xFF, (seq >> 8) & 0xFF,
                       cmd & 0xFF, len(payload) & 0xFF])
    inner.extend(payload)
    crc = crc16_kermit(bytes(inner))
    return bytes(PREAMBLE + inner
                 + bytes([crc & 0xFF, (crc >> 8) & 0xFF, END_MARKER]))

def hexdump(data, prefix='  '):
    out = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hex_str = ' '.join('%02x' % b for b in chunk)
        ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        out.append('%s%04x  %-47s  %s' % (prefix, i, hex_str, ascii_str))
    return '\n'.join(out)

def parse_frames(buf):
    """Yield (frame_dict, consumed_bytes) tuples from buf."""
    while len(buf) >= 10:
        idx = buf.find(PREAMBLE)
        if idx < 0:
            buf[:] = buf[-1:] if buf else b''
            return
        if idx > 0:
            del buf[:idx]
            continue
        for end in range(9, min(len(buf), 300)):
            if buf[end] != END_MARKER:
                continue
            plen = buf[6]
            exp = 7 + plen + 2
            if end != exp:
                continue
            inner = bytes(buf[2:7 + plen])
            crc_recv = buf[7 + plen] | (buf[8 + plen] << 8)
            if crc_recv != crc16_kermit(inner):
                del buf[:end + 1]
                break
            frame_bytes = bytes(buf[:end + 1])
            frame = {
                'raw': frame_bytes,
                'flags': buf[2],
                'seq': buf[3] | (buf[4] << 8),
                'cmd': buf[5],
                'plen': buf[6],
                'payload': bytes(buf[7:7 + plen]),
                'crc': crc_recv,
            }
            del buf[:end + 1]
            yield frame
            break
        else:
            return

def send_and_listen(ser, cmd, payload=b'', seq=1, flags=0x00,
                    listen_s=2.0, label=''):
    pkt = build_packet(cmd, payload, seq, flags)
    print('\n=== %s cmd=%d (%s) seq=%d flags=0x%02x len=%d ===' % (
        label or 'SEND', cmd, CMD_NAMES.get(cmd, '?'), seq, flags, len(pkt)))
    print('  TX:')
    print(hexdump(pkt))
    ser.reset_input_buffer()
    ser.write(pkt)
    ser.flush()
    buf = bytearray()
    t_end = time.time() + listen_s
    while time.time() < t_end:
        n = ser.in_waiting
        if n:
            buf.extend(ser.read(n))
        else:
            time.sleep(0.01)
    frames = list(parse_frames(buf))
    if not frames:
        if buf:
            print('  RX (no valid frames, raw %d bytes):' % len(buf))
            print(hexdump(bytes(buf)))
        else:
            print('  RX: (no response)')
        return []
    print('  RX %d frame(s):' % len(frames))
    for n, fr in enumerate(frames):
        print('  --- frame %d ---' % (n + 1))
        print('    flags=0x%02x  seq=%d  cmd=%d (%s)  plen=%d  crc=0x%04x' % (
            fr['flags'], fr['seq'], fr['cmd'],
            CMD_NAMES.get(fr['cmd'], '?'), fr['plen'], fr['crc']))
        print('    raw:')
        print(hexdump(fr['raw'], prefix='    '))
        if fr['payload']:
            print('    decoded payload:')
            fields = pb_decode(fr['payload'])
            for k in sorted(fields):
                vs = fields[k]
                for wt, v in vs:
                    if isinstance(v, bytes):
                        v_display = '%d bytes' % len(v)
                    else:
                        v_display = str(v)
                    print('      field %d (wtype=%d): %s' % (k, wt, v_display))
    return frames

def main():
    p = argparse.ArgumentParser(description='V2 multi-device bus probe')
    p.add_argument('port', nargs='?', default='/dev/ttyACM0',
                   help='Serial port (default /dev/ttyACM0)')
    p.add_argument('--baud', type=int, default=BAUD)
    p.add_argument('--listen-s', type=float, default=2.0,
                   help='Seconds to listen after each TX (default 2.0)')
    p.add_argument('--listen-only', action='store_true',
                   help='Just listen for ambient traffic, send nothing')
    p.add_argument('--assign', action='store_true',
                   help='Also try ASSIGN_DEVICE_ID for each discovered device')
    p.add_argument('--probe-flags', action='store_true',
                   help='After ASSIGN, send GET_INFO with flags=0x00, 0x01, '
                        '0x02 etc to test if flags carry device address')
    p.add_argument('--probe-comprehensive', action='store_true',
                   help='Run all addressing hypothesis tests after ASSIGN: '
                        'flags low/high bits, seq_hi byte, protobuf payload '
                        'fields')
    args = p.parse_args()

    print('Opening %s @ %d baud...' % (args.port, args.baud))
    ser = serial.Serial(args.port, args.baud, timeout=0.1)
    print('Open.')

    if args.listen_only:
        print('\n=== Listening for %.1fs (no TX) ===' % args.listen_s)
        buf = bytearray()
        t_end = time.time() + args.listen_s
        while time.time() < t_end:
            n = ser.in_waiting
            if n:
                buf.extend(ser.read(n))
            else:
                time.sleep(0.01)
        if buf:
            print('  Raw RX (%d bytes):' % len(buf))
            print(hexdump(bytes(buf)))
            frames = list(parse_frames(buf))
            print('  Parsed %d frames' % len(frames))
        else:
            print('  (silent)')
        ser.close()
        return

    frames = send_and_listen(ser, CMD_DISCOVER_DEVICE, listen_s=args.listen_s,
                             label='DISCOVER_DEVICE')

    uids = []
    for fr in frames:
        if fr['cmd'] != CMD_DISCOVER_DEVICE:
            continue
        f = pb_decode(fr['payload'])
        uid1 = f.get(1, [(0, 0)])[0][1]
        uid2 = f.get(2, [(0, 0)])[0][1]
        uid3 = f.get(3, [(0, 0)])[0][1]
        uids.append((uid1, uid2, uid3, fr['flags']))

    print('\n=== SUMMARY ===')
    print('  Discovered %d unique device response(s):' % len(uids))
    for i, (u1, u2, u3, flags) in enumerate(uids):
        print('    [%d] uid=(0x%08x, 0x%08x, 0x%08x)  flags=0x%02x' % (
            i + 1, u1, u2, u3, flags))

    if args.assign and uids:
        print('\n=== ASSIGN_DEVICE_ID - assigning ids 1..N to each UID ===')
        for i, (u1, u2, u3, _f) in enumerate(uids):
            dev_id = i + 1
            payload = (pb_uint32(1, u1) + pb_uint32(2, u2)
                       + pb_uint32(3, u3) + pb_uint32(4, dev_id))
            send_and_listen(ser, CMD_ASSIGN_DEVICE_ID, payload=payload,
                            seq=10 + i, listen_s=args.listen_s,
                            label='ASSIGN_DEVICE_ID uid=(0x%x,0x%x,0x%x) id=%d'
                                  % (u1, u2, u3, dev_id))

    if args.probe_flags:
        print('\n=== GET_INFO with varying flags - does flags carry device address? ===')
        for flags in (0x00, 0x01, 0x02, 0x03):
            send_and_listen(ser, CMD_GET_INFO, seq=100 + flags,
                            flags=flags, listen_s=args.listen_s,
                            label='GET_INFO flags=0x%02x' % flags)

    if args.probe_comprehensive:
        print('\n')
        print('#' * 70)
        print('# COMPREHENSIVE ADDRESSING PROBE')
        print('#' * 70)
        print('# For each hypothesis we send GET_INFO twice - once "for id=1"')
        print('# and once "for id=2". Goal: find an encoding where each request')
        print('# elicits a response from a DIFFERENT physical device. Distinct')
        print('# UIDs in the bytes returned would prove the addressing.')
        print('# (Note: GET_INFO returns FW string only; if FW strings differ')
        print('# between the two ACEs we can tell which one answered. If they')
        print('# are identical, the only proof is a 2-response burst on a')
        print('# broadcast command.)')

        print('\n=== A. flags low nibble = device_id ===')
        for fl in (0x01, 0x02, 0x04, 0x08):
            send_and_listen(ser, CMD_GET_INFO, seq=200 + fl,
                            flags=fl, listen_s=args.listen_s,
                            label='GET_INFO flags=0x%02x' % fl)

        print('\n=== B. flags high nibble = device_id ===')
        for fl in (0x10, 0x20, 0x30, 0x40):
            send_and_listen(ser, CMD_GET_INFO, seq=220 + (fl >> 4),
                            flags=fl, listen_s=args.listen_s,
                            label='GET_INFO flags=0x%02x' % fl)

        print('\n=== C. flags = addressed_mode_bit | device_id ===')
        for bit in (0x40, 0x20, 0x10):
            for did in (1, 2):
                fl = bit | did
                send_and_listen(ser, CMD_GET_INFO, seq=240 + fl,
                                flags=fl, listen_s=args.listen_s,
                                label='GET_INFO flags=0x%02x (addr_bit=0x%02x|id=%d)'
                                      % (fl, bit, did))

        print('\n=== D. seq_hi byte = device_id ===')
        for did in (1, 2):
            seq_val = (did << 8) | 0x33
            send_and_listen(ser, CMD_GET_INFO, seq=seq_val,
                            flags=0x00, listen_s=args.listen_s,
                            label='GET_INFO seq=0x%04x (seq_hi=%d)'
                                  % (seq_val, did))

        print('\n=== E. GET_INFO with protobuf payload device_id field ===')
        for fld in (1, 2, 15):
            for did in (1, 2):
                payload = pb_uint32(fld, did)
                send_and_listen(ser, CMD_GET_INFO, payload=payload,
                                seq=280 + fld * 10 + did,
                                flags=0x00, listen_s=args.listen_s,
                                label='GET_INFO pb_field=%d value=%d (id=%d in payload)'
                                      % (fld, did, did))

        print('\n=== F. seq_lo = device_id, seq_hi = 0 ===')
        for did in (1, 2):
            send_and_listen(ser, CMD_GET_INFO, seq=did,
                            flags=0x00, listen_s=args.listen_s,
                            label='GET_INFO seq=%d (just device_id as seq)' % did)

        print('\n=== G. GET_STATUS broadcast - content-based differentiation? ===')
        send_and_listen(ser, CMD_GET_STATUS, seq=600, flags=0x00,
                        listen_s=args.listen_s,
                        label='GET_STATUS (broadcast - expect 2 distinct responses)')

        print('\n=== H. GET_FILAMENT_INFO index sweep - does index encode device+slot? ===')
        for idx in (0, 1, 2, 3, 4, 5, 6, 7, 16, 17, 18, 19, 32, 33, 34, 35):
            payload = pb_uint32(1, idx)
            send_and_listen(ser, CMD_GET_FILAMENT_INFO, payload=payload,
                            seq=700 + idx, flags=0x00,
                            listen_s=args.listen_s,
                            label='GET_FILAMENT_INFO index=%d' % idx)

    ser.close()

if __name__ == '__main__':
    main()
