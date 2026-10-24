# selftest.py – runs the new code in a small 68HC11 emulator, together with the R33 routines it calls (the real
# R33 code for sending, the size, allocation, free/compaction and save routines), on a patch memory filled with
# generated patches that R33's own save routine has stored. Also plays the boot intro script and counts its ticks.
import random
from sysops import *
# --- emulator: runs the new code and the R33 routines it calls ------------------------------------------------
DEC = {}
for m, modes in OPS.items():
    for mode, hx in modes.items(): DEC[bytes.fromhex(hx)] = (m, mode)
for m, hx in INH.items(): DEC[bytes.fromhex(hx)] = (m, 'inh')
for m, hx in REL.items(): DEC[bytes.fromhex(hx)] = (m, 'rel')
DEC[bytes.fromhex('a3')] = ('SUBD', 'ix')
DEC[bytes.fromhex('14')] = ('BSET', 'dir'); DEC[bytes.fromhex('15')] = ('BCLR', 'dir')
DEC[bytes.fromhex('96')] = ('LDAA', 'dir'); DEC[bytes.fromhex('97')] = ('STAA', 'dir')

class CPU:
    def __init__(self, mem, rx=0):
        self.m = mem; self.a = self.b = 0; self.x = self.y = 0; self.sp = 0x1FF0
        self.n = self.z = self.c = self.v = False; self.i = False
        self.tx, self.writes, self.rx, self.calls = [], [], rx, []
    def r8(self, a):
        a &= 0xffff
        if a == 0x102E: return 0xE0                    # SCSR: TDRE, TC, RDRF
        if a == 0x102F: return self.rx
        return self.m[a]
    def r16(self, a): return self.r8(a) << 8 | self.r8(a + 1)
    def w8(self, a, v):
        a &= 0xffff; v &= 0xff
        assert a < 0x8000, 'write to ROM/flash: %04X' % a
        if a == 0x102F: self.tx.append(v); return
        self.m[a] = v; self.writes.append(a)
    def w16(self, a, v): self.w8(a, v >> 8); self.w8(a + 1, v)
    def push(self, v): self.w8(self.sp, v); self.sp = (self.sp - 1) & 0xffff
    def pull(self): self.sp = (self.sp + 1) & 0xffff; return self.m[self.sp]
    def push16(self, v): self.push(v & 0xff); self.push(v >> 8)
    def pull16(self): h = self.pull(); return h << 8 | self.pull()
    @property
    def d(self): return self.a << 8 | self.b
    @d.setter
    def d(self, v): self.a, self.b = v >> 8 & 0xff, v & 0xff
    def nz(self, v, bits=8): self.n = bool(v >> (bits - 1) & 1); self.z = (v & ((1 << bits) - 1)) == 0
    def cc(self): return 0xc0 | self.i << 4 | self.n << 3 | self.z << 2 | self.v << 1 | self.c
    def setcc(self, v): self.i, self.n, self.z, self.v, self.c = (bool(v & k) for k in (16, 8, 4, 2, 1))
    def sub(self, r, m, bits):
        mask = (1 << bits) - 1; res = (r - m) & mask; s = bits - 1
        self.c = r < m; self.v = bool(((r ^ m) & (r ^ res)) >> s & 1); self.nz(res, bits); return res
    def add(self, r, m, bits, c=0):
        mask = (1 << bits) - 1; full = r + m + c; res = full & mask; s = bits - 1
        self.c = full > mask; self.v = bool((~(r ^ m) & (r ^ res)) >> s & 1); self.nz(res, bits); return res
    def run(self, pc, exits, limit=3_000_000):
        for _ in range(limit):
            if pc in exits: return pc
            k = 2 if self.r8(pc) in (0x18, 0x1a, 0xcd) else 1
            key = bytes(self.m[pc:pc + k])
            assert key in DEC, 'unknown opcode %s at %04X' % (key.hex(), pc)
            m, mode = DEC[key]; p = pc + k
            if mode == 'inh': ea, nxt = None, p
            elif mode == 'rel': off = self.r8(p); ea, nxt = p + 1 + (off - 256 if off > 127 else off), p + 1
            elif mode == 'imm': ea, nxt = p, p + (2 if m in W16 else 1)
            elif mode == 'ext': ea, nxt = self.r16(p), p + 2
            elif mode == 'dir': ea, nxt = self.r8(p), p + 1
            elif mode == 'ix': ea, nxt = (self.x + self.r8(p)) & 0xffff, p + 1
            else: ea, nxt = (self.y + self.r8(p)) & 0xffff, p + 1
            if m in ('BSET', 'BCLR'):
                mask = self.r8(nxt); nxt += 1; v = self.r8(ea)
                v = v | mask if m == 'BSET' else v & ~mask; self.w8(ea, v); self.nz(v); self.v = False
            r = self.step(m, mode, ea, nxt)
            if r == 'RTI': return 'RTI'
            pc = nxt if r is None else r
        raise AssertionError('no exit within %d instructions' % limit)
    def step(self, m, mode, ea, nxt):
        reg8 = {'A': 'a', 'B': 'b'}
        if m in ('BSET', 'BCLR'): return None
        if m in REL:
            n, z, c, v = self.n, self.z, self.c, self.v
            take = {'BRA': True, 'BSR': True, 'BNE': not z, 'BEQ': z, 'BCC': not c, 'BCS': c, 'BMI': n, 'BPL': not n,
                    'BHI': not (c or z), 'BLS': c or z, 'BVS': v, 'BVC': not v, 'BGE': n == v, 'BLT': n != v,
                    'BGT': not z and n == v, 'BLE': z or n != v}[m]
            if m == 'BSR': self.push16(nxt); self.calls.append(ea)
            return ea if take else None
        if m in ('LDAA', 'LDAB'):
            v = self.r8(ea); setattr(self, reg8[m[-1]], v); self.nz(v); self.v = False
        elif m in ('STAA', 'STAB'):
            v = getattr(self, reg8[m[-1]]); self.w8(ea, v); self.nz(v); self.v = False
        elif m in ('LDD', 'LDX', 'LDY'):
            v = self.r16(ea); setattr(self, {'LDD': 'd', 'LDX': 'x', 'LDY': 'y'}[m], v); self.nz(v, 16); self.v = False
        elif m in ('STD', 'STX', 'STY'):
            v = getattr(self, {'STD': 'd', 'STX': 'x', 'STY': 'y'}[m]); self.w16(ea, v); self.nz(v, 16); self.v = False
        elif m == 'ADDD': self.d = self.add(self.d, self.r16(ea), 16)
        elif m == 'SUBD': self.d = self.sub(self.d, self.r16(ea), 16)
        elif m in ('CPD', 'CPX', 'CPY'): self.sub(getattr(self, {'CPD': 'd', 'CPX': 'x', 'CPY': 'y'}[m]), self.r16(ea), 16)
        elif m in ('CMPA', 'CMPB'): self.sub(getattr(self, reg8[m[-1]]), self.r8(ea), 8)
        elif m in ('ANDA', 'ANDB'):
            r = reg8[m[-1]]; v = getattr(self, r) & self.r8(ea); setattr(self, r, v); self.nz(v); self.v = False
        elif m == 'ORAA': self.a |= self.r8(ea); self.nz(self.a); self.v = False
        elif m in ('ADDA', 'ADDB'): r = reg8[m[-1]]; setattr(self, r, self.add(getattr(self, r), self.r8(ea), 8))
        elif m == 'ADCA': self.a = self.add(self.a, self.r8(ea), 8, int(self.c))
        elif m == 'SUBA': self.a = self.sub(self.a, self.r8(ea), 8)
        elif m == 'CLR': self.w8(ea, 0); self.n = self.v = self.c = False; self.z = True
        elif m == 'TST': self.nz(self.r8(ea)); self.v = self.c = False
        elif m in ('INC', 'DEC'):
            v = (self.r8(ea) + (1 if m == 'INC' else -1)) & 0xff; self.w8(ea, v); self.nz(v)
        elif m == 'JSR': self.push16(nxt); self.calls.append(ea); return ea
        elif m == 'JMP': return ea
        elif m == 'RTS': return self.pull16()
        elif m == 'RTI': return 'RTI'
        elif m in ('INX', 'DEX'): self.x = (self.x + (1 if m == 'INX' else -1)) & 0xffff; self.z = self.x == 0
        elif m in ('INY', 'DEY'): self.y = (self.y + (1 if m == 'INY' else -1)) & 0xffff; self.z = self.y == 0
        elif m == 'XGDX': self.d, self.x = self.x, self.d
        elif m == 'XGDY': self.d, self.y = self.y, self.d
        elif m == 'ABX': self.x = (self.x + self.b) & 0xffff
        elif m == 'ABY': self.y = (self.y + self.b) & 0xffff
        elif m in ('CLRA', 'CLRB'): setattr(self, reg8[m[-1]], 0); self.n = self.v = self.c = False; self.z = True
        elif m in ('TSTA', 'TSTB'): self.nz(getattr(self, reg8[m[-1]])); self.v = self.c = False
        elif m == 'TAB': self.b = self.a; self.nz(self.b); self.v = False
        elif m == 'TBA': self.a = self.b; self.nz(self.a); self.v = False
        elif m in ('PSHA', 'PSHB'): self.push(getattr(self, reg8[m[-1]]))
        elif m in ('PULA', 'PULB'): setattr(self, reg8[m[-1]], self.pull())
        elif m == 'PSHX': self.push16(self.x)
        elif m == 'PULX': self.x = self.pull16()
        elif m == 'PSHY': self.push16(self.y)
        elif m == 'PULY': self.y = self.pull16()
        elif m == 'TPA': self.a = self.cc()
        elif m == 'TAP': self.setcc(self.a)
        elif m == 'SEI': self.i = True
        elif m == 'CLI': self.i = False
        elif m == 'NOP': pass
        elif m in ('INCA', 'INCB', 'DECA', 'DECB'):
            r = reg8[m[-1]]; v = (getattr(self, r) + (1 if m[:3] == 'INC' else -1)) & 0xff; setattr(self, r, v); self.nz(v)
        elif m in ('LSRA', 'LSRB'):
            r = reg8[m[-1]]; v = getattr(self, r); self.c = bool(v & 1); v >>= 1; setattr(self, r, v); self.nz(v); self.v = self.c
        elif m in ('ASLA', 'ASLB', 'ROLA', 'ROLB'):
            r = reg8[m[-1]]; v = getattr(self, r); cin = int(self.c) if m[:3] == 'ROL' else 0
            self.c = bool(v & 0x80); v = (v << 1 | cin) & 0xff; setattr(self, r, v); self.nz(v); self.v = self.n != self.c
        elif m == 'ASLD': v = self.d; self.c = bool(v & 0x8000); v = (v << 1) & 0xffff; self.d = v; self.nz(v, 16); self.v = self.n != self.c
        elif m == 'LSRD': v = self.d; self.c = bool(v & 1); v >>= 1; self.d = v; self.nz(v, 16); self.v = self.c
        elif m == 'IDIV':
            if self.x == 0: self.d, self.x, self.c = 0xffff, 0xffff, True   # (not used by the tested paths)
            else: q, rem = divmod(self.d, self.x); self.x, self.d, self.c = q, rem, False
            self.z = self.x == 0; self.v = False
        elif m == 'CBA': self.sub(self.a, self.b, 8)
        elif m == 'ABA': self.a = self.add(self.a, self.b, 8)
        else: raise AssertionError('no semantics for ' + m)
        return None

# --- self-tests ------------------------------------------------------------------------------------------------
SYS, EDIT = 0x1AC8, 0x1B62
HDR = [0xF0, 0x00, 0x20, 0x3C, 0x01, 0x00]
def unstore(data):
    """The inverse of $992A: a stored patch into edit buffer layout (name+global, 3 osc, 4 LFO, 3 tables)."""
    eb = [0] * 0x27C
    eb[0x00:0x24] = data[0:36]
    eb[0x47:0x86] = data[36:99]
    for n in range(4): eb[0x86 + 0x1C * n:0x86 + 0x1C * n + 0x0B] = data[99 + 11 * n:110 + 11 * n]
    k = 143
    for base in (0xF6, 0x178, 0x1FA):
        t = 0
        while t < 0x82:
            eb[base + t] = data[k]; k += 1; t += 1
            if eb[base + t - 1] == 0xFF and (t - 1) % 2 == 0: break
            if eb[base + t - 1] == 0xFE and (t - 1) % 2 == 0: eb[base + t] = data[k]; k += 1; break
            if t < 0x82: eb[base + t] = data[k]; k += 1; t += 1
    return eb, k

class Synth:
    """64K memory = R33 with the new code, patch heap, edit buffer, system block."""
    def __init__(self, img, patches, current=5):
        self.m = bytearray(img)
        for a in range(0x1000, 0x8000): self.m[a] = 0
        self.m[0x1206:0x1208] = SYS.to_bytes(2, 'big'); self.m[0x1220:0x1222] = EDIT.to_bytes(2, 'big')
        self.m[SYS:SYS + 12] = bytes([0, current, current, 1, 120, 3, 0, 0, 0x08, 0x23, 0x0A, 0x2B])   # period 2083 = 120 Hz
        ptr, h = 0x2000, 0x1600
        for slot in sorted(patches):
            size, data = patches[slot]
            self.m[h:h + 2] = ptr.to_bytes(2, 'big'); self.m[h + 0x200:h + 0x202] = size.to_bytes(2, 'big')
            self.m[0x1A00 + 2 * slot:0x1A02 + 2 * slot] = h.to_bytes(2, 'big')
            self.m[ptr:ptr + size] = bytes(data); ptr += size; h += 2
        self.m[STATE:STATE + 2] = IDLE.to_bytes(2, 'big')
        c = CPU(self.m); assert c.run(L['INIT'], {SYSEXINIT}) == SYSEXINIT
    def state(self): return self.m[STATE] << 8 | self.m[STATE + 1]
    def isr(self, byte):
        cpu = CPU(self.m, rx=byte); s = self.state()
        if s == 0x829B:                                  # the original's dispatch, for ids that go via $82B1
            assert byte not in (1, 2, 3, 4); cpu.a = byte; s = L['DISP']
        return cpu.run(s, {IDLE, STATUS, REALTIME}), cpu
    def frame(self, bs):
        outs = []
        for x in bs:
            if not outs: self.m[STATE:STATE + 2] = (0x829B).to_bytes(2, 'big')
            r, cpu = self.isr(x); outs.append(r)
            assert set(cpu.writes) <= {RAM['REQ'], RAM['TMP'], RAM['OP'], RAM['RQS'], RAM['RQW'], STATE, STATE + 1, 0x1000} \
                | set(range(0x1F00, 0x2000)), 'the receiver writes outside its variables'
        return outs
    def tick(self):
        cpu = CPU(self.m); sp0 = cpu.sp
        assert cpu.run(L['TICK'], {WAITTICK}) == WAITTICK and cpu.sp == sp0 and cpu.i is False, 'stack/I flag wrong after TICK'
        return cpu
    def slots(self):
        """slot -> stored bytes, through the slot table and the handles, as the dump reads them."""
        out = {}
        for s in range(100):
            h = int.from_bytes(self.m[0x1A00 + 2 * s:0x1A02 + 2 * s], 'big')
            if h:
                p = int.from_bytes(self.m[h:h + 2], 'big'); n = int.from_bytes(self.m[h + 0x200:h + 0x202], 'big')
                out[s] = bytes(self.m[p:p + n])
        return out
    def unchanged(self, snap):
        """$1600-$77FF as in snap (taken from the same region), except the stack page $1F00-$1FFF (SP starts at $1FFF)."""
        return all(snap[k] == self.m[0x1600 + k] for k in range(len(snap)) if not 0x1F00 <= 0x1600 + k < 0x2000)
    def heap_ok(self):
        blocks = sorted((int.from_bytes(self.m[h:h + 2], 'big'), int.from_bytes(self.m[h + 0x200:h + 0x202], 'big'))
                        for h in range(0x1600, 0x1800, 2) if int.from_bytes(self.m[h:h + 2], 'big'))
        p = 0x2000
        for a, n in blocks: assert a == p, 'heap not contiguous'; p += n
        return p

TABLES = (0xF6, 0x178, 0x1FA)   # wave, pulse width and filter table in the edit buffer, 0x82 bytes each

def edit_buffer(rnd, unterminated=False):
    """A made-up patch in edit buffer layout: random values in every stored field, three tables of random steps
    ending with FF (end) or FE n (loop), or, if unterminated, a first table filling all 0x82 bytes without an end."""
    eb = [0] * 0x27C
    eb[0:10] = [rnd.randrange(0x20, 0x7F) for _ in range(10)]
    for a, b in [(10, 0x24), (0x47, 0x86)] + [(0x86 + 0x1C * n, 0x86 + 0x1C * n + 0x0B) for n in range(4)]:
        eb[a:b] = [rnd.randrange(256) for _ in range(b - a)]
    step = lambda: [rnd.randrange(0xFE), rnd.randrange(256)]
    for k, base in enumerate(TABLES):
        if unterminated and k == 0: t = [x for _ in range(0x41) for x in step()]
        else: t = [x for _ in range(rnd.randrange(12)) for x in step()] + rnd.choice([[0xFF], [0xFE, rnd.randrange(8)]])
        eb[base:base + len(t)] = t
    return eb

def generated_patches(img, rnd, count=60):
    """slot -> (size, stored bytes): made-up patches stored one by one by R33's own save routine ($99A6). Slots 5 and 7
    are always occupied (the tests use them); a few patches have an unterminated table."""
    sy = Synth(img, {}); made = {}
    for slot in sorted({5, 7} | set(rnd.sample(range(100), count - 2))):
        eb = edit_buffer(rnd, unterminated=rnd.random() < 0.1)
        sy.m[EDIT:EDIT + len(eb)] = bytes(eb)
        c = CPU(sy.m); c.b = slot; c.push16(0); c.run(SAVE, {0})
        assert c.x, 'the generated patches do not fit'
        made[slot] = eb
    pats = {s: (len(d), list(d)) for s, d in sy.slots().items()}
    assert sorted(pats) == sorted(made)
    for s, (size, data) in pats.items():
        eb, n = unstore(data)
        assert eb == made[s] and n <= size, 'slot %d: stored and read back differently' % s
    sy.heap_ok()
    return pats

def nib(tx):
    assert all(x < 0x10 for x in tx)
    return [tx[k] | tx[k + 1] << 4 for k in range(0, len(tx), 2)]

def reply(cpu, rid):
    t = cpu.tx; assert t[:6] == HDR and t[6] == rid and t[-1] == 0xF7, t[:8]
    return nib(t[7:-1])

def selftest(img, seed=1):
    rnd = random.Random(seed)
    pats = generated_patches(img, rnd)
    # 06: the settings reply
    sy = Synth(img, pats); sy.m[EDIT + 0x22] = 100; sy.m[EDIT + 0x23] = 4; sy.m[0x1238] = 0
    assert sy.frame([6, 0xF7]) == ['RTI', 'RTI'] and sy.state() == IDLE
    v = reply(sy.tick(), 0x46)
    assert v == [1, 0, 5, 5, 1, 120, 3, 0, 100, 4, 0x08, 0x23, 0x0A, 0x2B, 0], v
    assert sy.tick().tx == [], 'one request gives one reply'
    assert sy.frame([6, 0x00]) == ['RTI', 'RTI'] and sy.tick().tx == [], '06 with a data byte: no request'
    # 05 as before: slot 7, 7F = current, 99, refusals
    sy.frame([5, 7, 0xF7]); c = sy.tick(); assert c.tx[6] in (2, 3) and DUMP in c.calls
    sy.frame([5, 0x7F, 0xF7]); c = sy.tick(); assert c.tx[6] == 2 and (c.tx[8] << 7 | c.tx[9]) == pats[5][0]
    for bad in (100, 0x7D, 0x7E): assert sy.frame([5, bad]) == ['RTI', 'RTI'] and sy.state() == IDLE and sy.tick().tx == []
    assert sy.frame([5, 0xF8, 3, 0xFE, 0xF7]) == ['RTI', REALTIME, 'RTI', REALTIME, 'RTI'] and sy.tick().tx[6] in (2, 3)
    sy.frame([5]); r, cpu = sy.isr(0x90); assert r == STATUS and cpu.a == 0x90 and sy.tick().tx == []
    for bad in (0, 8, 0x45, 0x46, 0x47, 0x7F):
        sy.m[STATE:STATE + 2] = (0x829B).to_bytes(2, 'big'); r, cpu = sy.isr(bad)
        assert r == 'RTI' and sy.state() == IDLE and set(cpu.writes) <= {STATE, STATE + 1}, 'id %02X' % bad
    # 07 with anything but 7F is refused
    for bad in (0, 5, 99, 100): assert sy.frame([7, bad]) == ['RTI', 'RTI'] and sy.state() == IDLE and sy.tick().tx == []

    def store(sy):
        assert sy.frame([7, 0x7F, 0xF7]) == ['RTI', 'RTI', 'RTI'] and sy.state() == IDLE
        c = sy.tick(); v = reply(c, 0x47); assert len(v) == 8 and sy.tick().tx == []
        return v[0], v[1], v[2] << 8 | v[3], v[4] << 8 | v[5], v[6] << 8 | v[7]

    # 07: every occupied slot stored back unchanged (edit buffer = the stored patch): same bytes, others untouched
    for cur in sorted(rnd.sample(sorted(pats), 12)) + [min(pats), max(pats)]:
        sy = Synth(img, pats, cur); before = sy.slots()
        eb, n = unstore(pats[cur][1]); sy.m[EDIT:EDIT + len(eb)] = bytes(eb)
        st, slot, size, cs, free = store(sy)
        after = sy.slots(); end = sy.heap_ok()
        terminated = n == pats[cur][0]
        assert st == 0 and slot == cur and free == HEAPTOP - end
        if terminated:
            assert after == before, 'slot %d: the stored patch or another patch changed' % cur
            assert size == len(after[cur]) and cs == sum(after[cur]) & 0xffff
        else:
            assert {s: d for s, d in after.items() if s != cur} == {s: d for s, d in before.items() if s != cur}
    # a changed and bigger patch (name, an extra table step) into a slot in the middle; the dump reads it back
    cur = sorted(pats)[len(pats) // 2]
    sy = Synth(img, pats, cur); before = sy.slots()
    eb, _ = unstore(pats[cur][1]); eb[0:10] = list(b'SysopsTest'); eb[0xF6:0xF6 + 6] = [0x10, 0x30, 0x20, 0x40, 0xFE, 0x00]
    sy.m[EDIT:EDIT + len(eb)] = bytes(eb)
    st, slot, size, cs, free = store(sy); after = sy.slots()
    assert st == 0 and after[cur][:10] == b'SysopsTest' and cs == sum(after[cur]) & 0xffff and size == len(after[cur])
    assert {s: d for s, d in after.items() if s != cur} == {s: d for s, d in before.items() if s != cur}
    assert free == HEAPTOP - sy.heap_ok()
    sy.frame([5, 0x7F, 0xF7]); t = sy.tick().tx; assert bytes(t[35:45]) == b'SysopsTest'
    # an empty current slot
    empty = min(s for s in range(100) if s not in pats)
    sy = Synth(img, pats, empty); eb, _ = unstore(pats[cur][1]); sy.m[EDIT:EDIT + len(eb)] = bytes(eb)
    st, slot, size, cs, free = store(sy)
    assert st == 0 and slot == empty and sy.slots()[empty] == bytes(pats[cur][1][:size]) and sy.m[SYS + 1] == empty
    # memory full: a filler block so the new patch does not fit -> status 1, memory untouched; exactly fitting -> stored
    for spare, want in ((-1, 1), (0, 1), (1, 0)):
        sy = Synth(img, pats, cur); eb, _ = unstore(pats[cur][1]); eb[0xF6:0xF6 + 6] = [0x10, 0x30, 0x20, 0x40, 0xFE, 0x00]
        sy.m[EDIT:EDIT + len(eb)] = bytes(eb)
        end = sy.heap_ok(); old = pats[cur][0]
        c = CPU(sy.m); c.push16(0); c.run(SIZE, {0}); newsz = c.x    # the size the OS will need, from its own routine
        filler = HEAPTOP - (end - old + newsz) - spare     # end after the store = HEAPTOP - spare
        h = 0x17FE; sy.m[h:h + 2] = end.to_bytes(2, 'big'); sy.m[h + 0x200:h + 0x202] = filler.to_bytes(2, 'big')
        snap = bytes(sy.m[0x1600:HEAPTOP])
        st, slot, size, cs, free = store(sy)
        assert st == want, (spare, st)
        if want: assert sy.unchanged(snap) and size == newsz, 'memory full must change nothing'
        else: assert free == spare and sy.heap_ok() == HEAPTOP - spare
    # busy and invalid slot: nothing changed
    sy = Synth(img, pats, cur); snap = bytes(sy.m[0x1600:HEAPTOP])
    sy.frame([7, 0x7F, 0xF7]); sy.m[STATE:STATE + 2] = L['ST_SLOT'].to_bytes(2, 'big')
    v = reply(sy.tick(), 0x47); assert v[0] == 3 and sy.unchanged(snap)
    sy = Synth(img, pats, 100); snap = bytes(sy.m[0x1600:HEAPTOP])
    st, *_ = store(sy); assert st == 2 and sy.unchanged(snap)
    # all three requests pending at once: dump, settings, store in that order
    sy = Synth(img, pats, cur); sy.frame([5, 7, 0xF7]); sy.frame([6, 0xF7]); sy.frame([7, 0x7F, 0xF7])
    t = sy.tick().tx; ids = [t[k + 6] for k in range(len(t) - 6) if t[k:k + 6] == HDR]
    assert ids[0] in (2, 3) and ids[1:] == [0x46, 0x47], ids
    shots, t, k = intro(img)
    assert shots[1][1][0][5:14] == 'OS1.12R01' and shots[1][1][1][5:16] == 'norrby/2026', shots
    assert k == 0xE24F and t <= 142, (hex(k), t)            # ends on its 00 80 hold, within the 142 ticks
    return 'self-test ok (%d generated patches; intro %r / %r, %d ticks)' % (len(pats), shots[1][1][0][4:], shots[1][1][1][4:], t)

def intro(img):
    """Runs the intro script ($E1F6, as $E155 reads it): the screen at each wipe and the tick count."""
    scr, cur, k, t, shots = [[' '] * 16 for _ in range(2)], 0, 0xE1F6, 0, []
    put = lambda pos, ch: scr[pos >> 6 & 1].__setitem__(pos & 0x3F, ch) if (pos & 0x3F) < 16 else None
    while k < 0xE24F:
        b = img[k]
        if b >= 0x80:
            if cur: put(cur, ' ')
            cur = b & 0x7F; k += 1
        elif b == 0x00:
            d = img[k + 1]; k += 2
            if d >= 0x80: break
            t += d
        elif b == 0x01:
            shots.append((t, [''.join(r) for r in scr])); k += 1; t += 12
            for r in scr: r[4:] = [' '] * 12
        else:
            put(cur, chr(b)); cur += 1; put(cur, '#'); t += img[k + 1] + 1; k += 2
    return shots, t, k
