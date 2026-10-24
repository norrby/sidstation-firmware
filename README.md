# SidStation Firmware

A patch for the **Elektron SidStation**'s OS 1.10 R33 that adds three SysEx operations, used by
[SidStation Editor](https://github.com/norrby/sidstation-editor):

- **05** dump request: send the stored patch of a slot
- **06** settings request: MIDI channel, current slot, clock source, update rate and more
- **07** store: store the edit buffer to the current slot and report the result

The result is **OS 1.12 R01**; everything else is R33 byte for byte. Details in
[docs/protocol.md](docs/protocol.md); why not R34 in [docs/r34.md](docs/r34.md).

Not affiliated with or endorsed by Elektron. **Use at your own risk:** a failed upgrade can leave
the synth without an operating system.

## You need Elektron's R33 files

This repository contains no Elektron code, only the new code and the tools that build and check it.

| File | SHA-256 |
|---|---|
| `sidos_v33_1.syx` | `125c036a5f64d12fc3ec142b7baf06ca961b65455b69275fb6b7e473bc95710e` |
| `sidos_v33_2.syx` | `94b48b7605a4d0bc4339fe8dd916760158baa44df408a37ce3595bd3f6fba194` |

Elektron does not offer the OS for download; ask Elektron's support. Elektron's terms do not allow
redistribution, so please do not upload the original or the built files anywhere.

## Build

Python 3.9 or later, nothing else. Depending on the system the command is `python3`, `python` or `py`.

```
python3 build.py sidos_v33_1.syx sidos_v33_2.syx out
python3 check.py out/sidos_1.12_v01_2.syx out/sidos_1.12_v01_1.syx --orig sidos_v33_2.syx
```

`build.py` checks the inputs, applies the patch, runs the self-test and writes
`out/sidos_1.12_v01_1.syx` and `out/sidos_1.12_v01_2.syx` (SHA-256 `30a4522e39059f4fe20b7f269b2e0924c94f3ebcc60bdf25529781a460e2d746`;
the build always gives exactly this file). The self-test runs the new code in a 68HC11 emulator
together with the R33 routines it calls, on a patch memory of made-up patches. `check.py` checks a
file before it is sent. Nothing here sends MIDI.

Tests: `python3 -m unittest discover tests`. Set the environment variable `SIDOS_R33` to a folder with
the R33 files to include the build.

## Upgrade

You need a MIDI interface and a program that sends `.syx` files with a pause between messages;
**150 ms** has worked. Elektron's C6 SysEx Manager, on the
[SidStation download page](https://www.elektron.se/support-downloads/sidstation), can do this, but it
does not run on current macOS, and its pause is set in ticks; it has not been tried for this upgrade.

1. Back up your patches with an AllPatch dump.
2. Connect MIDI Out to the synth's MIDI In and close every other program that sends MIDI.
3. Hold **[D]** while switching the synth on: *Ready for FLASH upgrade*, one LED lit.
4. Send `sidos_1.12_v01_1.syx`. The synth erases the chip; two LEDs light.
5. Send `sidos_1.12_v01_2.syx` (about three minutes). The synth restarts with the new OS.

**Red LED, everything stops:** switch off, hold [D] while switching on, and repeat from step 4
(no greeting this time, one LED). If the transfer stopped at the very end, the flash routine may be
gone as well; then the flash chip has to be programmed outside the synth.

**Going back:** the same steps with Elektron's R33 files.

## License

Copyright (C) 2026 M Norrby. GNU General Public License version 3 or later; see [LICENSE](LICENSE).

SidStation and Elektron are trademarks of Elektron Music Machines.
