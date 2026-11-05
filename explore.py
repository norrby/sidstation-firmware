# explore.py – runs the OS in machine.py through as many paths as we can think of and reports what it used: the code
# that ran, every RAM byte written or read and by which code, the deepest stack and how it got there, nested and lost
# interrupts, SCI overruns, and anything odd (flash/EEPROM writes, unknown I/O, unknown opcodes). Offline only.
#
#   python3 explore.py <os.syx> <allpatch.syx> [--minutes N] [--seeds N] [--out report.json]
#
# The machine is booted once and the bank is loaded through MIDI in (as a librarian would send it); each scenario then
# starts from that state in its own process.
import json, multiprocessing as mp, pickle, random, sys, time
from osdecode import image, decode
from machine import Machine, Stop, E_HZ

MS = E_HZ // 1000
KEYMAP = '123A456B789C*0#D'                  # $A143: key code -> panel key (A = EXIT, B = UP, C = DOWN, D = ENTER)
WATCH = {0x8000: 'reset', 0x8AB8: 'engine tick skipped (busy)', 0x8232: 'MIDI error reboot',
         0xFA7D: 'ASID entered', 0xFC6F: 'boot key 3 path', 0xFD45: 'boot key D path', 0x8095: 'memory init'}


def frames(d):
    out, i = [], 0
    while (a := d.find(b'\xf0', i)) >= 0:
        b = d.find(b'\xf7', a); out.append(d[a:b + 1]); i = b + 1
    return out


class Run:
    def __init__(self, m, rnd):
        self.m, self.r = m, rnd
        m.watch = dict.fromkeys(WATCH, 0) | {k: v for k, v in m.watch.items()}

    def wait(self, ms): self.m.run(int(ms * MS))

    def drain(self, extra_ms=5):
        m = self.m
        while m.rx_pending(): m.run(20 * MS)
        self.wait(extra_ms)

    def send(self, data, gap_ms=0, drain=True):
        self.m.midi(bytes(data), int(gap_ms * MS))
        if drain: self.drain()

    def key(self, k, hold=60, gap=60):
        self.m.keys = 0x10 | KEYMAP.index(k); self.wait(hold); self.m.keys = 0; self.wait(gap)

    def keys(self, s, **kw):
        for k in s: self.key(k, **kw)

    def note(self, n, vel=100, ch=0, length=50):
        self.send([0x90 | ch, n, vel], drain=False); self.wait(length)
        self.send([0x80 | ch, n, 0], drain=False); self.wait(5)


# --- scenarios ----------------------------------------------------------------------------------------------------
def sc_notes(R):
    """Notes in every way: single notes, chords beyond three, legato overlaps, retrigger, velocity 0 as note off,
    running status, notes on other channels, very short and very long notes, on many patches (Single and Poly)."""
    r = R.r
    for slot in r.sample(range(100), 30):
        R.send([0xC0, slot]); R.wait(30)
        for _ in range(8): R.note(r.randrange(128), r.randrange(1, 128), length=r.choice([1, 10, 80, 300]))
        chord = r.sample(range(30, 90), r.randrange(2, 8))
        R.send([0x90] + [x for n in chord for x in (n, 100)]); R.wait(200)          # running status
        R.send([0x90] + [x for n in chord for x in (n, 0)]); R.wait(50)            # velocity 0 = off
        for n in range(40, 52): R.send([0x90, n, 90], drain=False); R.wait(15)       # legato run
        for n in range(40, 52): R.send([0x80, n, 0], drain=False)
        R.drain(30)
        for _ in range(20): R.send([0x90, 60, 100, 0x90, 60, 100], drain=False)      # retrigger the same key
        R.send([0x80, 60, 0]); R.send([0xB0, 123, 0]); R.wait(20)
        R.send([0x91, 60, 100, 0x9F, 61, 100, 0x81, 60, 0]); R.wait(20)               # other channels


def sc_cc(R):
    """Every controller 0-127 at the edge values and at random, on several patches, with notes sounding."""
    r = R.r
    for slot in r.sample(range(100), 6):
        R.send([0xC0, slot]); R.send([0x90, 48, 100, 52, 100, 55, 100])
        for cc in range(128):
            for v in (0, 1, 63, 64, 126, 127, r.randrange(128)): R.send([0xB0, cc, v], drain=False)
            R.drain(2)
        R.send([0xB0, 123, 0]); R.wait(30)


def sc_sweeps(R):
    """Fast sweeps of the direct controllers and of pitch bend, aftertouch and mod wheel while notes play, as Live's
    automation would send them, at the full cable rate."""
    r = R.r
    for slot in r.sample(range(100), 6):
        R.send([0xC0, slot]); R.send([0x90, 40, 100, 47, 90, 52, 80])
        ccs = r.sample(list(range(16, 20)) + list(range(24, 64)) + list(range(70, 96)) + list(range(102, 118)), 20)
        for step in range(0, 128, 2):
            data = []
            for cc in ccs: data += [0xB0, cc, step]
            data += [0xE0, step, 127 - step, 0xD0, step, 0xB0, 1, step]
            R.send(data, drain=False); R.wait(10)
        R.drain(20); R.send([0xB0, 123, 0])


def sc_program(R):
    """Program changes: every slot, out of range (100-127), quickly after each other, during notes and SysEx."""
    r = R.r
    for slot in list(range(128)) + [r.randrange(128) for _ in range(100)]:
        R.send([0xC0, slot], drain=False); R.wait(r.choice([0, 1, 5, 40]))
        if r.random() < 0.3: R.note(r.randrange(30, 90), length=20)
    R.drain()
    for _ in range(50):                                   # a program change in the middle of a dense stream
        R.send([0x90, 60, 100, 0xB0, 20, 64, 0xC0, r.randrange(100), 0xE0, 0, 64, 0x80, 60, 0], drain=False)
    R.drain(50)


def sc_realtime(R):
    """MIDI clock and start/continue: 24 ppq at many tempos, clock between the bytes of other messages, start/stop,
    with the synth set to external clock (system block +3 = 2) and to its own clock."""
    r = R.r; m = R.m
    for source in (0, 2, 1):
        m.m[0x1AC8 + 3] = source
        for bpm in (30, 60, 120, 180, 300, 999):
            gap = max(0, int(60_000 / bpm / 24) - 1)
            R.send([0xFA])
            for k in range(96):
                R.send([0xF8], drain=False)
                if k % 12 == 0: R.send([0x90, 0xF8, 60, 0xF8, 100], drain=False)    # clock inside a message
                R.wait(gap)
            R.send([0xFC, 0xFB]); R.send([0x80, 60, 0]); R.drain(10)
        R.send([0xF8] * 2000, drain=True)                 # a clock flood at the full cable rate
    m.m[0x1AC8 + 3] = 0


HDR = [0xF0, 0x00, 0x20, 0x3C, 0x01, 0x00]
DP_MASKS = (0x01, 0x03, 0x07, 0x0F, 0x7F)


def dp(addr, mask, shift, value):
    """A Direct Program frame (id 04): F0 00 20 3C 01 00 04 Ph Pl mask shift value F7."""
    return HDR + [0x04, addr >> 7 & 0x7F, addr & 0x7F, mask, shift, value, 0xF7]


DC_TARGETS = (0x0A, 0x0D, 0x10, 0x13)        # the four direct controllers' target bytes; $CCC0 knows targets 0-82 only


def dp_valid(r, addr):
    """A well-formed DP frame for addr. The direct-controller targets get a target the OS knows (a bigger one makes
    CC 16-19 jump through a pointer read past the end of the table at $D873: see sc_dc_bad)."""
    if addr in DC_TARGETS: return dp(addr, 0x7F, 0, r.randrange(83))
    mask = r.choice(DP_MASKS); shift = r.randrange(8 - mask.bit_length() + 1)
    return dp(addr, mask, shift, r.randrange(mask + 1))


IDLE = (0xA208, 0xA20B)                      # the main loop waiting for the next tick ($A205)


def call(m, addr):
    """Run the subroutine at addr as the menu would: from the main loop while it waits for a tick, no interrupt
    active (it returns there)."""
    for _ in range(2_000_000):
        if m.pc in IDLE and not m.isrs: break
        m.run(1)
    else: raise Stop('the main loop never got idle')
    m.push16(m.pc); m.calls.append(('call', addr, m.sp)); m.pc = addr


def sc_sysex(R):
    """SysEx: the editor's frames (DP for every patch byte), the OS's own requests (05/06/07/08), patch dumps, the 01
    frame, and broken frames: truncated, unterminated, with status bytes inside, wrong ids, huge."""
    r = R.r
    hdr = HDR
    for a in range(0x00, 0x27C):                          # every edit-buffer offset, a well-formed field each time
        R.send(dp_valid(r, a), drain=False)
        if a % 16 == 0: R.drain(5)
    R.drain(50)
    for s in [0x7F] + r.sample(range(100), 10) + [100, 0x7E]:
        R.send(hdr + [0x05, s, 0xF7]); R.wait(400)
    R.send(hdr + [0x06, 0xF7]); R.wait(300)
    R.send(hdr + [0x07, 0x7F, 0xF7]); R.wait(800)
    R.send(hdr + [0x08, 0xF7]); R.wait(300)
    for _ in range(200):                                  # broken frames
        kind = r.randrange(6)
        if kind == 0: f = hdr[:r.randrange(1, 7)]
        elif kind == 1: f = hdr + [r.randrange(128) for _ in range(r.randrange(40))]
        elif kind == 2: f = hdr + [r.randrange(8)] + [r.randrange(256) for _ in range(r.randrange(20))] + [0xF7]
        elif kind == 3: f = [0xF0] + [r.randrange(128) for _ in range(r.randrange(300))] + [0xF7]
        elif kind == 4: f = hdr + [0x02] + [r.randrange(128) for _ in range(r.randrange(2000))] + [0xF7]
        else: f = [0xF0, 0x2D, r.choice([0x4C, 0x4D, 0x4E, 0x4F])] + [r.randrange(128) for _ in range(30)] + [0xF7]
        R.send(f, drain=False)
        if r.random() < 0.2: R.send([0x90, 60, 100], drain=False)
    R.drain(200)


def sc_dp_wild(R):
    """DP frames the editor would refuse: any 14-bit address, any mask, shift and value (R33 does not check them, so
    this shows how far a bad frame reaches)."""
    r = R.r
    for _ in range(1500):
        R.send(dp(r.randrange(0x4000), r.randrange(128), r.randrange(128), r.randrange(128)), drain=False)
        if r.random() < 0.05: R.note(r.randrange(30, 90), length=20)
    R.drain(100)


def sc_dc(R):
    """Every direct-controller target 0-82 on each of the four controllers, driven by CC 16-19 while notes play."""
    r = R.r
    for slot in r.sample(range(100), 3):
        R.send([0xC0, slot]); R.send([0x90, 45, 100, 52, 100, 57, 100])
        for k, off in enumerate(DC_TARGETS):
            for t in range(83):
                R.send(dp(off, 0x7F, 0, t), drain=False)
                for v in (0, 1, 64, 126, 127, r.randrange(128)): R.send([0xB0, 16 + k, v], drain=False)
                R.drain(3)
        R.send([0xB0, 123, 0])


def sc_memory(R, bank):
    """The patch memory at its limits: factory reset (the Mem menu's routine $8095), the 01 frame, the bank again,
    then stores of ever bigger patches (long tables) into every slot until memory is full, then smaller ones."""
    r = R.r; m = R.m
    call(m, 0x8095); R.wait(500)
    R.send(HDR + [0x01] + [0x45] + [0x2D] * 12 + [0xF7]); R.wait(500)       # 01: clear (as the bank's first frame)
    for f in bank: R.send(f); R.wait(150)
    for rnd in range(3):
        for slot in r.sample(range(100), 100):
            R.send([0xC0, slot]); R.wait(50)
            for osc, base in enumerate((0xF6, 0x178, 0x1FA)):
                n = r.choice([0, 4, 16, 32, 64])
                for k in range(n):
                    R.send(dp(base + 2 * k, 0x7F, 0, r.randrange(0x7F)), drain=False)
                    R.send(dp(base + 2 * k + 1, 0x7F, 0, r.randrange(128)), drain=False)
                R.send(dp(base + 2 * n, 0x7F, 0, 0x7F) + dp(base + 2 * n, 0x01, 7, 1), drain=False)   # FF = end
                R.drain(2)
            R.send(HDR + [0x07, 0x7F, 0xF7]); R.wait(400)                  # store to the current slot
        call(m, 0x8095); R.wait(300)
        for f in r.sample(bank, len(bank)): R.send(f); R.wait(r.choice([0, 20, 150]))


def sc_dumps(R, bank):
    """The bank again: as fast as the cable allows (no pause between frames), then single frames in random order,
    then corrupted patch frames (a byte flipped, cut short, lengths changed)."""
    r = R.r
    R.send(b''.join(bank)); R.wait(500)
    for f in r.sample(bank, 40): R.send(f); R.wait(r.choice([0, 20, 150]))
    for f in r.sample([f for f in bank if f[6] == 2], 30):
        g = bytearray(f); k = r.randrange(7, len(g) - 1); g[k] = r.randrange(128)
        if r.random() < 0.3: g = g[:r.randrange(8, len(g))] + b'\xf7'
        R.send(g); R.wait(150)


def sc_keys(R):
    """The front panel: random walks through the menus with every key, holding keys (auto-repeat of up/down), the
    data encoder, digits for quick access; never the reset-time keys."""
    r = R.r; m = R.m
    for _ in range(600):
        k = r.choice(KEYMAP + 'BBCC##AA')
        R.key(k, hold=r.choice([20, 60, 60, 400, 1500]), gap=r.choice([20, 60, 200]))
        if r.random() < 0.2:
            for _ in range(r.randrange(1, 40)): m.encoder(r.random() < 0.5); R.wait(r.choice([2, 10, 30]))
        if r.random() < 0.1: R.note(r.randrange(30, 90), length=100)


def sc_keys_midi(R):
    """The front panel while MIDI streams in: notes, controllers, DP frames and program changes."""
    r = R.r; m = R.m
    for _ in range(400):
        burst = []
        for _ in range(r.randrange(1, 12)):
            t = r.randrange(5)
            if t == 0: burst += [0x90, r.randrange(30, 90), 100]
            elif t == 1: burst += [0x80, r.randrange(30, 90), 0]
            elif t == 2: burst += [0xB0, r.randrange(128), r.randrange(128)]
            elif t == 3: burst += dp_valid(r, r.randrange(0x27C))
            else: burst += [0xC0, r.randrange(100)]
        R.send(burst, drain=False)
        R.key(r.choice(KEYMAP), hold=r.choice([20, 60, 300]), gap=20)
    R.drain(100)


def sc_asid(R):
    """ASID: into the mode through the menu (found by walking), frames of every kind, then out with each key."""
    r = R.r; m = R.m
    hdr = [0xF0, 0x2D]
    def regs():
        mask = [0x7F] * 4 if r.random() < 0.3 else [r.randrange(128) for _ in range(4)]   # all 28 registers too
        msb = [r.randrange(128) for _ in range(4)]
        n = sum(bin(x).count('1') for x in mask)
        return hdr + [0x4E] + mask + msb + [r.randrange(128) for _ in range(n)] + [0xF7]
    for exit_key in KEYMAP:
        m.pc = 0xFA7D                                     # the menu's ASID action (reached by the walks too)
        m.i = 1
        for _ in range(100):
            k = r.randrange(5)
            if k < 3: f = regs()
            elif k == 3: f = hdr + [0x4F] + [r.randrange(32, 127) for _ in range(r.randrange(40))] + [0xF7]
            else: f = hdr + [0x4D, 0xF7]
            R.send(f, drain=False)
        R.send([0xF8, 0x90, 60, 100] + regs()[:-3] + [0xF7], drain=False)      # clock and a note inside a frame
        R.drain(10)
        R.key(exit_key, hold=80, gap=0)                   # leaves through the reset vector; key may still be held
        R.wait(12_000)                                    # boot (and whatever a held key at reset starts)


def sc_flood(R, bank):
    """Everything at once at the full cable rate for a long time: the worst case for the timer ISR (which runs the
    MIDI consumer with interrupts enabled) and the stack."""
    r = R.r; m = R.m
    for round_ in range(30):
        data = []
        while len(data) < 3000:
            t = r.randrange(9)
            if t == 0: data += [0x90, r.randrange(128), r.randrange(128)]
            elif t == 1: data += [0x80, r.randrange(128), 0]
            elif t == 2: data += [0xB0, r.randrange(128), r.randrange(128)]
            elif t == 3: data += [0xE0, r.randrange(128), r.randrange(128)]
            elif t == 4: data += [0xC0, r.randrange(128)]
            elif t == 5: data += dp_valid(r, r.randrange(0x27C))
            elif t == 6: data += [0xF8]
            elif t == 7: data += list(r.choice(bank))
            else: data += [r.randrange(256)]
        R.send(data, drain=False)
        while m.rx_pending():
            R.wait(r.choice([5, 20, 50]))
            if r.random() < 0.3: R.key(r.choice(KEYMAP.replace('A', '')), hold=30, gap=10)
            if r.random() < 0.3: m.encoder(r.random() < 0.5)


def sc_random(R, bank):
    """Pure random bytes, then random valid messages with random timing."""
    r = R.r
    R.send([r.randrange(256) for _ in range(20000)], drain=True); R.wait(200)
    for _ in range(3000):
        st = r.choice([0x80, 0x90, 0xA0, 0xB0, 0xC0, 0xD0, 0xE0]) | r.randrange(16)
        n = 1 if st & 0xE0 == 0xC0 else 2
        R.send([st] + [r.randrange(128) for _ in range(n)], drain=False)
        if r.random() < 0.05: R.wait(r.randrange(50))
    R.drain(100)


def sc_mix(R, bank):
    """A long random mix of everything a user and a computer can do, in random order and timing."""
    r = R.r; m = R.m
    for _ in range(1500):
        t = r.randrange(12)
        if t == 0: R.key(r.choice(KEYMAP), hold=r.choice([20, 60, 400]), gap=r.choice([10, 60]))
        elif t == 1: R.note(r.randrange(20, 100), r.randrange(1, 128), length=r.choice([5, 50, 300]))
        elif t == 2: R.send([0xB0, r.randrange(128), r.randrange(128)], drain=False)
        elif t == 3: R.send([0xC0, r.randrange(128)], drain=False)
        elif t == 4: R.send(dp_valid(r, r.randrange(0x27C)), drain=False)
        elif t == 5: R.send(HDR + [0x05, r.choice([0x7F, r.randrange(100)]), 0xF7], drain=False)
        elif t == 6: R.send(HDR + [0x06, 0xF7], drain=False)
        elif t == 7: R.send(HDR + [r.choice([0x07, 0x08]), 0x7F if r.random() < 0.5 else 0xF7, 0xF7], drain=False)
        elif t == 8: R.send([0xF8] * r.randrange(1, 50) + [r.choice([0xFA, 0xFB, 0xFC])], drain=False)
        elif t == 9:
            for _ in range(r.randrange(1, 20)): m.encoder(r.random() < 0.5); R.wait(r.choice([1, 8, 30]))
        elif t == 10: R.send(r.choice(bank), drain=False)
        else: R.send([0xE0, r.randrange(128), r.randrange(128), 0xD0, r.randrange(128)], drain=False)
        R.wait(r.choice([0, 1, 5, 20, 100]))
    R.drain(200)


def heap_fault(m):
    """None if the patch memory is consistent, else what is wrong: each slot's handle in $1600-$17FF, its block inside
    $2000-$77FF (size at handle+$200), no two blocks overlapping."""
    M = m.m; blocks = []
    for s in range(100):
        h = M[0x1A00 + 2 * s] << 8 | M[0x1A01 + 2 * s]
        if not h: continue
        if not (0x1600 <= h < 0x1800 and h % 2 == 0): return 'slot %d: handle %04X' % (s, h)
        p = M[h] << 8 | M[h + 1]; n = M[h + 0x200] << 8 | M[h + 0x201]
        if not (0x2000 <= p and p + n <= 0x7800): return 'slot %d: block %04X size %d' % (s, p, n)
        blocks.append((p, n, s))
    blocks.sort()
    for (p, n, s), (q, _, t) in zip(blocks, blocks[1:]):
        if p + n > q: return 'slots %d and %d overlap' % (s, t)
    return None


def heap_end(m):
    M = m.m
    return max([0x2000] + [(M[h] << 8 | M[h + 1]) + (M[h + 0x200] << 8 | M[h + 0x201])
                           for h in range(0x1600, 0x1800, 2) if M[h] | M[h + 1]])


def sc_store_race(R, bank):
    """Stores and deletes while patch frames (02) and clears (01) stream in: the front panel's Store ($AF0D) and Delete
    ($AFC9) and the 07 store, each started at a random point of a frame, the patch memory checked after each."""
    r = R.r; m = R.m
    # Store and Delete end by queueing EXIT key presses for the menu page they belong to ($AF3E, $AFE8), and Store's
    # error path ($AF33) unwinds the menu's caller: called from outside the menu, each returns there instead (RTS).
    for a in (0xAF33, 0xAF3E, 0xAFE8): m.m[a] = 0x39; m.cache.pop(a, None)
    for n in range(300):
        R.send([0xC0, r.randrange(100)]); R.wait(30)
        f = r.choice([f for f in bank if f[6] == 2] * 20 + [HDR + [0x01, 0x45] + [0x2D] * 12 + [0xF7]])
        if r.random() < 0.5:
            R.send(f, drain=False); R.wait(r.uniform(0, len(f) * 0.32))
        if heap_end(m) > 0x7000:                                 # a full memory: the Store's error path ($AF33) expects
            R.send(HDR + [0x01, 0x45] + [0x2D] * 12 + [0xF7]); R.wait(300)   # the menu's caller on the stack, which
            for g in bank: R.send(g, drain=False); R.drain(150)              # call() does not provide
        what = r.choice(['store', 'store', 'delete', '07'])
        cur = m.m[0x1AC9]
        if what != '07' and not (cur < 100 and m.m[0x1A00 + 2 * cur] | m.m[0x1A01 + 2 * cur]):
            what = '07'                                          # the menu offers Store and Delete on a stored patch only
        if what == '07': R.send(HDR + [0x07, 0x7F, 0xF7], drain=False)
        else: call(m, 0xAF0D if what == 'store' else 0xAFC9)
        R.wait(r.uniform(0, 30)); R.send(f, drain=False)
        R.drain(r.choice([20, 150, 400]))
        end = m.cyc + 3 * E_HZ                                   # a store can still be waiting for a frame or moving
        while m.cyc < end:                                       # patches: check when it is done
            busy = {t for k, t, _ in m.calls} & {0xAF0D, 0xAFC9, 0xFA08, 0x99A6, 0xF96D}
            if m.pc in IDLE and not m.isrs and not busy: break
            m.run(1)
        bad = heap_fault(m)
        if bad: raise Stop('patch memory broken after %d rounds: %s' % (n, bad))
        if r.random() < 0.05:                                    # the bank again, now and then
            for g in bank: R.send(g, drain=False); R.drain(150)


def sc_dc_bad(R):
    """Direct-controller targets 83-255, which R33's table at $D873 does not cover, driven by CC 16-19."""
    r = R.r
    for slot in r.sample(range(100), 4):
        R.send([0xC0, slot]); R.send([0x90, 45, 100])
        for k, off in enumerate(DC_TARGETS):
            for t in list(range(83, 0x100, 7)) + [0x7F, 0xFF]:
                R.send(dp(off, 0x7F, 0, t & 0x7F) + dp(off, 0x01, 7, t >> 7), drain=False)
                for v in (0, 64, 127): R.send([0xB0, 16 + k, v], drain=False)
                R.drain(3)
        R.send([0xB0, 123, 0])


def sc_midi_cut(R):
    """Channel messages cut short by another status byte (a cable plugged in while playing, two merged streams):
    every channel message cut after 0 or 1 data bytes by every kind of status byte; after each, a full note must be
    read (the ring buffer, once the engine has read it, empty: nothing left half read). The engine reads about one
    message per tick, so each round waits for it."""
    r = R.r; m = R.m
    msgs = [[0x90, 60, 100], [0x80, 60, 0], [0xB0, 7, 100], [0xE0, 0, 64], [0xA0, 60, 10], [0xC0, 5], [0xD0, 50]]
    for _ in range(300):
        g = r.choice(msgs); cut = g[:r.randrange(1, len(g))]
        st = r.choice([0x90, 0x80, 0xB0, 0xC0, 0xE0, 0x91, 0xF0, 0xF6, 0xF7, 0xF2, 0xF3])
        tail = {0xF0: HDR + [0x06, 0xF7], 0xF2: [0xF2, 1, 2], 0xF3: [0xF3, 1]}.get(st, [st, 61, 90][:3 if st not in (0xC0, 0xF6, 0xF7) else (2 if st == 0xC0 else 1)])
        R.send(cut + tail + [0x90, 62, 80], drain=False); R.drain(10)
        R.send([0x80, 62, 0, 0x80, 61, 0]); R.drain(5)
        for _ in range(100):
            if not m.m[0x1205]: break
            R.wait(20)
        if m.m[0x1205] or m.m[0x1203] != m.m[0x1204]:
            raise Stop('ring buffer left uneven: count %d, write %02X, read %02X' % (m.m[0x1205], m.m[0x1203], m.m[0x1204]))



def sc_threech(R):
    """Three channels (R03): random channel settings in PATCH_MODE2 ($17), notes on every channel, Poly on and off, program
    changes, all notes off and channel changes in the middle of held notes."""
    r = R.r
    for _ in range(300):
        t = r.randrange(10)
        if t == 0: R.send(dp(0x17, 0x0F, r.choice([0, 4]), r.randrange(16)), drain=False)
        elif t == 1: R.send(dp(0x16, 0x01, 3, r.randrange(2)), drain=False)
        elif t == 2: R.send([0xC0, r.randrange(100)], drain=False)
        elif t == 3: R.send([0xB0, 123, 0], drain=False)
        else:
            ch = r.randrange(16)
            for _ in range(r.randrange(1, 6)): R.send([0x90 | ch, r.randrange(128), r.choice([0, 1, 64, 127])], drain=False)
            if r.random() < 0.7: R.send([0x80 | ch, r.randrange(128), 0], drain=False)
        R.wait(r.choice([0, 1, 5, 20, 80]))
    R.drain(500)


SCENARIOS = {'notes': sc_notes, 'cc': sc_cc, 'sweeps': sc_sweeps, 'program': sc_program, 'threech': sc_threech, 'realtime': sc_realtime,
             'sysex': sc_sysex, 'dp_wild': sc_dp_wild, 'dc': sc_dc, 'memory': sc_memory, 'dumps': sc_dumps, 'keys': sc_keys, 'keys_midi': sc_keys_midi, 'asid': sc_asid,
             'flood': sc_flood, 'random': sc_random, 'mix': sc_mix, 'store_race': sc_store_race, 'dc_bad': sc_dc_bad,
             'midi_cut': sc_midi_cut}
NEEDS_BANK = {'dumps', 'flood', 'random', 'memory', 'mix', 'store_race'}


def result(m, name, seed, err, wall):
    return dict(name=name, seed=seed, err=err, wall=wall, synth_s=m.cyc / E_HZ,
                ran=bytes(m.ran), writers={a: sorted(s) for a, s in m.writers.items()},
                readers={a: sorted(s) for a, s in m.readers.items()}, min_sp=m.min_sp, deepest=m.deepest,
                isr_max_nest=m.isr_max_nest, isr_pairs=sorted(m.isr_pairs), isr_longest=m.isr_longest,
                missed=m.missed, overruns=m.overruns, lost=m.lost, events=m.events, event_kinds=m.event_kinds,
                watch={WATCH.get(k, '%04X' % k): v for k, v in m.watch.items()}, screens=sorted(m.screens)[:3000],
                tx=len(m.tx))


def work(job):
    name, seed, snap, bank = job
    m = pickle.loads(snap); t0 = time.time(); err = None
    # each scenario records only its own run: start the recording afresh
    m.ran = bytearray(0x10000); m.writers = {}; m.readers = {}; m.min_sp = 0x10000; m.deepest = []
    m.isr_max_nest = {}; m.isr_pairs = set(); m.isr_longest = {}; m.missed = {}; m.events = []; m.event_kinds = {}
    m.overruns = 0; m.lost = {}; m.screens = set()
    R = Run(m, random.Random(seed))
    try:
        fn = SCENARIOS[name]
        fn(R, bank) if name in NEEDS_BANK else fn(R)
        R.wait(500)
    except Stop as e: err = str(e)
    except Exception as e: err = '%s: %s at pc %04X' % (type(e).__name__, e, m.pc_now)
    return result(m, name, seed, err, time.time() - t0)


def boot(img, bank):
    """Boot from empty RAM, then load the bank through MIDI in with the pauses a librarian leaves (150 ms)."""
    m = Machine(img); R = Run(m, random.Random(0)); t0 = time.time()
    R.wait(10_000)
    for f in bank: R.send(f, drain=False); R.drain(150)
    R.wait(1000)
    return m, time.time() - t0


if __name__ == '__main__':
    args = sys.argv[1:]
    opt = lambda k, d: type(d)(args[args.index(k) + 1]) if k in args else d
    img, _ = image(decode(open(args[0], 'rb').read())[0])
    bank = frames(open(args[1], 'rb').read())
    seeds = opt('--seeds', 2); out = opt('--out', 'explore-report.json')
    only = opt('--only', '')
    snapfile = opt('--snap', '')
    try: snap = open(snapfile, 'rb').read() if snapfile else None
    except FileNotFoundError: snap = None
    if snap is None:
        m, bt = boot(img, bank)
        print('booted and loaded the bank in %.0f s (synth %.1f s); screen %r' % (bt, m.cyc / E_HZ, m.screen()), flush=True)
        snap = pickle.dumps(m)
        if snapfile: open(snapfile, 'wb').write(snap)
    else: m = pickle.loads(snap); bt = 0
    boot_res = result(m, 'boot+bank', 0, None, bt)
    names = [n for n in SCENARIOS if not only or n in only.split(',')]
    jobs = [(n, s, snap, bank) for s in range(1, seeds + 1) for n in names]
    results = [boot_res]
    with mp.Pool(min(8, len(jobs))) as pool:
        for res in pool.imap_unordered(work, jobs):
            results.append(res)
            print('%-10s seed %d: %6.1f s synth in %5.0f s, min SP %04X, err %s, events %s' % (
                res['name'], res['seed'], res['synth_s'], res['wall'], res['min_sp'], res['err'], res['event_kinds']),
                flush=True)
    with open(out.replace('.json', '.pickle'), 'wb') as f: pickle.dump(results, f)
    print('saved', out.replace('.json', '.pickle'))
