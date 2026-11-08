# Protocol

## New SysEx operations (OS 1.12 R01 and later)

All frames start with Elektron's SidStation header `F0 00 20 3C 01 00`, followed by an id.
The stock OS knows ids 01–04; R01 adds 05–07 and R02 adds 08. Any other id is ignored, as before.

| Request | Frame | Reply |
|---|---|---|
| 05 dump request | `F0 00 20 3C 01 00 05 <slot> F7` | the front panel's Patch dump of that slot |
| 06 settings request | `F0 00 20 3C 01 00 06 F7` | `F0 00 20 3C 01 00 46 <15 values> F7` |
| 07 store | `F0 00 20 3C 01 00 07 7F F7` | `F0 00 20 3C 01 00 47 <8 values> F7` |
| 08 version request (R02) | `F0 00 20 3C 01 00 08 F7` | `F0 00 20 3C 01 00 48 <count> <count values> F7` |

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
- Reply ids are 40 + the request id. The OS ignores 46, 47 and 48 if they are ever echoed back to it.

Every value in a 46, 47 or 48 reply is sent as two bytes, low nibble first, as the patch data in a dump.

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

### 48: version

| # | Value |
|---|---|
| 1 | count: the number of values that follow (3 in R02 and R03) |
| 2 | major version (1) |
| 3 | minor version (12) |
| 4 | revision (2 for R02, 3 for R03) |

A later version may add values after these and raise the count. A reader takes the count first, reads the values
it knows and skips the rest, so an older reader still gets the version. OS 1.12 R01 does not answer 08; the stock
OS answers neither 06 nor 08. Example, R03: `F0 00 20 3C 01 00 48 03 00 01 00 0C 00 03 00 F7`.

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

## Fixes of R33 bugs (OS 1.12 R02)

R02 is R01 with these fixes and the version request 08; the operations 05–07 are unchanged.

1. **Patch frames during a store.** R33 receives a patch (02) inside the MIDI interrupt, and allocates its memory
   there. Store and Delete on the front panel move the patches in memory with interrupts enabled, and both use the
   same scratch bytes as the allocation. A patch frame arriving in the middle of a store left a patch pointing
   outside the patch memory, and the next store overwrote RAM. Now, while a store or delete is working in the patch
   memory (and during 07), an arriving 02 frame is dropped, as R33 drops one when the memory is full, and a clear
   (01) is ignored. Store and Delete first wait, at most about a second, for an 02 frame that is already being
   received.
2. **Patch frames longer than their size.** R33 wrote every data byte of an 02 frame, also past the size the frame
   gave, into the next patch or past the end of the patch memory. Bytes past the end are now dropped, and a size of
   4000 hex or more is refused.
3. **Status bytes inside a DP frame (04).** R33 took a byte 80–F6 inside a DP frame as data, so the address could
   point anywhere in RAM, including the stored patches. Such a byte now ends the frame and is handled as a new
   status byte, as MIDI requires.
4. **Status bytes inside a channel message.** R33 restarted the synth when a status byte arrived before a channel
   message was complete (a cable plugged in while playing, two merged streams). The incomplete message is now
   dropped and the byte handled as a new status byte.
5. **Direct control targets above 82.** CC 16–19 with a direct control target of 83 or more made R33 read past
   its table and jump to a random address. Such a target now does nothing.

## Three channels (OS 1.12 R03)

R03 is R02 with one addition: oscillators 2 and 3 can play notes on MIDI channels of their own, so one patch plays up
to three parts. Oscillator 1 stays on the synth's base channel (System > MIDI).

The channels are kept in the patch, in byte 23 (PATCH_MODE2, address 17 hex in the edit buffer, unused before R03):

| Bits | Value |
|---|---|
| 0–3 | oscillator 2's channel, as steps above the base channel (mod 16) |
| 4–7 | oscillator 3's channel, the same way |

A step of 0 means the base channel. Every older patch has 0 in this byte, and then the synth behaves exactly as R02.
The byte is set like any other patch byte: in a patch frame (02), or with a DP frame (04) at address 17. Because
the steps count from the base channel, a stored patch keeps its parts apart on a synth with another base channel.

With a channel of its own for oscillator 2 or 3:

1. Only note on and note off follow the oscillators' channels. Every other message (CC, pitch bend, program change,
   SysEx) is read on the base channel only, as before.
2. Each oscillator plays the notes of its channel one at a time, as in Single mode, with its own sound, delay and
   Active setting. Two oscillators on the same channel play the same notes.
3. Poly counts as off.
4. The filter envelope releases when no oscillator holds a note.
5. On CC 123 (all notes off), and when the channels have changed since the last note (a program change, a patch
   frame, a DP frame at 17), every held note is forgotten and all three oscillators are released; after a change this
   happens when the next note arrives, which then plays on the new channels.
