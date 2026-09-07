
import json
import logging

PARK_STEP_MM = 20
PARK_SPEED = 40
PARK_MAX_MM = 600
REQ_TIMEOUT = 3.0
RC_IDEMPOTENT_OPS = (0, 4, 5, 6)
RC_IDEMPOTENT_RETRIES = 2
MOVE_RETRIES = 3
MOVE_RETRY_PAUSE = 1.0
UID_VERSION = 513
V2_ACTIVE_MOTION_STATES = ('feeding', 'rollback', 'rollback_assisting',
                           'preloading')
SEARCH_MODE = 0
RESTORE_MODE = 1
PROBE_OFFSETS = (0, -10, -20, 10, 20, -30, -40, 30, 40)
CENTER_MM = 20
START_PROBE_MM = 80
CLEAR_STEP_MM = 20
CLEAR_SILENT_STEPS = 2
CLEAR_MAX_MM = 200

RC_BIT31 = 0x80000000
REG_BITFRAMING = 0x0D
REG_TXMODE = 0x12
REG_RXMODE = 0x13
PCD_TRANSCEIVE = 0x0C
RC_MAX_MM_STAGE2 = None
SELECT_OK = 0

class AceTagReader:
    def __init__(self, ace, debug=False, dump=False):
        self.ace = ace
        self._debug = bool(debug)
        self._dump = bool(dump)

    def _pause(self, seconds):
        r = self.ace.reactor
        r.pause(r.monotonic() + seconds)

    @staticmethod
    def _rejected(resp):
        """True when a motor command was NOT accepted - the S38 rule:
        None / code!=0 / code=0 msg=FORBIDDEN (busy rejection). Own copy:
        the reference lives in ace_bg_swap (not on the ace object), and
        this module must not depend on the bg module being present."""
        if not resp:
            return True
        if resp.get('code', -1) != 0:
            return True
        return str(resp.get('msg', '')).strip().upper() == 'FORBIDDEN'

    def _req(self, idx, method, params, timeout=REQ_TIMEOUT):
        """Synchronous request from inside the greenlet: send + poll the
        reply box. The dispatcher invokes callbacks with KEYWORD
        arguments - callback(self=<ace>, response=<ret>) (ace.py ~5109) -
        so the parameters must literally be named 'self' and 'response';
        a differently-named positional signature raises TypeError inside
        the reactor = full Klipper shutdown (HW 2026-08-30, the S41/S43
        callback class). **kw form tolerates both call styles."""
        box = {}

        def _cb(*args, **kw):
            box['r'] = kw.get('response',
                              args[-1] if args else None)

        try:
            self.ace.send_request_to(
                idx, {'method': method, 'params': dict(params or {})}, _cb)
        except Exception as e:
            logging.info('[multiACE] [rc522] send %s failed: %s'
                         % (method, e))
            return None
        deadline = self.ace.reactor.monotonic() + timeout
        while 'r' not in box and self.ace.reactor.monotonic() < deadline:
            self._pause(0.005)
        return box.get('r')

    def _move(self, idx, slot, length, mode, respond, speed=PARK_SPEED):
        """One fixed-length FEED_OR_ROLLBACK (mode 1 = rollback, 0 = feed)
        with the S38 FORBIDDEN retry ladder. Returns True when accepted.
        The move self-completes (fixed length), no stop needed."""
        for attempt in range(MOVE_RETRIES):
            resp = self._req(idx, 'feed_or_rollback_raw',
                             {'index': slot, 'speed': int(speed),
                              'length': int(length), 'mode': mode})
            if not self._rejected(resp):
                return True
            self._pause(MOVE_RETRY_PAUSE)
        respond('rc522: motor command rejected (slot busy?) - aborting')
        return False

    def _identify(self, idx, slot):
        """One FILAMENT_IDENTIFY probe. Returns the result dict or None.
        'code' 0 = a tag answered (needs the protocol's field-12 decode);
        3 = nothing in the field right now."""
        resp = self._req(idx, 'filament_identify', {'index': slot})
        if not isinstance(resp, dict):
            return None
        res = resp.get('result')
        return res if isinstance(res, dict) else None

    @staticmethod
    def _answered(res):
        if not isinstance(res, dict):
            return False
        if res.get('code', 0) != 0:
            return False
        return bool(res.get('sku') or res.get('type'))

    @staticmethod
    def _is_uid_read(res):
        try:
            return int((res.get('tag') or {}).get('field2', 0)) == UID_VERSION
        except (TypeError, ValueError):
            return False

    def _rc(self, idx, slot, op, a1=0, a2=0):
        """One RC522 sub-command through the identify tunnel. `idx` is the
        ACE DEVICE index (which unit) - send_request_to's first arg; the
        packed sub-command's bit24 is the reader (slot>>1, the antenna pair
        WITHIN that unit). Passing slot as the device index queried the
        wrong ACE entirely (HW 2026-08-30: reads worked only where
        idx==slot by coincidence). Returns the op's byte result (response
        'code', field 12) or None."""
        reader = 1 if slot >= 2 else 0
        packed = (RC_BIT31 | (reader << 24) | ((op & 0xFF) << 16)
                  | ((a1 & 0x3F) << 8) | (a2 & 0xFF))
        tries = 1 + (RC_IDEMPOTENT_RETRIES if op in RC_IDEMPOTENT_OPS else 0)
        for attempt in range(tries):
            if attempt:
                self._pause(0.01)
            resp = self._req(idx, 'filament_identify', {'index': packed})
            if not isinstance(resp, dict):
                continue
            res = resp.get('result')
            if not isinstance(res, dict):
                continue
            try:
                return int(res.get('code', 0)) & 0xFF
            except (TypeError, ValueError):
                continue
        return None

    def _probe_page(self, idx, slot):
        """SELECT + TWO page-0 READs: True only when both come back as a
        full 16-byte frame (rx_bits 0x80 inside _rc_read_page). The
        read-quality probe of _centre_on_read - a card answering SELECT at
        the field edge still fails this. Two frames, not one: a marginal
        spot returns a single lucky frame and then flickers (HW 2026-09-06
        09:51: probe at -10 passed on one frame, the stable-UID rounds
        then alternated fragments and frames, NDEF empty; +20 read clean
        on the retry). Costs one READ (~0.2 s) per probe."""
        if not self._rc_select(idx, slot):
            return False
        self._rc_setup_crc(idx, slot)
        if not any(self._rc_read_page(idx, slot, 0)):
            return False
        return any(self._rc_read_page(idx, slot, 0))

    def _centre_on_read(self, idx, slot, respond):
        """READ-guided centering after a SELECT hit: walk PROBE_OFFSETS
        (units relative to the hit point) and stop at the first position
        where a page read comes back whole; the lane is LEFT there.
        Returns the signed net units moved (callers add it to their
        depth/rolled accounting). No full frame anywhere -> the lane ends
        at the last offset and the caller's own read retries take over."""
        pos = 0
        for off in PROBE_OFFSETS:
            delta = off - pos
            if delta:
                if not self._move(idx, slot, abs(delta),
                                  SEARCH_MODE if delta > 0 else RESTORE_MODE,
                                  respond):
                    break
                self._pause(abs(delta) / float(PARK_SPEED) + 0.5)
                pos = off
            ok = self._probe_page(idx, slot)
            if respond and self._debug:
                respond('rc522[dbg] centre probe at %+d: %s'
                        % (off, 'full page' if ok else 'no page'))
            if ok:
                if off:
                    respond('rc522: tag centred at %+d units from the hit'
                            % off)
                return pos
        respond('rc522: no clean page within %+d..%+d units of the hit - '
                'reading anyway' % (min(PROBE_OFFSETS), max(PROBE_OFFSETS)))
        return pos

    def _rc_select(self, idx, slot):
        """op 6: power the reader + REQA/anticollision/SELECT. Returns True
        when a card is in the field (status 0) - works for ANY ISO14443A
        tag, unlike the firmware identify."""
        return self._rc(idx, slot, 6) == SELECT_OK

    def _rc_setup_crc(self, idx, slot):
        v = self._rc(idx, slot, 0, REG_TXMODE)
        if v is not None:
            self._rc(idx, slot, 1, REG_TXMODE, v | 0x80)
        v = self._rc(idx, slot, 0, REG_RXMODE)
        if v is not None:
            self._rc(idx, slot, 1, REG_RXMODE, v | 0x80)
        self._rc(idx, slot, 1, REG_BITFRAMING, 0x00)

    def _rc_read_page(self, idx, slot, page, dbg=None):
        """One NTAG READ (0x30) of `page`: returns the 16 bytes it yields
        (pages page..page+3) as a list. The proven raw path (HW: UID read
        works). `dbg` logs the raw reply when set."""
        for i, b in enumerate([0x30, page & 0xFF]):
            self._rc(idx, slot, 2, i, b)
        st = self._rc(idx, slot, 3, 2, PCD_TRANSCEIVE)
        bits = self._rc(idx, slot, 5, 0)
        if bits != 0x80:
            if dbg:
                dbg('rc522[dbg] READ p%d: status=%s rx_bits=%s -> no reply'
                    % (page, self._hx(st), self._hx(bits)))
            return [0] * 16
        pg = []
        for i in range(16):
            b = self._rc(idx, slot, 4, i)
            if b is None:
                if dbg:
                    dbg('rc522[dbg] READ p%d: byte %d lost -> no reply'
                        % (page, i))
                logging.info('[multiACE] [rc522] page %d read: byte %d '
                             'reply lost - treating as no reply'
                             % (page, i))
                return [0] * 16
            pg.append(b)
        if dbg:
            dbg('rc522[dbg] READ p%d: status=%s rx_bits=%s bytes=%s'
                % (page, self._hx(st), self._hx(bits), bytes(pg).hex()))
        return pg

    def _rc_read_userdata(self, idx, slot, respond=None, first=4, last=39):
        """Read the NTAG user pages (4-39 = the NDEF area) as bytes. READ
        returns 4 pages at a time, so step by 4. Logs each line when
        `respond` is given (DUMP): the raw bytes for schema work."""
        data = []
        if respond:
            respond('rc522: dumping pages %d-%d' % (first, last))
        p = first
        while p <= last:
            pg = self._rc_read_page(idx, slot, p)
            data += pg
            if respond:
                respond('rc522: p%02d-%02d %s' % (p, p + 3, bytes(pg).hex()))
            p += 4
        return bytes(data)

    @staticmethod
    def _openspool_decode(data):
        """Parse an OpenSpool NDEF tag (openspool.io): NDEF-message TLV
        (0x03) -> MIME record 'application/json' -> JSON with type /
        color_hex / brand / min_temp / max_temp. Verified against a real
        tag (HW 2026-08-31: PLA 925B5B). Returns an identity dict or None
        (not OpenSpool / unparseable). Robust: skips NULL and lock/memory
        TLVs, tolerates the 3-byte length form."""
        try:
            i, n = 0, len(data)
            while i < n:
                t = data[i]
                if t == 0x00:
                    i += 1; continue
                if t == 0x03:
                    break
                if t == 0xFE:
                    return None
                if t in (0x01, 0x02):
                    i += 2 + data[i + 1]; continue
                return None
            else:
                return None
            ln = data[i + 1]
            if ln == 0xFF:
                ln = (data[i + 2] << 8) | data[i + 3]; p = i + 4
            else:
                p = i + 2
            msg = data[p:p + ln]
            hdr = msg[0]
            tl = msg[1]
            if hdr & 0x10:
                pl = msg[2]; off = 3
            else:
                pl = int.from_bytes(msg[3:7], 'big'); off = 6
            typ = msg[off:off + tl]; off += tl
            payload = msg[off:off + pl]
            if typ != b'application/json':
                return None
            j = json.loads(payload.decode('utf-8', 'replace'))
            if str(j.get('protocol', '')).lower() != 'openspool':
                return None
            return {
                'material': (j.get('type') or '').strip(),
                'color': (j.get('color_hex') or '').lstrip('#').upper()[:6],
                'vendor': (j.get('brand') or '').strip(),
                'min_temp': j.get('min_temp'),
                'max_temp': j.get('max_temp'),
            }
        except (IndexError, ValueError, TypeError, UnicodeError):
            return None

    @staticmethod
    def _openspool_encode(material, color_hex, brand='',
                          min_temp='', max_temp='', version='1.0'):
        """Inverse of _openspool_decode: build the NDEF-message TLV bytes
        for an OpenSpool tag. Byte-identical structure to a real tag (HW
        2026-08-31), padded to a 4-byte page boundary. Compact JSON
        (no spaces) to keep it inside NTAG213's 144-byte user area."""
        obj = {"protocol": "openspool", "version": version,
               "type": material or '',
               "color_hex": (color_hex or '').lstrip('#').upper()[:6],
               "brand": brand or ''}
        if str(min_temp or ''):
            obj["min_temp"] = str(min_temp)
        if str(max_temp or ''):
            obj["max_temp"] = str(max_temp)
        payload = json.dumps(obj, separators=(',', ':')).encode('utf-8')
        typ = b'application/json'
        rec = bytes([0xD2, len(typ), len(payload)]) + typ + payload
        if len(rec) < 0xFF:
            tlv = bytes([0x03, len(rec)]) + rec
        else:
            tlv = (bytes([0x03, 0xFF, (len(rec) >> 8) & 0xFF, len(rec) & 0xFF])
                   + rec)
        tlv += bytes([0xFE])
        if len(tlv) % 4:
            tlv += bytes(4 - (len(tlv) % 4))
        return tlv

    ANY_MAGIC = b'\x7b\x00'
    ANY_VERSION = 101
    ANY_PAGES = 28

    @staticmethod
    def _any_str(v, n=20):
        b = (v or '').encode('utf-8', 'replace')[:n - 1]
        return b + bytes(n - len(b))

    @classmethod
    def _anycubic_encode(cls, sku, material, subtype='', brand='',
                         color_hex='', min_temp='', max_temp='',
                         weight_g=0):
        import struct
        typ = (material or '').strip()
        if (subtype or '').strip():
            typ = '%s %s' % (typ, subtype.strip())
        col = (color_hex or '').lstrip('#')
        try:
            r, g, b = (int(col[0:2], 16), int(col[2:4], 16),
                       int(col[4:6], 16)) if len(col) == 6 else (0, 0, 0)
        except ValueError:
            r, g, b = 0, 0, 0
        def _u16(v):
            try:
                return max(0, min(65535, int(float(v))))
            except (TypeError, ValueError):
                return 0
        out = bytearray(cls.ANY_PAGES * 4)
        out[0:4] = cls.ANY_MAGIC + struct.pack('<H', cls.ANY_VERSION)
        out[4:24] = cls._any_str(sku)
        out[24:44] = cls._any_str(brand)
        out[44:64] = cls._any_str(typ)
        out[64:68] = bytes([0xFF, b, g, r])
        out[80:84] = struct.pack('<HH', _u16(min_temp), _u16(max_temp))
        out[104:108] = struct.pack('<HH', 175, 0)
        out[108:112] = struct.pack('<HH', _u16(weight_g), 0)
        return bytes(out)

    @classmethod
    def _anycubic_decode(cls, data):
        """Parse the Anycubic layout (for the write verify). Returns
        {'sku','brand','material','color'} or None."""
        import struct
        if len(data) < 68 or bytes(data[0:2]) != cls.ANY_MAGIC:
            return None
        def _s(a):
            return bytes(data[a:a + 20]).split(b'\x00', 1)[0].decode(
                'utf-8', 'replace')
        a_, b, g, r = data[64:68]
        return {'version': struct.unpack('<H', bytes(data[2:4]))[0],
                'sku': _s(4), 'brand': _s(24), 'material': _s(44),
                'color': '%02X%02X%02X' % (r, g, b)}

    def _rc_user_page_limit(self, idx, slot):
        """Highest writable user page from the CC (page 3, in the page-0
        READ). NTAG213 CC e1 10 12 00 -> 0x12*8=144 B = pages 4..39.
        Returns (last_user_page) or None if the CC is unreadable."""
        pg = self._rc_read_page(idx, slot, 0)
        if len(pg) < 16 or pg[12] != 0xE1:
            return None
        user_bytes = pg[14] * 8
        return 4 + (user_bytes // 4) - 1

    def _rc_write_page(self, idx, slot, page, four, respond=None,
                       readback=True):
        """NTAG WRITE (0xA2) of one 4-byte page, verified by READ-BACK.
        The 4-bit ACK through the op tunnel is UNRELIABLE - HW 2026-08-31:
        page 4 was PROVEN written (the next run's backup read exactly the
        new bytes, twice, byte-identical) while the tunnel reported
        bits=0x00 / stale-buffer ack. The NTAG sends its ACK only after
        the ~4 ms programming time and the firmware's transceive does not
        wait that long. So the ACK is logged as diagnostics only and the
        page is READ BACK and compared - ground truth over handshake,
        via the HW-proven read path. One full write+readback retry (a
        field-edge read can drop a byte, same class as the UID flap).
        With readback=False the page is written BLIND (no read, no
        retry, returns True) - the caller then verifies a whole 4-page
        chunk with ONE read (2.5x faster than per-page readback) and
        falls back to this readback path only for a mismatching chunk.
        HARD guard: never page < 4 (UID/lock/OTP/CC) - the caller also
        caps at the user limit so config/lock pages past the data area
        are never touched."""
        if page < 4 or len(four) != 4:
            if respond:
                respond('rc522: refusing to write page %d (protected)' % page)
            return False
        want = list(four)
        for attempt in range(2):
            rx = self._rc(idx, slot, 0, REG_RXMODE)
            if rx is not None and rx & 0x80:
                self._rc(idx, slot, 1, REG_RXMODE, rx & 0x7F)
            for i, b in enumerate([0xA2, page & 0xFF] + want):
                self._rc(idx, slot, 2, i, b)
            self._rc(idx, slot, 3, 6, PCD_TRANSCEIVE)
            bits = self._rc(idx, slot, 5, 0)
            ack = self._rc(idx, slot, 4, 0)
            if rx is not None and rx & 0x80:
                self._rc(idx, slot, 1, REG_RXMODE, rx)
            if not readback:
                if respond and self._debug:
                    respond('rc522[dbg] WRITE p%d %s -> bits=%s ack=%s '
                            '(chunk-verify)'
                            % (page, bytes(four).hex(), self._hx(bits),
                               self._hx(ack)))
                return True
            self._pause(0.02)
            self._rc(idx, slot, 6)
            back = self._rc_read_page(idx, slot, page)
            ok = (back[:4] == want)
            if respond and self._debug:
                respond('rc522[dbg] WRITE p%d %s -> bits=%s ack=%s '
                        'readback=%s %s'
                        % (page, bytes(four).hex(), self._hx(bits),
                           self._hx(ack), bytes(back[:4]).hex(),
                           'OK' if ok else 'FAIL'))
            if ok:
                return True
        return False

    def _fw_identify_busy(self, idx):
        """Is the FIRMWARE running its own tag identify on any slot of
        this unit (slot status 'identifying')? Its RC522 driver shares the
        bus with our tunnel ops: HW 2026-09-02 07:30, every corrupted UID
        read of the round (byte0 53->01, byte1 e9->00, a user-data page in
        place of page 0) fell inside a neighbour's 'identifying' window."""
        try:
            info = self.ace._info_per_ace.get(idx) or {}
            for sl in info.get('slots') or []:
                if str(sl.get('status', '')).lower() == 'identifying':
                    return True
        except Exception:
            pass
        return False

    def _wait_fw_identify(self, idx, max_s=8.0):
        """Hold our reader ops while the firmware identify runs (bounded).
        Returns True when it waited."""
        t0 = self.ace.reactor.monotonic()
        waited = False
        while (self._fw_identify_busy(idx)
               and self.ace.reactor.monotonic() - t0 < max_s):
            self._pause(0.5)
            waited = True
        return waited

    def _rc_stable_uid(self, idx, slot, respond=None):
        """Anticollision confirm with tolerance for a single flaky read:
        require SELECT ok + two consecutive IDENTICAL non-empty UID reads,
        retrying up to 3 rounds. A read right after the tag enters the
        field can drop a byte to 0x00 (HW 2026-08-31: 535B2DF4-00-0001 vs
        the real ...-62-0001 one step apart) - that is field-edge noise,
        not a neighbour tag (a neighbour never shares 6 of 7 UID bytes),
        and must not refuse the write. A REAL collision / flapping UID
        stays unstable across all rounds and is still refused. Returns
        (uid, None) on success or ('', (uid1, uid2, sel)) for the
        refusal message."""
        detail = ('?', '?', None)
        self._wait_fw_identify(idx)
        for attempt in range(4):
            if attempt:
                self._pause(0.3)
            uid1 = self._rc_read_uid(idx, slot, respond)
            sel = self._rc(idx, slot, 6)
            uid2 = self._rc_read_uid(idx, slot, respond)
            if respond and self._debug:
                respond('rc522[dbg] stable-uid round %d: uid1=%s sel=%s '
                        'uid2=%s' % (attempt + 1, uid1 or '-',
                                     self._hx(sel), uid2 or '-'))
            if sel == SELECT_OK and uid1 and uid1 == uid2:
                return uid1, None
            detail = (uid1 or '?', uid2 or '?', sel)
        return '', detail

    def _rc_read_uid(self, idx, slot, respond=None):
        """Read an NTAG's UID from pages 0-1 after a SELECT. A READ (0x30)
        of page 0 returns 16 bytes = pages 0..3; the 7-byte UID is bytes
        0-2 (page 0, byte 3 is BCC0) + bytes 4-6 (page 1). Returns the UID
        hex string, or '' when the read looks empty/invalid. With
        `respond` set (DEBUG), logs each raw step."""
        dbg = respond if self._debug else None
        tx = self._rc(idx, slot, 0, REG_TXMODE)
        rx = self._rc(idx, slot, 0, REG_RXMODE)
        bf = self._rc(idx, slot, 0, REG_BITFRAMING)
        if dbg:
            dbg('rc522[dbg] reader=%d before: TxMode=%s RxMode=%s '
                'BitFraming=%s' % (1 if slot >= 2 else 0,
                                   self._hx(tx), self._hx(rx), self._hx(bf)))
        self._rc_setup_crc(idx, slot)
        if dbg:
            tx2 = self._rc(idx, slot, 0, REG_TXMODE)
            rx2 = self._rc(idx, slot, 0, REG_RXMODE)
            dbg('rc522[dbg] after CRC setup: TxMode=%s RxMode=%s'
                % (self._hx(tx2), self._hx(rx2)))
        pg = self._rc_read_page(idx, slot, 0, dbg)
        if not any(pg):
            return ''
        bcc0 = 0x88 ^ pg[0] ^ pg[1] ^ pg[2]
        bcc1 = pg[4] ^ pg[5] ^ pg[6] ^ pg[7]
        if pg[3] != bcc0 or pg[8] != bcc1:
            logging.info('[multiACE] [rc522] UID read rejected (BCC '
                         'mismatch) ACE %d slot %d: %s',
                         idx, slot, bytes(pg[0:9]).hex())
            return ''
        uid = bytes(pg[0:3] + pg[4:8])
        if not any(uid):
            return ''
        return uid.hex().upper()

    @staticmethod
    def _hx(v):
        return '?' if v is None else '0x%02X' % (v & 0xFF)

    def read_slot(self, idx, slot, respond, max_mm=PARK_MAX_MM):
        """Park the slot's tag in the antenna field and read it UID-FIRST
        (Dirk 2026-09-01): op 6 SELECT is the ONE probe - it sees every
        ISO14443A card (Anycubic NTAG, OpenSpool, Bambu MIFARE). At the hit
        the UID is read first (the uniform per-chip binding key - factory
        Anycubic skus are per-ARTICLE and cannot bind a spool), then
        identify harvests the Anycubic identity, then the NDEF pages
        (OpenSpool). The firmware identify is no longer its own exit with
        its own sku-binding. Runs in a greenlet (reactor.pause fine); lane
        restored in finally. Caller-facing guards live in ace.py."""
        ace = self.ace
        rolled = 0
        try:
            neighbour = slot ^ 1
            blocking_uid = ''
            if self._rc_select(idx, slot):
                n_status = ace._v2_get_slot_status(idx, neighbour)
                if (n_status is not None
                        and ace._is_empty_status(str(n_status))):
                    respond('rc522: card already in the field - no rotation '
                            'needed')
                    rolled += self._read_card(idx, slot, respond)
                    return
                uid_a, _bd = self._rc_stable_uid(idx, slot)
                verdict = None
                if not uid_a:
                    if not self._clear_neighbour(idx, slot, respond):
                        respond('rc522: a card already answers and the '
                                'neighbouring bay (slot %d) may hold a '
                                'spool - and its UID is unreadable, so the '
                                'two cannot be told apart. Remove or rotate '
                                'the neighbour spool a bit, then retry.'
                                % ace._disp(neighbour))
                        return
                    if self._rc_select(idx, slot):
                        uid_a, _bd = self._rc_stable_uid(idx, slot)
                        verdict = 'ours' if uid_a else None
                else:
                    n_uid = self._known_neighbour_uid(idx, slot)
                    if n_uid and uid_a == n_uid:
                        verdict = 'neighbour'
                    elif n_uid:
                        verdict = 'ours'
                    else:
                        verdict = self._rotation_test(idx, slot, uid_a,
                                                      respond)
                        if verdict is None:
                            return
                        if verdict == 'neighbour':
                            rolled += START_PROBE_MM
                if verdict == 'ours':
                    respond('rc522: card %s is this slot\'s tag - reading '
                            'it' % uid_a)
                    rolled += self._read_card(idx, slot, respond)
                    return
                if verdict == 'neighbour':
                    if self._clear_neighbour(idx, slot, respond):
                        respond('rc522: searching for the tag (rotating up '
                                'to %d mm)' % max_mm)
                    else:
                        blocking_uid = uid_a
                        respond('rc522: card %s is the neighbour\'s - '
                                'searching for a second tag' % uid_a)
            else:
                respond('rc522: searching for the tag (rotating up to %d '
                        'mm)' % max_mm)
            found = False
            while rolled < max_mm:
                if not self._move(idx, slot, PARK_STEP_MM, SEARCH_MODE,
                                  respond):
                    return
                rolled += PARK_STEP_MM
                self._pause(PARK_STEP_MM / float(PARK_SPEED) + 0.6)
                if self._rc_select(idx, slot):
                    if blocking_uid:
                        uid_now, _d = self._rc_stable_uid(idx, slot)
                        if uid_now == blocking_uid:
                            continue
                    found = True
                    break
                if rolled % 100 == 0:
                    respond('rc522: ... %d mm, no answer yet' % rolled)
            if found:
                rolled += self._centre_on_read(idx, slot, respond)
                rolled += self._read_card(idx, slot, respond)
            elif blocking_uid:
                respond('rc522: only the neighbour card (UID %s) stayed in '
                        'range over %d mm - this slot\'s tag could not be '
                        'told apart. Lane is being restored.'
                        % (blocking_uid, max_mm))
            else:
                respond('rc522: no tag answered within %d mm - either the '
                        'spool carries no (working) tag, or it is on the '
                        'far face / needs more than one revolution. Lane '
                        'is being restored.' % max_mm)
        finally:
            if rolled:
                if self._move(idx, slot, rolled, RESTORE_MODE, respond):
                    self._pause(rolled / float(PARK_SPEED) + 1.0)
                    respond('rc522: lane restored (%d mm)' % rolled)
                else:
                    respond('rc522: WARNING - restore feed rejected, lane '
                            'is %d mm short. Re-seat the spool or run a '
                            'load to re-feed.' % rolled)

    def read_slot_transport(self, idx, slot, respond, start_depth=0,
                            net_target=390, sweep_mm=700):
        """The INSERT read (Dirk 2026-09-01, 'zuerst der Move, 700 mit
        Rueckzug'): ONE long forward sweep of sweep_mm - that is more than
        a full spool revolution (~600 units on a full 1kg spool, fewer
        units per revolution as it empties), so the tag MUST pass the
        antenna - listening the whole way. Hit -> brake, read, then ONE
        correction move to net_target (forward or back, wherever the stop
        landed). No hit -> one rollback to net_target. No stop-and-go
        poking. start_depth = the verified abort's pulled-in amount (None
        = the firmware procedure completed, spool already AT net_target -
        the sweep then runs from there and rolls back further)."""
        ace = self.ace
        neighbour = slot ^ 1
        blocking_uid = self._known_neighbour_uid(idx, slot)
        depth = start_depth if start_depth is not None else net_target
        if self._rc_select(idx, slot):
            n_status = ace._v2_get_slot_status(idx, neighbour)
            if (n_status is not None
                    and ace._is_empty_status(str(n_status))):
                respond('rc522: card already in the field - reading, then '
                        'transporting to park depth')
                depth += self._read_card(idx, slot, respond)
                self._correct_to(idx, slot, net_target - depth, respond)
                return
            uid_a, _bd = self._rc_stable_uid(idx, slot)
            verdict = None
            if not uid_a:
                if not self._clear_neighbour(idx, slot, respond):
                    try:
                        n_busy = (ace._v2_get_slot_status(idx, neighbour)
                                  in V2_ACTIVE_MOTION_STATES)
                    except Exception:
                        n_busy = False
                    self._correct_to(idx, slot, net_target - depth, respond)
                    if n_busy:
                        respond('rc522: neighbour slot %d is being inserted '
                                'right now - this read is queued behind it'
                                % ace._disp(neighbour))
                        return 'deferred'
                    respond('rc522: a card already answers but its UID is '
                            'unreadable - cannot tell it from the '
                            'neighbour. Remove or rotate the neighbour '
                            'spool a bit, then retry.')
                    return
                if self._rc_select(idx, slot):
                    uid_a, _bd = self._rc_stable_uid(idx, slot)
                    verdict = 'ours' if uid_a else None
            else:
                verdict = self._rotation_test(idx, slot, uid_a, respond)
                if verdict is None:
                    return
                if verdict == 'neighbour':
                    depth += START_PROBE_MM
            if verdict == 'neighbour':
                if self._clear_neighbour(idx, slot, respond):
                    blocking_uid = ''
                else:
                    blocking_uid = uid_a
                    respond('rc522: card %s is the neighbour\'s - '
                            'listening for a different one' % uid_a)
            elif verdict == 'ours':
                respond('rc522: card %s is this slot\'s tag - reading it'
                        % uid_a)
                depth += self._read_card(idx, slot, respond)
                self._correct_to(idx, slot, net_target - depth, respond)
                return
        swept = 0
        found = False
        speed = PARK_SPEED
        slow_pass = False
        while swept < sweep_mm and not found:
            leg = sweep_mm - swept
            if not self._move(idx, slot, leg, SEARCH_MODE, respond,
                              speed=speed):
                break
            deadline = (self.ace.reactor.monotonic()
                        + leg / float(speed) + 1.5)
            stopped = False
            while self.ace.reactor.monotonic() < deadline:
                self._pause(0.1)
                if not self._rc_select(idx, slot):
                    continue
                if blocking_uid:
                    if self._rc_read_uid(idx, slot) == blocking_uid:
                        continue
                ace._stop_feeding(slot, idx=idx)
                stopped = True
                self._pause(0.3)
                moved = ace._read_decoder(idx, slot)
                d = moved if (moved is not None
                              and 0 <= moved <= leg) else leg
                swept += d
                depth += d
                uid_now, _d2 = self._rc_stable_uid(idx, slot)
                if blocking_uid and uid_now == blocking_uid:
                    break
                _c = self._centre_on_read(idx, slot, respond)
                swept += _c
                depth += _c
                if self._neighbour_occupied(idx, slot):
                    uid_c, _d3 = self._rc_stable_uid(idx, slot)
                    n_uid = self._known_neighbour_uid(idx, slot)
                    verdict = None
                    probed = False
                    if uid_c and blocking_uid and uid_c == blocking_uid:
                        verdict = 'neighbour'
                    elif not uid_c or not n_uid or uid_c == n_uid:
                        verdict = self._rotation_test(idx, slot, uid_c,
                                                      respond)
                        if verdict is None:
                            break
                        probed = True
                        if verdict == 'neighbour':
                            swept += START_PROBE_MM
                            depth += START_PROBE_MM
                    if verdict == 'neighbour':
                        respond('rc522: %s at ~%d units is the parked '
                                'neighbour tag'
                                % ('unreadable card' if not uid_c
                                   else 'card %s' % uid_c, depth))
                        if not self._clear_neighbour(idx, slot, respond):
                            blocking_uid = uid_c or blocking_uid
                        sweep_mm += max(_c, 0) + (START_PROBE_MM
                                                  if probed else 0)
                        break
                respond('rc522: tag found during transport (~%d units in)'
                        % depth)
                depth += self._read_card(idx, slot, respond)
                found = True
                break
            if not stopped:
                swept += leg
                depth += leg
            if swept >= sweep_mm and not found and not slow_pass:
                slow_pass = True
                speed = max(10, PARK_SPEED // 2)
                swept = 0
                respond('rc522: nothing in the fast pass - sweeping again '
                        'at %d units/s' % speed)
        if not found:
            if blocking_uid:
                respond('rc522: only the neighbour card (UID %s) answered '
                        'over %d units - this slot\'s tag could not be '
                        'told apart.' % (blocking_uid, sweep_mm))
            else:
                respond('rc522: no tag answered over %d units - the spool '
                        'may carry no (working) tag.' % sweep_mm)
        self._correct_to(idx, slot, net_target - depth, respond)

    def _correct_to(self, idx, slot, delta, respond):
        """One paced correction move: positive delta feeds forward,
        negative rolls back. Small offsets (<=20) are left alone."""
        delta = int(delta)
        if abs(delta) <= 20:
            return
        mode = SEARCH_MODE if delta > 0 else RESTORE_MODE
        length = abs(delta)
        if self._move(idx, slot, length, mode, respond):
            self._pause(length / float(PARK_SPEED) + 1.0)
            respond('rc522: parked at target depth (%s %d)'
                    % ('fed' if delta > 0 else 'rolled back', length))

    def write_slot(self, idx, slot, ident, respond, max_mm=PARK_MAX_MM,
                   fmt='openspool'):
        """Park the slot's tag, then write an OpenSpool NDEF onto it.
        Greenlet context (reactor.pause OK). Safety, in order of regret:
        (1) only ever writes user pages (>=4), capped at the CC user limit -
        UID/lock/OTP/CC/config pages are never touched; (2) refuses on an
        ambiguous field (shared-antenna collision / unstable UID) so the
        WRONG tag is never written (Simon-CR rule); (3) reads a backup
        first; (4) verifies by read-back. Only NTAG (OpenSpool/blank) - a
        MIFARE (Bambu/Snapmaker) never SELECTs cleanly here and is refused
        upstream. Lane restored in finally."""
        ace = self.ace
        rolled = 0
        try:
            uid1 = ''
            announced = False
            while True:
                if self._rc_select(idx, slot):
                    uid1, detail = self._rc_stable_uid(idx, slot)
                    n_occ = self._neighbour_occupied(idx, slot)
                    if not uid1:
                        if n_occ and self._clear_neighbour(idx, slot,
                                                           respond):
                            continue
                        respond('rc522: ambiguous field (SELECT=%s UID '
                                '%s/%s after 3 tries) - a neighbour tag '
                                'may be in range. Rotate the neighbour '
                                'spool away and retry; not writing.'
                                % (self._hx(detail[2]), detail[0],
                                   detail[1]))
                        return
                    if n_occ:
                        n_uid = self._known_neighbour_uid(idx, slot)
                        if n_uid and uid1 != n_uid:
                            verdict = 'ours'
                        else:
                            verdict = self._rotation_test(idx, slot, uid1,
                                                          respond)
                            if verdict is None:
                                return
                            if verdict == 'neighbour':
                                rolled += START_PROBE_MM
                        if verdict == 'neighbour':
                            respond('rc522: card %s is the neighbour\'s '
                                    'tag' % uid1)
                            if not self._clear_neighbour(idx, slot,
                                                         respond):
                                respond('rc522: cannot clear the field - '
                                        'not writing.')
                                return
                            uid1 = ''
                            continue
                    break
                if rolled >= max_mm:
                    break
                if not announced:
                    respond('rc522: searching for the tag to write (up to '
                            '%d mm)' % max_mm)
                    announced = True
                if not self._move(idx, slot, PARK_STEP_MM, SEARCH_MODE,
                                  respond):
                    return
                rolled += PARK_STEP_MM
                self._pause(PARK_STEP_MM / float(PARK_SPEED) + 0.6)
                if rolled % 100 == 0:
                    respond('rc522: ... %d mm' % rolled)
            if not uid1:
                respond('rc522: no tag found within %d mm - cannot write.'
                        % max_mm)
                return
            rolled += self._centre_on_read(idx, slot, respond)
            uid_c = ''
            for _try in range(3):
                if _try:
                    self._pause(1.0)
                    if self._move(idx, slot, CENTER_MM, SEARCH_MODE,
                                  respond):
                        self._pause(CENTER_MM / float(PARK_SPEED) + 0.5)
                        rolled += CENTER_MM
                self._rc_select(idx, slot)
                uid_c, _dc = self._rc_stable_uid(idx, slot)
                if uid_c:
                    break
            if uid_c != uid1:
                respond('rc522: tag %s changed to %s after centering - '
                        'ambiguous field, not writing.'
                        % (uid1, uid_c or '?'))
                return
            if fmt == 'anycubic':
                data = self._anycubic_encode(
                    (ident.get('sku') or '').strip() or uid1,
                    ident.get('material', ''), ident.get('subtype', ''),
                    ident.get('vendor', ''), ident.get('color', ''),
                    ident.get('min_temp', ''), ident.get('max_temp', ''),
                    ident.get('weight_g', 0))
            else:
                data = self._openspool_encode(
                    ident.get('material', ''), ident.get('color', ''),
                    ident.get('vendor', ''), ident.get('min_temp', ''),
                    ident.get('max_temp', ''))
            npages = len(data) // 4
            last_user = self._rc_user_page_limit(idx, slot)
            if last_user is not None and 4 + npages - 1 > last_user:
                respond('rc522: needs %d pages but the tag ends at page %d - '
                        'refusing (would overflow into config/lock pages).'
                        % (npages, last_user))
                return
            backup = self._rc_read_userdata(idx, slot, respond=None,
                                            first=4, last=4 + npages - 1)
            respond('rc522: writing %s to UID %s (%d pages). backup=%s'
                    % ('Anycubic' if fmt == 'anycubic' else 'OpenSpool',
                       uid1, npages, backup.hex()))
            rb = b''
            for base in range(0, npages, 4):
                want = data[base * 4:min((base + 4) * 4, len(data))]
                for j in range(len(want) // 4):
                    k = base + j
                    self._rc_write_page(idx, slot, 4 + k,
                                        data[k*4:k*4+4], respond,
                                        readback=False)
                self._pause(0.02)
                self._rc(idx, slot, 6)
                pg = self._rc_read_page(idx, slot, 4 + base)
                got = bytes(pg[:len(want)])
                if got != want:
                    respond('rc522: pages %d-%d mismatch after write - '
                            'retrying per page'
                            % (4 + base, 4 + base + len(want) // 4 - 1))
                    for j in range(len(want) // 4):
                        k = base + j
                        if not self._rc_write_page(idx, slot, 4 + k,
                                                   data[k*4:k*4+4], respond):
                            respond('rc522: WRITE failed at page %d - '
                                    'aborting. The tag may be partly '
                                    'written; re-run to finish.' % (4 + k))
                            return
                    got = want
                rb += got
            if fmt == 'anycubic':
                dec = self._anycubic_decode(rb)
                want_mat = (ident.get('material') or '').strip()
                if (ident.get('subtype') or '').strip():
                    want_mat = '%s %s' % (want_mat, ident['subtype'].strip())
            else:
                dec = self._openspool_decode(rb)
                want_mat = ident.get('material')
            want_col = (ident.get('color', '') or '').lstrip('#').upper()[:6]
            if (dec and dec.get('material') == want_mat
                    and (dec.get('color') or '').upper() == want_col):
                respond('rc522: write VERIFIED - %s #%s (UID %s%s)'
                        % (dec['material'], dec.get('color') or '------',
                           uid1, (', sku %s' % dec.get('sku'))
                           if fmt == 'anycubic' else ''))
                try:
                    col = dec.get('color') or ''
                    rgb = ([int(col[0:2], 16), int(col[2:4], 16),
                            int(col[4:6], 16)] if len(col) == 6
                           else [0, 0, 0])
                    res = {'type': dec['material'], 'color': rgb,
                           'brand': (dec.get('brand') if fmt == 'anycubic'
                                     else ident.get('vendor', '')) or '',
                           'sku': (dec.get('sku') if fmt == 'anycubic'
                                   else uid1) or uid1,
                           'subtype': ''}
                    self._note_fmt(idx, slot, fmt)
                    ace._v2_store_filament_read(idx, slot, res, uid=uid1,
                                                fmt=fmt)
                    self._note_uid(idx, slot, uid1)
                    self._bind_uid(idx, slot, uid1, respond)
                except Exception as e:
                    respond('rc522: post-write ingest failed (tag is '
                            'written): %s' % e)
            else:
                respond('rc522: write done but VERIFY MISMATCH - read back %s. '
                        'Check the tag / re-run.' % dec)
        finally:
            if rolled:
                if self._move(idx, slot, rolled, RESTORE_MODE, respond):
                    self._pause(rolled / float(PARK_SPEED) + 1.0)
                    respond('rc522: lane restored (%d mm)' % rolled)
                else:
                    respond('rc522: WARNING - restore feed rejected, lane is '
                            '%d mm short. Re-seat the spool or run a load.'
                            % rolled)

    def _read_card(self, idx, slot, respond):
        """UID-first ingest of a card sitting in the field: stable UID ->
        identify harvest (Anycubic identity, or the Bambu UID sentinel) ->
        NDEF (OpenSpool). The UID bind runs LAST so it wins over the
        identity ingest's own sku-bind attempt - a factory Anycubic sku is
        per-ARTICLE (Dirk 2026-09-01) and must never be the binding key.
        Returns the units the lane was nudged forward (callers add them to
        their depth/restore accounting)."""
        ace = self.ace
        uid = ''
        os = None
        moved = 0
        for attempt in range(3):
            if attempt:
                self._pause(1.5)
                self._wait_fw_identify(idx)
                if attempt == 1:
                    if self._move(idx, slot, CENTER_MM, SEARCH_MODE, respond):
                        self._pause(CENTER_MM / float(PARK_SPEED) + 0.5)
                        moved += CENTER_MM
                else:
                    back = moved + CENTER_MM * 2
                    if back > 0 and self._move(idx, slot, back, 1, respond):
                        self._pause(back / float(PARK_SPEED) + 0.5)
                        moved -= back
                sel = self._rc_select(idx, slot)
                respond('rc522: read retry %d%s'
                        % (attempt + 1, '' if sel else ' (identify only)'))
                if not sel:
                    res = self._identify(idx, slot)
                    if self._answered(res):
                        break
                    res = None
                    continue
            uid, _detail = self._rc_stable_uid(idx, slot, respond)
            res = self._identify(idx, slot)
            if respond and self._debug:
                respond('rc522[dbg] identify: %s'
                        % ('answered' if self._answered(res) else 'nothing'))
            if self._answered(res):
                break
            os = self._read_openspool(idx, slot, respond)
            if os and os.get('material') and uid:
                break
            res = None
        if self._answered(res):
            if self._is_uid_read(res):
                uid = (res.get('sku') or '').strip() or uid
                respond('rc522: card UID %s (MIFARE, no readable pages)'
                        % (uid or '?'))
                self._note_fmt(idx, slot, 'mifare')
                self._note_uid(idx, slot, uid)
                self._bind_uid(idx, slot, uid, respond)
                return moved
            if (res.get('type') or '').strip():
                respond('rc522: anycubic tag: %s %s (sku %s, UID %s)'
                        % (res.get('type', ''), res.get('brand', ''),
                           (res.get('sku') or '-'), uid or '?'))
                if self._dump:
                    try:
                        self._rc(idx, slot, 6)
                        self._rc_read_userdata(idx, slot, respond=respond)
                    except Exception as e:
                        respond('rc522: raw dump failed: %s' % e)
                _sid, _sp = (ace._spool_by_sku(uid) if uid
                             else (None, None))
                self._note_fmt(idx, slot, 'anycubic')
                if _sp is not None:
                    ace._v2_store_filament_read(idx, slot, res, uid=uid,
                                                prebound=_sp, fmt='anycubic')
                    self._note_uid(idx, slot, uid)
                    self._bind_uid(idx, slot, uid, respond)
                else:
                    ace._v2_store_filament_read(idx, slot, res, uid=uid,
                                                fmt='anycubic')
                    self._note_uid(idx, slot, uid)
                    self._bind_uid(idx, slot, uid, respond, unbind=False)
                return moved
        self._note_fmt(idx, slot, 'openspool' if (os and os.get('material'))
                       else ('unknown' if uid else ''))
        self._note_uid(idx, slot, uid)
        self._finish_present(idx, slot, uid, os, respond)
        return moved

    def _note_fmt(self, idx, slot, fmt):
        """Tag format of the last read at (idx, slot) - anycubic /
        openspool / mifare / unknown - for the web (Dirk 2026-09-02)."""
        if not fmt:
            return
        reg = getattr(self.ace, '_rc_last_fmt', None)
        if reg is None:
            reg = self.ace._rc_last_fmt = {}
        reg[(idx, slot)] = fmt

    def _note_uid(self, idx, slot, uid):
        """Remember the UID last read at (idx, slot) on the ace object
        (this reader is created per op). The insert sweep of the
        NEIGHBOUR slot uses it: the two slots of a pair share one antenna,
        and a parked neighbour tag at the field edge answers
        intermittently - it did not answer at sweep start, then stopped
        the sweep mid-way as a "found" tag (HW 2026-09-02 04:16, the one
        miss of the round). Knowing the neighbour's UID up front makes it
        a blocker from the first unit on."""
        if not uid:
            return
        reg = getattr(self.ace, '_rc_last_uid', None)
        if reg is None:
            reg = self.ace._rc_last_uid = {}
        reg[(idx, slot)] = uid
        try:
            self.ace._persist_tag_reads()
        except Exception:
            pass

    def _known_neighbour_uid(self, idx, slot):
        """UID of the tag last read in the neighbour slot, '' when the
        neighbour is empty or was never read this boot."""
        ace = self.ace
        neighbour = slot ^ 1
        n_status = ace._v2_get_slot_status(idx, neighbour)
        if n_status is not None and ace._is_empty_status(str(n_status)):
            return ''
        return (getattr(ace, '_rc_last_uid', None) or {}).get(
            (idx, neighbour), '')

    def _neighbour_occupied(self, idx, slot):
        ace = self.ace
        n_status = ace._v2_get_slot_status(idx, slot ^ 1)
        return not (n_status is not None
                    and ace._is_empty_status(str(n_status)))

    def _rotation_test(self, idx, slot, uid, respond):
        """A card answers and the neighbour is occupied - whose tag is it?
        Move OUR lane START_PROBE_MM: our rotation cannot move the
        neighbour's tag, so the same UID still answering = stationary =
        the neighbour ('neighbour', lane left START_PROBE_MM further in);
        gone or changed = it moved with us = ours ('ours', lane rolled
        back to where it answered). None = motor refused."""
        if not self._move(idx, slot, START_PROBE_MM, SEARCH_MODE, respond):
            return None
        self._pause(START_PROBE_MM / float(PARK_SPEED) + 0.6)
        if self._rc_select(idx, slot):
            if not uid:
                return 'neighbour'
            uid_b, _b2 = self._rc_stable_uid(idx, slot)
            if uid_b == uid:
                return 'neighbour'
        if self._move(idx, slot, START_PROBE_MM, RESTORE_MODE, respond):
            self._pause(START_PROBE_MM / float(PARK_SPEED) + 0.6)
        return 'ours'

    def _neighbour_movable(self, idx, slot):
        """May the neighbour lane be rotated? Only an IDLE, unloaded
        neighbour: never one feeding a head (its filament sits in the
        bowden/toolhead), never one in motion, never during a print."""
        ace = self.ace
        neighbour = slot ^ 1
        try:
            ps = ace.printer.lookup_object('print_stats', None)
            if ps is not None and (getattr(ps, 'state', '') or '').lower() \
                    in ('printing', 'paused'):
                return False
            if ace._get_heads_for_ace_slot(idx, neighbour):
                return False
            st = ace._v2_get_slot_status(idx, neighbour)
            if st in V2_ACTIVE_MOTION_STATES:
                return False
        except Exception:
            return False
        return True

    def _clear_neighbour(self, idx, slot, respond):
        """Rotate the NEIGHBOUR spool until its card has left the shared
        field: feed the neighbour lane CLEAR_STEP_MM at a time, SELECT
        after each step, done when it stayed silent CLEAR_SILENT_STEPS
        steps in a row. Returns True when the field is clear. A queued
        insert of that neighbour gets its remembered depth corrected by
        the amount moved."""
        neighbour = slot ^ 1
        if not self._neighbour_movable(idx, slot):
            respond('rc522: neighbour slot %d is loaded or busy - cannot '
                    'rotate it away' % self.ace._disp(neighbour))
            return False
        moved = 0
        silent = 0
        respond('rc522: rotating the neighbour spool (slot %d) out of the '
                'field' % self.ace._disp(neighbour))
        while moved < CLEAR_MAX_MM:
            if not self._move(idx, neighbour, CLEAR_STEP_MM, SEARCH_MODE,
                              respond):
                break
            moved += CLEAR_STEP_MM
            self._pause(CLEAR_STEP_MM / float(PARK_SPEED) + 0.4)
            if self._rc_select(idx, slot):
                silent = 0
            else:
                silent += 1
                if silent >= CLEAR_SILENT_STEPS:
                    break
        if moved:
            q = getattr(self.ace, '_insert_read_queue', None) or []
            for i, ent in enumerate(q):
                if ent[0] == idx and ent[1] == neighbour \
                        and isinstance(ent[2], (int, float)):
                    q[i] = (idx, neighbour, ent[2] + moved)
        ok = silent >= CLEAR_SILENT_STEPS
        respond('rc522: neighbour rotated %d units - field %s'
                % (moved, 'clear' if ok else 'STILL occupied'))
        return ok

    def _bind_uid(self, idx, slot, uid, respond, unbind=True):
        """Bind the slot by card UID - the uniform per-chip key. On no
        table hit, _spool_bind_by_tag emits the load-bearing 'matches no
        table entry' line the web backend adopts from via Spoolman
        card_uids (S50). unbind=False when another code of the same tag
        already decided the binding."""
        if not uid:
            return
        ace = self.ace
        try:
            bound = ace._spool_bind_by_tag(idx, slot, uid, unbind=unbind)
        except TypeError:
            bound = ace._spool_bind_by_tag(idx, slot, uid)
        if bound is not None:
            respond('rc522: bound to spool %s' % ace._spool_label(bound))

    def _read_openspool(self, idx, slot, respond):
        """Read the NTAG user pages of a present foreign card and decode
        OpenSpool. Logs the raw pages when DUMP is on (schema work).
        Returns the identity dict or None (not OpenSpool / MIFARE / empty)."""
        pages = self._rc_read_userdata(
            idx, slot,
            respond=(respond if (self._dump or self._debug) else None))
        return self._openspool_decode(pages)

    def _finish_present(self, idx, slot, uid, os, respond):
        """A foreign card was SELECTed (stage 2). With OpenSpool data decoded
        (material/color/brand from the NDEF), feed the FULL identity through
        the normal ingest (identity + display + bind + Spoolman kick), sku =
        the card UID so card_uids binding still works. Without OpenSpool,
        bind by UID alone (_spool_bind_by_tag: local table match or the
        'matches no table entry' line the web adopts from). No identity is
        invented when neither is available."""
        ace = self.ace
        if os and os.get('material'):
            col = os.get('color') or ''
            try:
                rgb = [int(col[0:2], 16), int(col[2:4], 16),
                       int(col[4:6], 16)] if len(col) == 6 else [0, 0, 0]
            except ValueError:
                rgb = [0, 0, 0]
            res = {'type': os['material'], 'color': rgb,
                   'brand': os.get('vendor', ''), 'sku': uid or '',
                   'subtype': ''}
            respond('rc522: OpenSpool tag: %s %s #%s (UID %s)'
                    % (os['material'], os.get('vendor') or '-',
                       col or '------', uid or '?'))
            ace._v2_store_filament_read(idx, slot, res, uid=uid,
                                        fmt='openspool')
            return
        if not uid:
            respond('rc522: a card is present but its UID could not be '
                    'read (raw RC522 read returned nothing) - the register '
                    'setup may need tuning on this firmware.')
            return
        bound = ace._spool_bind_by_tag(idx, slot, uid)
        if bound is not None:
            respond('rc522: bound to spool %s' % ace._spool_label(bound))
        else:
            respond('rc522: UID %s not in the table (a Spoolman spool with '
                    'this card_uid will adopt on the next sweep)' % uid)
