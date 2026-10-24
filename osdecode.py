# osdecode.py – reads and writes SidStation OS files (sidos_v33_2.syx and the like).
#
# Format, as read from the OS's own flash routine ($FD5D, copied to RAM $7E00 and run there):
#   F0 00 45 01 69 <type> <pairs...> F7   type 7F = data, 7E = start (the _1 file), F7 right after 69 = done (reset)
#   each value is a byte pair a b: value = (a << 1) + b (8 bits). Elektron's files write a = (v >> 4) << 3, b = v & 15.
#   values: address low, address high, checksum, data bytes. Checksum + sum of the data bytes = 0 (mod 256); the
#   address is not included. A wrong sum makes the flash routine stop with the red LED ($FDCA).
#
# Usage: python3 osdecode.py <sidos.syx> [<out.bin>] [--verify]
#   prints a summary, optionally writes the 64K image; --verify re-encodes and compares with the file.
# Read-only: never sends anything anywhere.
import sys

HDR = bytes.fromhex('f0004501697f')
START = bytes.fromhex('f0004501697e41f7')
END = bytes.fromhex('f000450169f7')

def frames(d):
    """The F0..F7 frames of a file; raises ValueError on anything between or after them."""
    out, i = [], 0
    while i < len(d):
        if d[i] != 0xF0: raise ValueError('byte %d is not F0' % i)
        j = d.find(b'\xf7', i)
        if j < 0: raise ValueError('frame without F7 at byte %d' % i)
        out.append(d[i:j + 1]); i = j + 1
    return out

def decode(d):
    """Records in file order: (address, data bytes) for data frames, the frame itself for anything else.
    Also returns the number of data frames with a wrong checksum."""
    recs, bad = [], 0
    for f in frames(d):
        if not f.startswith(HDR): recs.append(f); continue
        p = f[6:-1]
        v = [((p[k] << 1) + p[k + 1]) & 0xff for k in range(0, len(p), 2)]
        if (v[2] + sum(v[3:])) & 0xff: bad += 1
        recs.append((v[0] | v[1] << 8, v[3:]))
    return recs, bad

def encode(recs):
    out = bytearray()
    for r in recs:
        if isinstance(r, bytes): out += r; continue
        addr, data = r
        vals = [addr & 0xff, addr >> 8, (-sum(data)) & 0xff] + list(data)
        out += HDR + bytes(x for v in vals for x in ((v >> 4) << 3, v & 15)) + b'\xf7'
    return bytes(out)

def image(recs):
    """64K image (FF where the file writes nothing, as an erased chip) and a coverage count per address."""
    img, cov = bytearray(b'\xff' * 0x10000), bytearray(0x10000)
    for r in recs:
        if isinstance(r, bytes): continue
        a, bs = r
        for j, x in enumerate(bs): img[(a + j) & 0xffff] = x; cov[(a + j) & 0xffff] += 1
    return img, cov

if __name__ == '__main__':
    if len(sys.argv) < 2: raise SystemExit('usage: python3 osdecode.py <sidos.syx> [<out.bin>] [--verify]')
    d = open(sys.argv[1], 'rb').read()
    recs, bad = decode(d)
    data = [r for r in recs if not isinstance(r, bytes)]
    img, cov = image(recs)
    lo = min(a for a, _ in data); hi = max(a + len(b) - 1 for a, b in data)
    print('data frames %d, checksum errors %d, %04X-%04X, %d bytes, reset -> %02X%02X'
          % (len(data), bad, lo, hi, sum(len(b) for _, b in data), img[0xfffe], img[0xffff]))
    if '--verify' in sys.argv: print('re-encoded identical to the file:', encode(recs) == d)
    out = [a for a in sys.argv[2:] if not a.startswith('--')]
    if out: open(out[0], 'wb').write(img)
