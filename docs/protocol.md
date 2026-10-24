# Protocol

## New SysEx operations (OS 1.12 R01)

All frames start with Elektron's SidStation header `F0 00 20 3C 01 00`, followed by an id.
The stock OS knows ids 01–04; R01 adds 05–07. Any other id is ignored, as before.

| Request | Frame | Reply |
|---|---|---|
| 05 dump request | `F0 00 20 3C 01 00 05 <slot> F7` | the front panel's Patch dump of that slot |
| 06 settings request | `F0 00 20 3C 01 00 06 F7` | `F0 00 20 3C 01 00 46 <15 values> F7` |
| 07 store | `F0 00 20 3C 01 00 07 7F F7` | `F0 00 20 3C 01 00 47 <8 values> F7` |

- **05:** `slot` is 0–99, or 7F for the current slot. The reply is what the front panel's
  *Patch dump* sends: an 02 frame with the **stored** patch, or an 03 frame if the slot is empty.
  It is not the edit buffer: changes that have not been stored are not in the reply.
- **07:** stores the edit buffer to the current slot, as the front panel's Store does, with one
  difference: it first checks that the new patch fits. The front panel frees the old patch first,
  so with a full memory the old patch is lost; 07 changes nothing then. Only `7F` (current slot) is
  accepted.
- A request is registered only when its F7 arrives; a malformed frame is dropped without a reply.
  Realtime bytes (F8–FF) inside a frame are handled as usual. Requests are served from the main
  loop, between two ticks of the sound engine, never inside the MIDI interrupt.
- Reply ids are 40 + the request id. The OS ignores 46 and 47 if they are ever echoed back to it.

Every value in a 46 or 47 reply is sent as two bytes, low nibble first, as the patch data in a dump.

### 46: settings

| # | Value |
|---|---|
| 1 | format version (1) |
| 2 | MIDI base channel (0–15) |
| 3 | current slot |
| 4 | loaded slot (FF after a patch was received over MIDI) |
| 5 | clock source: 0 Patch, 1 Global, 2 External clock |
| 6 | global Speed (Hz) |
| 7 | global HCut |
| 8 | SYSEX dump setting: 0 Patch, 1 AllPatch |
| 9 | the patch's Speed |
| 10 | the patch's HCut |
| 11, 12 | timer period, high and low byte: update rate = 250000 / period Hz |
| 13, 14 | external clock period, high and low byte |
| 15 | external clock lock counter, non-zero = locked to MIDI clock |

### 47: store result

| # | Value |
|---|---|
| 1 | status: 0 stored, 1 memory full, 2 invalid current slot, 3 busy (a SysEx frame was being received) |
| 2 | slot |
| 3, 4 | size of the stored patch in bytes, high and low byte |
| 5, 6 | 16-bit sum of the stored bytes, high and low byte |
| 7, 8 | bytes left in the patch memory afterwards, high and low byte |

With status 1–3 nothing was changed.

## OS file format

An OS upgrade is two files. `_1` is a single start frame; `_2` holds the data frames and the end frame.
This is read from the OS's own flash routine ($FD5D), which is copied to RAM ($7E00) and run there
during the upgrade.

```
F0 00 45 01 69 7E 41 F7        start: erase the chip (file _1)
F0 00 45 01 69 7F <pairs> F7   data
F0 00 45 01 69 F7              done: restart
```

- Each value is a byte pair `a b` with value = `(a << 1) + b`. Elektron's files write
  `a = (v >> 4) << 3`, `b = v & 15`, so every byte stays below 80.
- The values of a data frame: address low, address high, checksum, then the data bytes (32 in
  Elektron's files). Checksum + the sum of the data bytes = 0 (mod 256); the address is not
  included. With a wrong sum the flash routine stops with the red LED.
- The frames are programmed in file order. In R33 that is address order, so the flash routine itself
  ($FD5D–$FEBC) is written close to the end of the transfer.
