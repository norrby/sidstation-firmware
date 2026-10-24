# check.py – checks a SidStation OS file before it is sent to the synth. Sends nothing.
#
# Usage: python3 check.py <file_2.syx> [<file_1.syx>] [--orig <sidos_v33_2.syx>]
#
# The data file must be one of the known files below (SHA-256) and pass a check of every frame: header, 7-bit data,
# checksum, addresses inside $8000-$FFFF and none written twice, the end frame exactly once and last, the reset vector
# $8000. A start file, if given, must be exactly the start frame. With --orig (Elektron's R33 data file) it also checks
# that the flash routine, the path that lets a failed transfer be retried, is identical to R33's.
import sys, hashlib
from osdecode import HDR, START, END, frames, decode, image

R33_1 = '125c036a5f64d12fc3ec142b7baf06ca961b65455b69275fb6b7e473bc95710e'
R33_2 = '94b48b7605a4d0bc4339fe8dd916760158baa44df408a37ce3595bd3f6fba194'
V01_2 = '30a4522e39059f4fe20b7f269b2e0924c94f3ebcc60bdf25529781a460e2d746'
KNOWN = {
    R33_2: 'Elektron OS 1.10 R33 (sidos_v33_2.syx)',
    V01_2: 'OS 1.12 R01: R33 + SysEx operations 05/06/07 (sidos_1.12_v01_2.syx)',
}
FLASHER = (0xFD5D, 0xFEBD)    # the flash routine, copied to RAM and run from there during an upgrade

class CheckError(Exception): pass

def check_frames(d):
    """Every frame of a data file; returns the 64K image."""
    try: fs = frames(d)
    except ValueError as e: raise CheckError(str(e))
    if fs[-1] != END or fs.count(END) != 1: raise CheckError('the end frame must occur exactly once, last')
    seen = set()
    for n, f in enumerate(fs[:-1]):
        if not f.startswith(HDR): raise CheckError('frame %d has a wrong header' % n)
        p = f[6:-1]
        if len(p) % 2 or any(x >= 0x80 for x in p): raise CheckError('frame %d: odd length or byte >= 80' % n)
        v = [((p[k] << 1) + p[k + 1]) & 0xFF for k in range(0, len(p), 2)]
        if len(v) < 4 or (v[2] + sum(v[3:])) & 0xFF: raise CheckError('frame %d: checksum' % n)
        a = v[0] | v[1] << 8
        if a < 0x8000 or a + len(v) - 3 > 0x10000: raise CheckError('frame %d: address %04X outside $8000-$FFFF' % (n, a))
        for x in range(a, a + len(v) - 3):
            if x in seen: raise CheckError('address %04X is written twice' % x)
            seen.add(x)
    img, _ = image(decode(d)[0])
    if img[0xFFFE:0x10000] != b'\x80\x00': raise CheckError('the reset vector is not $8000')
    return img

def check(d, start=None, orig=None):
    """Raises CheckError unless d is a known, intact data file; returns a one-line summary."""
    h = hashlib.sha256(d).hexdigest()
    if h not in KNOWN: raise CheckError('unknown file (sha256 %s)' % h)
    img = check_frames(d)
    if start is not None and start != START: raise CheckError('the start file is not exactly F0 00 45 01 69 7E 41 F7')
    diff = ''
    if orig is not None:
        if hashlib.sha256(orig).hexdigest() != R33_2: raise CheckError('--orig is not sidos_v33_2.syx (R33)')
        o = check_frames(orig)
        if img[FLASHER[0]:FLASHER[1]] != o[FLASHER[0]:FLASHER[1]]: raise CheckError('the flash routine differs from R33')
        diff = ', flash routine as R33, %d bytes differ from R33' % sum(1 for x in range(0x8000, 0x10000) if img[x] != o[x])
    return 'ok: %s, %d frames, %d bytes%s' % (KNOWN[h], len(frames(d)), len(d), diff)

if __name__ == '__main__':
    args = sys.argv[1:]
    orig = None
    if '--orig' in args:
        k = args.index('--orig'); orig = open(args[k + 1], 'rb').read(); del args[k:k + 2]
    if len(args) not in (1, 2): raise SystemExit('usage: python3 check.py <file_2.syx> [<file_1.syx>] [--orig <sidos_v33_2.syx>]')
    try:
        print(check(open(args[0], 'rb').read(), open(args[1], 'rb').read() if len(args) > 1 else None, orig))
    except CheckError as e:
        raise SystemExit('ERROR: %s – do not send this file' % e)
