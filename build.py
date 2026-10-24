# build.py – builds SidStation OS 1.12 R01 from Elektron's OS 1.10 R33 files. Writes files only; sends nothing.
#
# Usage: python3 build.py <sidos_v33_1.syx> <sidos_v33_2.syx> <out dir> [--list]
#
#   1. checks that the inputs are exactly R33 (SHA-256)
#   2. applies the patch (sysops.py) and checks that the result differs from R33 exactly where intended
#   3. runs the self-test (selftest.py) and cross-checks the new code with the disassembler (hc11.py)
#   4. checks the result as check.py does; it must be byte for byte the released R01 file
#   5. writes <out dir>/sidos_1.12_v01_1.syx (the start frame, as R33's) and sidos_1.12_v01_2.syx
# --list prints the labels and a listing of the new code.
import sys, os, hashlib
import sysops, selftest, check
from osdecode import decode, image

def main(args):
    if not __debug__: raise SystemExit('ERROR: run without -O (the checks are assertions)')
    lst = '--list' in args
    args = [a for a in args if a != '--list']
    if len(args) != 3: raise SystemExit('usage: python3 build.py <sidos_v33_1.syx> <sidos_v33_2.syx> <out dir> [--list]')
    start, orig = (open(p, 'rb').read() for p in args[:2])
    for d, want, name in ((start, check.R33_1, 'sidos_v33_1.syx'), (orig, check.R33_2, 'sidos_v33_2.syx')):
        if hashlib.sha256(d).hexdigest() != want: raise SystemExit('ERROR: %s is not Elektron\'s R33 file (see README)' % name)

    new = sysops.build(orig)
    diff = sysops.check(orig, new)
    print('patch applied: %d bytes differ from R33, new code %04X-%04X (%d bytes free below %04X)'
          % (len(diff), sysops.ORG, sysops.ORG + len(sysops.CODE) - 1, sysops.LIMIT - sysops.ORG - len(sysops.CODE), sysops.LIMIT))
    img, _ = image(decode(new)[0])
    print(selftest.selftest(img))
    bad = sysops.crosscheck()
    if bad: raise SystemExit('ERROR: hc11.py disagrees with the assembler: %s' % bad[:10])
    print('cross-check with hc11.py ok')
    if hashlib.sha256(new).hexdigest() != check.V01_2:
        raise SystemExit('ERROR: the result is not the released R01 file (sha256 %s); a changed patch needs a new version '
                         'and its sha256 in check.py' % hashlib.sha256(new).hexdigest())
    print(check.check(new, start, orig))

    out = args[2]
    os.makedirs(out, exist_ok=True)
    for name, d in (('sidos_1.12_v01_1.syx', start), ('sidos_1.12_v01_2.syx', new)):
        p = os.path.join(out, name)
        with open(p, 'wb') as f: f.write(d)
        print('wrote %s (%d bytes)' % (p, len(d)))
    if lst:
        for n in sorted(sysops.L, key=sysops.L.get): print('%-9s %04X' % (n, sysops.L[n]))
        print('\n'.join(sysops.listing()))

if __name__ == '__main__':
    main(sys.argv[1:])
