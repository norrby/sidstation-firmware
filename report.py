# report.py – merges the runs that explore.py saved and prints what they found: code coverage (and the branches
# never taken), the RAM map (who writes and reads each byte), RAM nobody touched, the deepest stack and its call
# chain, nested and lost interrupts, and every odd event.
#
#   python3 report.py <os.syx> <run.pickle> [<run.pickle> ...]
import pickle, sys
from collections import defaultdict
from osdecode import image, decode
from machine import OPT, E_HZ

def ranges(addrs, gap=1):
    out = []
    for a in sorted(addrs):
        if out and a - out[-1][1] <= gap: out[-1][1] = a
        else: out.append([a, a])
    return out

def insn(img, pc):
    """(length, branch targets, falls through) of the instruction at pc, or None for an unknown opcode."""
    op = img[pc]; pre = None; p = pc + 1
    if op in (0x18, 0x1A, 0xCD): pre = op; op = img[p]; p += 1
    info = OPT.get((pre, op))
    if not info or info[0] == 'TEST': return None
    mn, mode, _ = info
    n = {'inh': 0, 'imm8': 1, 'imm16': 2, 'dir': 1, 'ext': 2, 'ix': 1, 'iy': 1, 'rel': 1, 'sdir': 2, 'six': 2,
         'siy': 2, 'bdir': 3, 'bix': 3, 'biy': 3}[mode]
    end = p + n; tg = []
    if mode == 'rel':
        off = img[p]; tg.append((end + (off - 256 if off > 127 else off)) & 0xffff)
    if mode in ('bdir', 'bix', 'biy'):
        off = img[p + 2]; tg.append((end + (off - 256 if off > 127 else off)) & 0xffff)
    if mn in ('JMP', 'JSR') and mode == 'ext': tg.append(img[p] << 8 | img[p + 1])
    falls = mn not in ('BRA', 'JMP', 'RTS', 'RTI')
    return end - pc, tg, falls

def untaken(img, ran):
    """Code that no run executed but that a branch or jump from executed code leads to: the paths never taken."""
    seen, out = set(), []
    starts = []
    for pc in range(0x8000, 0x10000):
        if not ran[pc]: continue
        d = insn(img, pc)
        if not d: continue
        ln, tg, falls = d
        for t in tg + ([pc + ln] if falls else []):
            if t >= 0x8000 and not ran[t]: starts.append((t, pc))
    for t, src in starts:
        if t in seen: continue
        pc, n = t, 0
        while pc < 0x10000 and not ran[pc] and pc not in seen and n < 200:
            seen.add(pc); d = insn(img, pc)
            if not d: break
            ln, tg, falls = d; n += 1
            if not falls: pc += ln; break
            pc += ln
        out.append((t, pc, src, n))
    return sorted(out)

def main():
    img, _ = image(decode(open(sys.argv[1], 'rb').read())[0])
    runs = [r for p in sys.argv[2:] for r in pickle.load(open(p, 'rb'))]
    ran = bytearray(0x10000); writers = defaultdict(set); readers = defaultdict(set)
    wild = defaultdict(set)                    # dp_wild writes anywhere on purpose: kept out of the RAM map
    for r in runs:
        if r['name'] == 'dp_wild':
            for a, s in r['writers'].items(): wild[a] |= set(s)
            for a, x in enumerate(r['ran']):
                if x: ran[a] = 1
            continue
        for a, x in enumerate(r['ran']):
            if x: ran[a] = 1
        if r['err']: continue                  # a crashed run wrote garbage anywhere: kept out of the RAM map
        for a, s in r['writers'].items(): writers[a] |= set(s)
        for a, s in r['readers'].items(): readers[a] |= set(s)
    print('== runs')
    for r in runs:
        print('  %-10s seed %d  %7.1f s synth  min SP %04X  err %s' % (r['name'], r['seed'], r['synth_s'], r['min_sp'], r['err']))
    print('  total synth time %.0f s' % sum(r['synth_s'] for r in runs))

    print('\n== code: %d instruction addresses executed' % sum(ran[0x8000:]))
    un = untaken(img, ran)
    print('  %d paths never taken (start, end, from, instructions); the longest:' % len(un))
    for t, e, src, n in sorted(un, key=lambda x: -x[3])[:40]:
        print('    %04X-%04X  from %04X  %3d insns' % (t, e, src, n))

    print('\n== stack')
    worst = min(runs, key=lambda r: r['min_sp'])
    print('  lowest SP %04X (run %s seed %d): %d bytes below $1FFF' % (worst['min_sp'], worst['name'], worst['seed'],
                                                                       0x1FFF - worst['min_sp']))
    print('  chain at that moment: ' + ' > '.join('%s %s' % (k, t if isinstance(t, str) else '%04X' % t)
                                                  for k, t in worst['deepest']))
    lo = min(a for a in writers if 0x1E00 <= a < 0x2000 and any(0x1E00 <= a for a in [a]))
    print('  lowest RAM byte written in $1E00-$1FFF: %04X' % lo)

    print('\n== interrupts')
    nest = defaultdict(int); longest = defaultdict(int); missed = defaultdict(int); pairs = set()
    for r in runs:
        for k, v in r['isr_max_nest'].items(): nest[k] = max(nest[k], v)
        for k, v in r['isr_longest'].items(): longest[k] = max(longest[k], v)
        for k, v in r['missed'].items(): missed[k] += v
        pairs |= {tuple(p) for p in r['isr_pairs']}
    for k in sorted(nest):
        print('  %-4s nested at most %d deep, longest %.2f ms, lost %d' % (k, nest[k], longest[k] / E_HZ * 1000, missed[k]))
    print('  nestings seen (outer, inner): ' + ', '.join('%s>%s' % p for p in sorted(pairs)))
    print('  SCI overruns: %d' % sum(r['overruns'] for r in runs))
    lost = defaultdict(int)
    for r in runs:
        for k, v in r.get('lost', {}).items(): lost[k] += v
    print('  lost bytes by value: ' + ', '.join('%02X x%d' % kv for kv in sorted(lost.items(), key=lambda x: -x[1])[:20]))
    for r in runs:
        if r.get('lost'): print('    %-10s %s' % (r['name'], ', '.join('%02X x%d' % kv for kv in sorted(r['lost'].items(), key=lambda x: -x[1])[:8])))

    print('\n== watched code (times executed)')
    w = defaultdict(int)
    for r in runs:
        for k, v in r['watch'].items(): w[k] += v
    for k, v in w.items(): print('  %-30s %d' % (k, v))

    print('\n== events')
    ev = defaultdict(int); first = {}
    for r in runs:
        for k, v in r['event_kinds'].items(): ev[k] += v
        for e in r['events']: first.setdefault(e[2], (r['name'], e))
    for k, v in sorted(ev.items()):
        name, (cyc, pc, kind, detail) = first[k]
        print('  %-28s %7d   first: run %s, pc %04X, %s' % (k, v, name, pc, detail))

    print('\n== RAM $0000-$1FFF and $7800-$7FFF: used ranges (writers)')
    used = {a for a in set(writers) | set(readers) if a < 0x2000 or a >= 0x7800}
    for lo, hi in ranges(used):
        ws = sorted(set().union(*(writers.get(a, set()) for a in range(lo, hi + 1))))
        rs = sorted(set().union(*(readers.get(a, set()) for a in range(lo, hi + 1))))
        print('  %04X-%04X %4d bytes  %d writers %s  %d readers' % (lo, hi, hi - lo + 1, len(ws),
              ' '.join('%04X' % p for p in ws[:6]) + (' …' if len(ws) > 6 else ''), len(rs)))
    print('\n== RAM nobody wrote or read (at least 8 bytes), $0000-$1FFF and $7800-$7FFF')
    free = [a for a in list(range(0x0000, 0x2000)) + list(range(0x7800, 0x8000))
            if a not in writers and a not in readers and not 0x1000 <= a < 0x1100]
    for lo, hi in ranges(free):
        if hi - lo + 1 >= 8: print('  %04X-%04X %4d bytes' % (lo, hi, hi - lo + 1))
    normal = set(writers)
    reach = [a for a in wild if a not in normal]
    print('\n== wild DP frames (dp_wild): bytes written that no other run writes, by range')
    for lo, hi in ranges(reach, gap=16):
        print('  %04X-%04X  writers %s' % (lo, hi, ' '.join('%04X' % p for p in sorted(set().union(*(wild.get(a, set()) for a in range(lo, hi + 1))))[:6])))
    heap = [a for a in writers if 0x2000 <= a < 0x7800]
    print('\n== patch heap $2000-$77FF: highest byte written %04X' % max(heap) if heap else '\n== heap untouched')

    scr = set()
    for r in runs: scr |= {tuple(s) for s in r['screens']}
    print('\n== %d distinct screens; first lines seen:' % len(scr))
    print('  ' + ' | '.join(sorted({s[0].strip() for s in scr if s[0].strip()})[:150]))

if __name__ == '__main__':
    main()
