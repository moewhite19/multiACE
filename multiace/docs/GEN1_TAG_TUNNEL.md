# Gen-1 (ACE Pro) tag tunnel - `ACE_TAG_READ`, `ACE_SET_TAG_TUNNEL`

The ACE Pro's own reader understands **Anycubic tags only**: a third-party
spool (OpenSpool NDEF, a blank NTAG, a Bambu/Snapmaker MIFARE chip) arrives
with no SKU - or an SKU no table entry carries. The **community firmware**
(`CV1.3.87x`) adds an RC522 tunnel on the existing `filament_recognition`
command, so multiACE can drive the reader itself and read those tags
directly: the card UID always, and the OpenSpool material/colour when the
tag carries an NDEF record.

Companion documents:

* the tunnel contract (packed index, ops 0..8, antenna map, gotchas):
  `REPORT-RC522-TUNNEL-EN.md` in
  <https://github.com/Godless50/ACE-PRO-v1.-NFC-UID>
* the host implementation: `multiace/klipper/extras/ace_gen1_tunnel.py`
* the self-check: `tests/gen1_tag_tunnel_selfcheck.py`

## What it does

* **Detects the tunnel safely.** Two gates, both must pass: the runtime
  firmware string is a known tunnel build (`CV1.3.87x`), and a probe op
  actually answers through the stub (a `result.code` exists - a stock reply
  never has one). A stock unit (`V1.3.863`) and the earlier UID-only
  community build (`CV1.3.863`) see **no tunnel traffic at all**. If the
  firmware matches but does not answer, exactly one probe is sent, logged
  once, and the feature is dropped for that firmware.
* **Reads a tag.** `acquire` (op 7, holds the reader) → `SELECT` on the
  slot's reader channel (op 6, `slot_channel(slot)`) → NTAG `READ(0x30)` of
  the page (the exact host order: `TXMODE |= 0x80`, `RXMODE |= 0x80`,
  `BitFraming = 0`, FIFO writes, `TRANSCEIVE`, RX bits, FIFO reads) →
  `release` (op 8, mandatory). Page 0 yields the 7-byte UID with both
  ISO14443-3 BCC check bytes verified (the shared `ace_rc522` check); an
  NTAG capability container additionally triggers a bounded read of the
  OpenSpool user area (pages 4..39), decoded by the shared `ace_rc522`
  OpenSpool decoder.
* **Falls back automatically** (when enabled): a slot that is occupied and
  whose tag the firmware did not identify - or whose SKU matches no table
  entry - gets **one opportunistic read per insert**. The result lands in
  multiACE's own per-unit store, never in `_info_per_ace`. Binding is
  **gated on attribution**: the two slots of an antenna pair share one RF
  path, so the card UID is offered to the **existing** tag-bind path
  (`_spool_bind_by_tag(..., unbind=False)`) **only when the partner slot
  (`slot ^ 1`) reads empty in the same status**. An occupied OR
  unknown/absent partner still stores and surfaces the read but does not
  bind (a wrong first binding has no repair path on a Gen 1); the operator
  probe `ACE_TAG_READ` is the explicit override and always binds.
  `unbind=False` on purpose: a tunnel read must never release a binding
  the vendor path owns.
* **Surfaces it.** `get_status`: the slot's `uid` / `tag_format` are filled
  from the tunnel read **only when the device delivered none** (a device
  value always wins), and each ACE carries an additive `tag_tunnel` block:

  ```json
  "tag_tunnel": {
    "enabled": true,
    "available": true,
    "reads": {
      "0": {"uid": "04225251C82A81", "format": "openspool",
            "material": "PETG", "color": "DE3530", "brand": "Creality",
            "bound": true, "age": 12.3}
    }
  }
  ```

## The command (operator probe)

```
ACE_TAG_READ ACE=<n> SLOT=<0..3> [PAGE=<n>]
```

* `PAGE` defaults to `0` (the UID page). Any page can be dumped.
* On a Gen 1 it prints the raw 16 bytes and the UID:

  ```
  [multiACE] ACE 0 slot 2 page 0: 04 22 52 FC 51 C8 2A 81 32 48 00 00 E1 10 6D 00
  [multiACE] ACE 0 slot 2: UID 04225251C82A81 (ntag) - third-party tag
  ```
* It **moves nothing** (no lane rotation) and is safe while printing; the
  tag must face the coil, so a read can fail on a stationary spool.
* It works with the enable flag **off** - the command itself is the
  explicit consent. The result feeds the same store/bind/status path as an
  automatic read.
* On an ACE 2 the command keeps its previous meaning (rotate + read +
  bind); the Gen-1 branch is only taken for a non-V2 protocol.

## The enable flag

Automatic fallback traffic needs an explicit opt-in:

```
ACE_SET_TAG_TUNNEL ENABLE=0|1 [PERSIST=0|1]
```

* Default **off** (`#gen1_tag_tunnel: false` in `[ace]`); the setter is
  live + write-through, `PERSIST=0` = RAM only until restart.
* With it off, the **only** tunnel traffic is a command the user typed.
* On a unit without the tunnel the flag is inert (one `klippy.log` line,
  no console noise, no repeated probes).

## Limits

* **Community firmware required.** The tunnel exists only on the
  community build `CV1.3.87x` (verified reference `CV1.3.871`). Stock and
  the UID-only community image are never touched.
* **The tag must face the coil.** A Gen-1 has no host-side motor control
  (`feed_filament`/`unwind_filament` are refused by the stock firmware), so
  an automatic read cannot rotate a spool to find the tag. The one attempt
  per insert is timed to the firmware's own insert procedure, which already
  rotates the spool; if the read misses, use `ACE_TAG_READ` to retry.
* **Two antennas, two bays each; reader channel = bit-swap.** Antenna 1
  covers slots 0 and 1 (reader channels 0 and 2), antenna 2 covers slots 2
  and 3 (channels 1 and 3). The reader **channel** for a slot is
  `0,1,2,3 -> 0,2,1,3` (`((slot & 1) << 1) | ((slot >> 1) & 1)`,
  `slot_channel()`); the **partner slot** sharing the antenna is
  `slot ^ 1`, whose channel is `channel ^ 2`. An earlier live run saw
  readers 0/2 and 1/3 answer with the same UID - that is the shared RF
  path, and the reason the automatic bind is gated on the partner slot
  reading empty (see above). Two slots reading the same UID cannot both
  bind: the existing duplicate guard refuses the second one, and an
  unattributed read is stored but never bound.
* **Read cost.** One page read is ~25 tunnel commands (~0.5-1 s on the
  wire); the OpenSpool user-area read adds 9 more. A per-command timeout
  bounds every op; a read session is always closed with `release`, and a
  failed release is logged once (the unit may need a power cycle - it
  would otherwise stay paused and report `status=busy`).
* **UID form.** The UID is the ISO14443-3 one (`page0[0:3] +
  page1[0:3]`, BCC-verified): `04225251C82A81`. The tunnel report's UID
  column prints raw page bytes 0..6 (BCC0 instead of UID6) - matching the
  project's existing `card_uids`/phone-confirmed form is deliberate.
* **Gen 1 only.** V2 behaviour is untouched; the automatic tick is a
  hard no-op for a V2 unit.

## Tests

The repository has no CI beyond the release tarball; this is the
test-in-a-script, under `tests/` alongside the Gen-1 flasher self-check:

```sh
python3 tests/gen1_tag_tunnel_selfcheck.py
```

It imports the real `ace.py`, `ace_gen1_tunnel.py` and `ace_rc522.py` and
drives them on hand-built fakes (no Klipper, no hardware): the
packing/signed conversion, the slot -> reader-channel bit-swap map
(`0,1,2,3 -> 0,2,1,3`), the exact op sequence, the reply parsing
(bit-masked `result.code`), graceful degradation (stock firmware sends
nothing; a matching-but-silent firmware gets one probe; a dead link times
out bounded), both genuine live captures (`04 22 52 FC ...` and
`53 42 70 E9 ...`) yielding their bytes and UIDs, an OpenSpool NDEF decode
through the reused `ace_rc522` decoder, and the ace.py wiring (flag
gating, one attempt per occupancy, the shared-antenna bind gate - partner
occupied/unknown/absent -> stored but not bound - own store, no
`_info_per_ace` writes, get_status surfacing, unchanged V2 path).

## Open points for the maintainer

1. **Flag name / default.** Implemented as `gen1_tag_tunnel`, default
   `false`, setter `ACE_SET_TAG_TUNNEL`. Happy to rename or default it on.
2. **Surface third-party spools automatically?** The automatic read binds
   by card UID only when a table entry already carries that UID **and the
   partner slot on the shared antenna reads empty**; it never creates
   entries and never releases a vendor binding. If the maintainer prefers
   report-only (no bind call at all), the `bind=False` path is already the
   non-binding one.
3. **Web UI.** The new `tag_tunnel` status block and the slot `uid` /
   `tag_format` fill are already in `get_status`; the web backend passes
   the slot `uid`/`tag_format` through today. Whether to add a Config tab
   toggle and a "third-party tag" badge is a UI decision.
4. **Retry policy.** One attempt per insert is deliberate and cheap; a
   bounded retry ladder (or a "read on next rotation" hook) would raise the
   hit rate on spools whose tag parks away from the coil.
5. **Firmware acceptance.** The version pre-gate accepts `CV1.3.87x`.
   A later tunnel build (e.g. `CV1.3.872`) would pass the gate; the probe
   then decides. If a future build changes the op contract, the constant
   needs revisiting (the reference is `CV1.3.871`).
