# machine.py – the whole SidStation in an emulator: a 68HC11A1 (every opcode), its timer, SCI, A/D converter and
# interrupts, and the board around it (32 KB RAM, the SID at $1040, the LCD at $1060/$1061, the keypad encoder at
# $1080, the data encoder on IC1/IC2). Offline only: MIDI in is a list of bytes given to the machine, MIDI out is
# collected in a list; nothing is sent anywhere.
#
# It also records what explore.py reports: the code that ran, every RAM byte written and by which instruction, the
# deepest stack with the call chain that got there, nested interrupts, missed timer ticks, SCI overruns, and anything
# that should not happen (writes to the flash, the internal EEPROM at $B600-$B7FF, unknown I/O, unknown opcodes).
#
# Clock: E = 2 MHz (8 MHz crystal); the OS sets the timer prescaler to 8, so TCNT counts at 250 kHz, which is what
# its tick formula 250000/period assumes. MIDI: 31250 baud = one byte per 640 E cycles.
E_HZ = 2_000_000
BYTE_CYCLES = 640

IO_LO, IO_HI = 0x1000, 0x1100            # HC11 registers $1000-$103F, board I/O $1040-$10FF
EEPROM = range(0xB600, 0xB800)
SID, LCD_CMD, LCD_DATA, KEYS = 0x1040, 0x1060, 0x1061, 0x1080

# vectors, highest priority first (fixed priority; the OS never writes HPRIO)
VECTORS = {'RESET': 0xFFFE, 'SWI': 0xFFF6, 'ILL': 0xFFF8, 'XIRQ': 0xFFF4, 'IRQ': 0xFFF2, 'RTI': 0xFFF0,
           'IC1': 0xFFEE, 'IC2': 0xFFEC, 'IC3': 0xFFEA, 'OC1': 0xFFE8, 'OC2': 0xFFE6, 'OC3': 0xFFE4, 'OC4': 0xFFE2,
           'OC5': 0xFFE0, 'TOF': 0xFFDE, 'PAOV': 0xFFDC, 'PAI': 0xFFDA, 'SPI': 0xFFD8, 'SCI': 0xFFD6}
TIMER_SOURCES = [('IC1', 0x04), ('IC2', 0x02), ('IC3', 0x01), ('OC1', 0x80), ('OC2', 0x40), ('OC3', 0x20),
                 ('OC4', 0x10), ('OC5', 0x08)]
TOC = {0x80: 0x16, 0x40: 0x18, 0x20: 0x1A, 0x10: 0x1C, 0x08: 0x1E}     # OCxF bit -> TOCx register offset
OCNAME = {0x80: 'OC1', 0x40: 'OC2', 0x20: 'OC3', 0x10: 'OC4', 0x08: 'OC5'}

# --- the opcode table: (prefix, opcode) -> (mnemonic, mode, cycles) -----------------------------------------------
OPT = {}
def _op(pre, op, m, mode, cyc): OPT[(pre, op)] = (m, mode, cyc)
for op, m, cyc in [(0x00, 'TEST', 0), (0x01, 'NOP', 2), (0x02, 'IDIV', 41), (0x03, 'FDIV', 41), (0x04, 'LSRD', 3),
                   (0x05, 'ASLD', 3), (0x06, 'TAP', 2), (0x07, 'TPA', 2), (0x08, 'INX', 3), (0x09, 'DEX', 3),
                   (0x0A, 'CLV', 2), (0x0B, 'SEV', 2), (0x0C, 'CLC', 2), (0x0D, 'SEC', 2), (0x0E, 'CLI', 2),
                   (0x0F, 'SEI', 2), (0x10, 'SBA', 2), (0x11, 'CBA', 2), (0x16, 'TAB', 2), (0x17, 'TBA', 2),
                   (0x19, 'DAA', 2), (0x1B, 'ABA', 2), (0x30, 'TSX', 3), (0x31, 'INS', 3), (0x32, 'PULA', 4),
                   (0x33, 'PULB', 4), (0x34, 'DES', 3), (0x35, 'TXS', 3), (0x36, 'PSHA', 3), (0x37, 'PSHB', 3),
                   (0x38, 'PULX', 5), (0x39, 'RTS', 5), (0x3A, 'ABX', 3), (0x3B, 'RTI', 12), (0x3C, 'PSHX', 4),
                   (0x3D, 'MUL', 10), (0x3E, 'WAI', 14), (0x3F, 'SWI', 14), (0x8F, 'XGDX', 3), (0xCF, 'STOP', 2)]:
    _op(None, op, m, 'inh', cyc)
for k, m in {0x0: 'NEG', 0x3: 'COM', 0x4: 'LSR', 0x6: 'ROR', 0x7: 'ASR', 0x8: 'ASL', 0x9: 'ROL', 0xA: 'DEC',
             0xC: 'INC', 0xD: 'TST', 0xF: 'CLR'}.items():
    _op(None, 0x40 | k, m + 'A', 'inh', 2); _op(None, 0x50 | k, m + 'B', 'inh', 2)
    _op(None, 0x60 | k, m, 'ix', 6); _op(None, 0x70 | k, m, 'ext', 6)
_op(None, 0x6E, 'JMP', 'ix', 3); _op(None, 0x7E, 'JMP', 'ext', 3)
for op, m in zip(range(0x20, 0x30), ['BRA', 'BRN', 'BHI', 'BLS', 'BCC', 'BCS', 'BNE', 'BEQ', 'BVC', 'BVS', 'BPL', 'BMI',
                                     'BGE', 'BLT', 'BGT', 'BLE']):
    _op(None, op, m, 'rel', 3)
_op(None, 0x8D, 'BSR', 'rel', 6)
_op(None, 0x12, 'BRSET', 'bdir', 6); _op(None, 0x13, 'BRCLR', 'bdir', 6)
_op(None, 0x14, 'BSET', 'sdir', 6); _op(None, 0x15, 'BCLR', 'sdir', 6)
_op(None, 0x1C, 'BSET', 'six', 7); _op(None, 0x1D, 'BCLR', 'six', 7)
_op(None, 0x1E, 'BRSET', 'bix', 7); _op(None, 0x1F, 'BRCLR', 'bix', 7)
ALU = {0x0: 'SUB', 0x1: 'CMP', 0x2: 'SBC', 0x4: 'AND', 0x5: 'BIT', 0x6: 'LDA', 0x7: 'STA', 0x8: 'EOR', 0x9: 'ADC',
       0xA: 'ORA', 0xB: 'ADD'}
MODES8 = [('imm8', 2), ('dir', 3), ('ix', 4), ('ext', 4)]
MODES16 = [('imm16', 3), ('dir', 4), ('ix', 5), ('ext', 5)]
for side, reg, base in (('A', 'A', 0x80), ('B', 'B', 0xC0)):
    for k, m in ALU.items():
        for row, (mode, cyc) in enumerate(MODES8):
            if m == 'STA' and mode == 'imm8': continue
            _op(None, base + 0x10 * row + k, m + reg, mode, cyc)
for row, (mode, cyc) in enumerate(MODES16):
    _op(None, 0x83 + 0x10 * row, 'SUBD', mode, cyc + 1); _op(None, 0xC3 + 0x10 * row, 'ADDD', mode, cyc + 1)
    _op(None, 0x8C + 0x10 * row, 'CPX', mode, cyc + 1); _op(None, 0xCC + 0x10 * row, 'LDD', mode, cyc)
    _op(None, 0x8E + 0x10 * row, 'LDS', mode, cyc); _op(None, 0xCE + 0x10 * row, 'LDX', mode, cyc)
    if row:
        _op(None, 0x8F + 0x10 * row, 'STS', mode, cyc); _op(None, 0xCF + 0x10 * row, 'STX', mode, cyc)
        _op(None, 0xCD + 0x10 * row, 'STD', mode, cyc)
        _op(None, 0x8D + 0x10 * row, 'JSR', mode, cyc + 1)
# prefix 18: X -> Y
for (pre, op), (m, mode, cyc) in list(OPT.items()):
    if pre is None and mode in ('ix', 'six', 'bix'):
        ym = {'CPX': 'CPY', 'LDX': 'LDY', 'STX': 'STY'}.get(m, m)
        OPT[(0x18, op)] = (ym, {'ix': 'iy', 'six': 'siy', 'bix': 'biy'}[mode], cyc + 1)
for op, m, mode, cyc in [(0x08, 'INY', 'inh', 4), (0x09, 'DEY', 'inh', 4), (0x30, 'TSY', 'inh', 4),
                         (0x35, 'TYS', 'inh', 4), (0x38, 'PULY', 'inh', 6), (0x3A, 'ABY', 'inh', 4),
                         (0x3C, 'PSHY', 'inh', 5), (0x8F, 'XGDY', 'inh', 4), (0x8C, 'CPY', 'imm16', 5),
                         (0x9C, 'CPY', 'dir', 6), (0xBC, 'CPY', 'ext', 7), (0xCE, 'LDY', 'imm16', 4),
                         (0xDE, 'LDY', 'dir', 5), (0xFE, 'LDY', 'ext', 6), (0xDF, 'STY', 'dir', 5),
                         (0xFF, 'STY', 'ext', 6)]:
    OPT[(0x18, op)] = (m, mode, cyc)
for op, m, mode, cyc in [(0x83, 'CPD', 'imm16', 5), (0x93, 'CPD', 'dir', 6), (0xA3, 'CPD', 'ix', 7),
                         (0xB3, 'CPD', 'ext', 7), (0xAC, 'CPY', 'ix', 7), (0xEE, 'LDY', 'ix', 6),
                         (0xEF, 'STY', 'ix', 6)]:
    OPT[(0x1A, op)] = (m, mode, cyc)
for op, m, mode, cyc in [(0xA3, 'CPD', 'iy', 7), (0xAC, 'CPX', 'iy', 7), (0xEE, 'LDX', 'iy', 6),
                         (0xEF, 'STX', 'iy', 6)]:
    OPT[(0xCD, op)] = (m, mode, cyc)
W16 = {'SUBD', 'ADDD', 'CPD', 'CPX', 'CPY', 'LDD', 'LDX', 'LDY', 'LDS', 'STD', 'STX', 'STY', 'STS', 'JSR', 'JMP'}


class Stop(Exception):
    """Something the machine cannot go on from (unknown opcode, STOP, a run-away PC)."""


class Machine:
    def __init__(self, image, analog=(128, 128, 128, 128)):
        self.m = bytearray(image)
        for a in range(0x0000, 0x8000): self.m[a] = 0
        self.rom = bytes(image)
        self.a = self.b = self.x = self.y = 0; self.sp = 0
        self.h = self.n = self.z = self.v = self.c = 0; self.i = 1; self.xm = 1; self.s = 1
        self.pc = self.r16(VECTORS['RESET'])
        self.cyc = 0
        self.reg = bytearray(0x40)
        self.reg[0x2E] = 0xC0                                         # SCSR: TDRE, TC
        self.reg[0x3D] = 0x01; self.reg[0x3F] = 0x0C                  # INIT, CONFIG (ROM off, EEPROM on, COP off)
        self.shift = 0; self.tcnt0 = 0                                # TCNT = (cyc >> shift) + tcnt0
        self.next_oc = 1 << 62; self.last_t = 0
        self.rx = []; self.rx_i = 0; self.tx = []; self.tx_done = 0
        self.adc_done = 0; self.analog = list(analog) + [0] * 4
        self.keys = 0                                                 # $1080: bit 4 = key held, bits 0-3 = key
        self.sid = bytearray(0x20); self.sid_writes = 0
        self.lcd = bytearray(b' ' * 0x80); self.lcd_addr = 0; self.lcd_cg = False; self.lcd_inc = 1
        self.leds = 0
        # recording
        self.ran = bytearray(0x10000)
        self.writers = {}                                             # RAM address -> set of writer PCs
        self.readers = {}                                             # RAM address -> set of reader PCs
        self.min_sp = 0x10000; self.deepest = []
        self.calls = []                                               # shadow stack: (kind, target, sp after push)
        self.isrs = []                                                # active interrupts: (name, sp, start cycle)
        self.isr_max_nest = {}; self.isr_pairs = set(); self.isr_longest = {}
        self.missed = {}                                              # timer flag set again while its ISR runs
        self.events = []                                              # (cycle, pc, kind, detail)
        self.event_kinds = {}
        self.overruns = 0; self.lost = {}                            # lost byte value -> count
        self.cache = {}
        self.pc_now = self.pc
        self.watch = {}                                               # pc -> times executed (pcs chosen by the caller)
        self.screens = set()
        self.trace = None                                             # a deque of recent (pc, a, b, x, y, sp) when set

    def __getstate__(self):
        d = dict(self.__dict__); d['cache'] = {}; return d                    # the decoded closures are bound to self

    # --- events ---------------------------------------------------------------------------------------------------
    def event(self, kind, detail=''):
        n = self.event_kinds.get(kind, 0); self.event_kinds[kind] = n + 1
        if n < 20: self.events.append((self.cyc, self.pc_now, kind, detail))

    # --- memory ---------------------------------------------------------------------------------------------------
    def r8(self, a):
        if IO_LO <= a < IO_HI: return self.io_read(a)
        if a < 0x8000:
            s = self.readers.get(a)
            if s is None: self.readers[a] = {self.pc_now}
            elif len(s) < 64: s.add(self.pc_now)
        elif 0xB600 <= a < 0xB800: self.event('eeprom read', '%04X' % a)
        return self.m[a]

    def r16(self, a): return self.r8(a) << 8 | self.r8((a + 1) & 0xffff)

    def w8(self, a, v):
        if IO_LO <= a < IO_HI: self.io_write(a, v); return
        if a >= 0x8000:
            self.event('eeprom write' if 0xB600 <= a < 0xB800 else 'flash write', '%04X=%02X' % (a, v)); return
        self.m[a] = v
        s = self.writers.get(a)
        if s is None: self.writers[a] = {self.pc_now}
        elif len(s) < 64: s.add(self.pc_now)

    def w16(self, a, v): self.w8(a, v >> 8 & 0xff); self.w8((a + 1) & 0xffff, v & 0xff)

    # --- the HC11 registers and the board -------------------------------------------------------------------------
    def tcnt(self): return ((self.cyc >> self.shift) + self.tcnt0) & 0xffff

    def plan_oc(self):
        """The cycle at which the next output compare (or the TCNT wrap) is due; flags are set whether or not their
        interrupt is enabled."""
        t = self.tcnt(); self.last_t = t; best = 0x10000 - t
        for off in TOC.values():
            toc = self.reg[off] << 8 | self.reg[off + 1]
            best = min(best, (toc - t) & 0xffff or 0x10000)
        self.next_oc = ((self.cyc >> self.shift) + best) << self.shift

    def timer_update(self):
        """Called when cyc >= next_oc: set the flags of every compare passed since the last update (an instruction
        can take up to 41 cycles, i.e. several timer counts). A flag that is still set when its compare comes round
        again is a lost interrupt."""
        t = self.tcnt(); span = (t - self.last_t) & 0xffff
        for bit, off in TOC.items():
            toc = self.reg[off] << 8 | self.reg[off + 1]
            if 0 < ((toc - self.last_t) & 0xffff or 0x10000) <= span:
                if self.reg[0x23] & bit & self.reg[0x22]:
                    name = OCNAME[bit]; self.missed[name] = self.missed.get(name, 0) + 1
                self.reg[0x23] |= bit
        if 0 < ((0 - self.last_t) & 0xffff or 0x10000) <= span: self.reg[0x25] |= 0x80
        self.plan_oc()

    def io_read(self, a):
        o = a - 0x1000
        if o < 0x40:
            if o == 0x0E: return self.tcnt() >> 8
            if o == 0x0F: return self.tcnt() & 0xff
            if o == 0x2E:
                if self.tx_done and self.cyc >= self.tx_done: self.reg[0x2E] |= 0xC0; self.tx_done = 0
                return self.reg[0x2E]
            if o == 0x2F:
                v = self.reg[0x2F]; self.reg[0x2E] &= ~0x28 & 0xff; return v
            if o == 0x30:
                if self.cyc >= self.adc_done: self.reg[0x30] |= 0x80
                return self.reg[0x30]
            return self.reg[o]
        if a == KEYS: return self.keys
        if SID <= a < SID + 0x20:
            if a in (0x105B, 0x105C): return (self.cyc >> 3) & 0xff
            self.event('sid read', '%04X' % a); return 0
        self.event('unknown io read', '%04X' % a); return 0xFF

    def io_write(self, a, v):
        o = a - 0x1000
        if o < 0x40:
            if o == 0x23: self.reg[0x23] &= ~v & 0xff; return                  # TFLG1: write 1 to clear
            if o == 0x25: self.reg[0x25] &= ~v & 0xff; return                  # TFLG2
            if o == 0x24:
                t = self.tcnt(); self.reg[0x24] = v
                self.shift = {0: 0, 1: 2, 2: 3, 3: 4}[v & 3]
                self.tcnt0 = (t - (self.cyc >> self.shift)) & 0xffff; self.plan_oc(); return
            if o in (0x0E, 0x0F): return                                         # TCNT is read-only
            if o == 0x2F:
                if not self.reg[0x2E] & 0x80: self.event('sci tx overwrite', '%02X' % v)
                self.tx.append((self.cyc, v)); self.reg[0x2E] &= 0x3F; self.tx_done = self.cyc + BYTE_CYCLES; return
            if o == 0x2E: return
            if o == 0x30:
                self.reg[0x30] = v & 0x3F; self.adc_done = self.cyc + (128 if v & 0x10 else 32)
                ch = v & 0x0F; base = ch & 0x0C if v & 0x10 else ch
                for k in range(4): self.reg[0x31 + k] = self.analog[(base + (k if v & 0x10 else 0)) & 7]
                return
            if o in (0x31, 0x32, 0x33, 0x34): return
            if o == 0x3B: self.event('PPROG write (EEPROM programming)', '%02X' % v)
            if o == 0x3A: return
            self.reg[o] = v
            if o in (0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x1F): self.plan_oc()
            if o == 0x00: self.leds = v
            return
        if SID <= a < SID + 0x20: self.sid[a - SID] = v; self.sid_writes += 1; return
        if a == LCD_CMD: self.lcd_command(v); return
        if a == LCD_DATA:
            if self.lcd_cg: self.lcd_addr = (self.lcd_addr + 1) & 0x3F; return
            self.lcd[self.lcd_addr & 0x7F] = v; self.lcd_addr = (self.lcd_addr + self.lcd_inc) & 0x7F
            if len(self.screens) < 5000: self.screens.add(tuple(self.screen()))
            return
        self.event('unknown io write', '%04X=%02X' % (a, v))

    def lcd_command(self, v):
        if v & 0x80: self.lcd_addr = v & 0x7F; self.lcd_cg = False
        elif v & 0x40: self.lcd_addr = v & 0x3F; self.lcd_cg = True
        elif v == 0x01: self.lcd[:] = b' ' * 0x80; self.lcd_addr = 0; self.lcd_cg = False
        elif v & 0xFE == 0x02: self.lcd_addr = 0; self.lcd_cg = False
        elif v & 0xFC == 0x04: self.lcd_inc = 1 if v & 2 else -1

    def screen(self):
        return [bytes(self.lcd[0x00:0x10]).decode('latin-1'), bytes(self.lcd[0x40:0x50]).decode('latin-1')]

    # --- inputs ---------------------------------------------------------------------------------------------------
    def midi(self, data, gap=0):
        """Queue MIDI bytes at the cable's rate, starting now (or after the bytes already queued), gap cycles apart
        on top of the byte time."""
        t = max(self.cyc, self.rx[-1][0] + BYTE_CYCLES if self.rx_i < len(self.rx) else self.cyc)
        for v in data:
            self.rx.append((t, v)); t += BYTE_CYCLES + gap

    def rx_pending(self): return self.rx_i < len(self.rx)

    def encoder(self, up):
        self.reg[0x23] |= 0x04 if up else 0x02

    # --- registers and flags --------------------------------------------------------------------------------------
    def ccr(self):
        return self.s << 7 | self.xm << 6 | self.h << 5 | self.i << 4 | self.n << 3 | self.z << 2 | self.v << 1 | self.c

    def set_ccr(self, v):
        self.s = v >> 7 & 1; self.xm = self.xm & (v >> 6 & 1); self.h = v >> 5 & 1; self.i = v >> 4 & 1
        self.n = v >> 3 & 1; self.z = v >> 2 & 1; self.v = v >> 1 & 1; self.c = v & 1

    def push8(self, v):
        self.w8(self.sp, v); self.sp = (self.sp - 1) & 0xffff
        if self.sp < self.min_sp: self.min_sp = self.sp; self.deepest = self.chain()

    def pull8(self):
        self.sp = (self.sp + 1) & 0xffff; return self.r8(self.sp)

    def push16(self, v): self.push8(v & 0xff); self.push8(v >> 8)
    def pull16(self): h = self.pull8(); return h << 8 | self.pull8()

    def chain(self):
        return [(k, t) for k, t, _ in self.calls]

    def nz8(self, v): self.n = v >> 7; self.z = int(v == 0)
    def nz16(self, v): self.n = v >> 15; self.z = int(v == 0)

    # --- interrupts -----------------------------------------------------------------------------------------------
    def pending(self):
        r = self.reg
        hit = r[0x23] & r[0x22]
        if hit:
            for name, bit in TIMER_SOURCES:
                if hit & bit: return name
        if r[0x25] & r[0x24] & 0x80: return 'TOF'
        sc = r[0x2D]; ss = r[0x2E]
        if sc & 0x20 and ss & 0x28: return 'SCI'
        if sc & 0x80 and ss & 0x80: return 'SCI'
        if sc & 0x40 and ss & 0x40: return 'SCI'
        return None

    def interrupt(self, name):
        self.push16(self.pc); self.push16(self.y); self.push16(self.x); self.push8(self.a); self.push8(self.b)
        self.push8(self.ccr()); self.i = 1
        self.pc = self.r16(VECTORS[name]); self.cyc += 12
        active = [n for n, _, _ in self.isrs]
        if name in active: self.isr_pairs.add((name, name))
        for n in active: self.isr_pairs.add((n, name))
        depth = active.count(name) + 1
        self.isr_max_nest[name] = max(self.isr_max_nest.get(name, 0), depth)
        self.isrs.append((name, self.sp, self.cyc))
        self.calls.append(('irq', name, self.sp))
        if self.sp < self.min_sp: self.min_sp = self.sp; self.deepest = self.chain()

    def rti(self):
        self.set_ccr(self.pull8()); self.b = self.pull8(); self.a = self.pull8()
        self.x = self.pull16(); self.y = self.pull16(); self.pc = self.pull16()
        while self.isrs and self.isrs[-1][1] < self.sp:
            name, _, start = self.isrs.pop()
            self.isr_longest[name] = max(self.isr_longest.get(name, 0), self.cyc - start)
        while self.calls and self.calls[-1][2] < self.sp: self.calls.pop()

    # --- running --------------------------------------------------------------------------------------------------
    def run(self, cycles):
        end = self.cyc + cycles
        rx = self.rx
        while self.cyc < end:
            if self.cyc >= self.next_oc: self.timer_update()
            if self.rx_i < len(rx) and self.cyc >= rx[self.rx_i][0]:
                t, v = rx[self.rx_i]; self.rx_i += 1
                if self.reg[0x2E] & 0x20:
                    self.reg[0x2E] |= 0x08; self.overruns += 1; self.lost[v] = self.lost.get(v, 0) + 1
                    self.event('sci overrun', '%02X' % v)
                else: self.reg[0x2F] = v; self.reg[0x2E] |= 0x20
            if self.tx_done and self.cyc >= self.tx_done: self.reg[0x2E] |= 0xC0; self.tx_done = 0
            if not self.i:
                p = self.pending()
                if p: self.interrupt(p)
            self.step()

    def step(self):
        pc = self.pc; self.pc_now = pc
        self.ran[pc] = 1
        if pc in self.watch: self.watch[pc] += 1
        if self.trace is not None: self.trace.append((pc, self.a, self.b, self.x, self.y, self.sp, self.i))
        if pc < 0x7E00: self.event('execute in RAM', '%04X' % pc)
        f = self.cache.get(pc) if pc >= 0x8000 else None
        if f is None:
            f = self.decode(pc)
            if pc >= 0x8000: self.cache[pc] = f
        f()

    def decode(self, pc):
        m = self.m
        op = m[pc]; pre = None; p = pc + 1
        if op in (0x18, 0x1A, 0xCD):
            pre = op; op = m[p]; p += 1
        info = OPT.get((pre, op))
        if info is None or info[0] == 'TEST':
            def bad():
                self.event('unknown opcode', '%04X: %s' % (pc, bytes(m[pc:pc + 2]).hex()))
                raise Stop('unknown opcode at %04X' % pc)
            return bad
        mn, mode, cyc = info
        # operand fetch, as a function returning the effective address (or the immediate value)
        if mode == 'inh': length = p - pc
        elif mode == 'imm8': imm = m[p]; length = p + 1 - pc
        elif mode == 'imm16': imm = m[p] << 8 | m[p + 1]; length = p + 2 - pc
        elif mode == 'dir': ea = m[p]; length = p + 1 - pc
        elif mode == 'ext': ea = m[p] << 8 | m[p + 1]; length = p + 2 - pc
        elif mode in ('ix', 'iy'): off = m[p]; length = p + 1 - pc
        elif mode == 'rel': off = m[p]; off = off - 256 if off > 127 else off; length = p + 1 - pc
        elif mode in ('sdir', 'six', 'siy'): off = m[p]; mask = m[p + 1]; length = p + 2 - pc
        elif mode in ('bdir', 'bix', 'biy'):
            off = m[p]; mask = m[p + 1]; r = m[p + 2]; r = r - 256 if r > 127 else r; length = p + 3 - pc
        nxt = (pc + length) & 0xffff
        S = self

        if mode in ('dir', 'ext'): EA = lambda: ea
        elif mode in ('ix', 'six', 'bix'): EA = lambda: (S.x + off) & 0xffff
        elif mode in ('iy', 'siy', 'biy'): EA = lambda: (S.y + off) & 0xffff
        elif mode == 'sdir' or mode == 'bdir': EA = lambda: off
        else: EA = None
        if mode == 'imm8': R8 = lambda: imm
        elif EA: R8 = lambda: S.r8(EA())
        if mode == 'imm16': R16 = lambda: imm
        elif EA: R16 = lambda: S.r16(EA())

        def done(c=cyc):
            S.pc = nxt; S.cyc += c

        # --- branches and bit operations ---
        if mode == 'rel':
            tgt = (nxt + off) & 0xffff
            cond = {'BRA': lambda: True, 'BRN': lambda: False, 'BHI': lambda: not (S.c or S.z),
                    'BLS': lambda: S.c or S.z, 'BCC': lambda: not S.c, 'BCS': lambda: S.c, 'BNE': lambda: not S.z,
                    'BEQ': lambda: S.z, 'BVC': lambda: not S.v, 'BVS': lambda: S.v, 'BPL': lambda: not S.n,
                    'BMI': lambda: S.n, 'BGE': lambda: S.n == S.v, 'BLT': lambda: S.n != S.v,
                    'BGT': lambda: not S.z and S.n == S.v, 'BLE': lambda: S.z or S.n != S.v}.get(mn)
            if mn == 'BSR':
                def f():
                    S.push16(nxt); S.calls.append(('call', tgt, S.sp)); S.pc = tgt; S.cyc += cyc
                return f
            def f():
                S.pc = tgt if cond() else nxt; S.cyc += cyc
            return f
        if mode in ('sdir', 'six', 'siy'):
            def f():
                a = EA(); v = S.r8(a); v = (v | mask) if mn == 'BSET' else (v & ~mask & 0xff)
                S.w8(a, v); S.nz8(v); S.v = 0; done()
            return f
        if mode in ('bdir', 'bix', 'biy'):
            def f():
                v = S.r8(EA())
                take = (v & mask) == mask if mn == 'BRSET' else (v & mask) == 0
                S.pc = (nxt + r) & 0xffff if take else nxt; S.cyc += cyc
            return f

        # --- helpers ---
        def sub8(a, b, cin=0):
            res = (a - b - cin) & 0xff
            S.c = int(a < b + cin); S.v = ((a ^ b) & (a ^ res)) >> 7 & 1; S.nz8(res); return res
        def add8(a, b, cin=0):
            full = a + b + cin; res = full & 0xff
            S.c = full >> 8; S.v = (~(a ^ b) & (a ^ res)) >> 7 & 1; S.h = ((a & 0xf) + (b & 0xf) + cin) >> 4 & 1
            S.nz8(res); return res
        def sub16(a, b):
            res = (a - b) & 0xffff
            S.c = int(a < b); S.v = ((a ^ b) & (a ^ res)) >> 15 & 1; S.nz16(res); return res
        def add16(a, b):
            full = a + b; res = full & 0xffff
            S.c = full >> 16; S.v = (~(a ^ b) & (a ^ res)) >> 15 & 1; S.nz16(res); return res
        def getD(): return S.a << 8 | S.b
        def setD(v): S.a = v >> 8 & 0xff; S.b = v & 0xff

        # --- read-modify-write (memory or accumulator) ---
        RMW = {'NEG', 'COM', 'LSR', 'ROR', 'ASR', 'ASL', 'ROL', 'DEC', 'INC', 'TST', 'CLR'}
        def rmw(name, v):
            if name == 'NEG': return sub8(0, v)
            if name == 'COM': v = ~v & 0xff; S.nz8(v); S.v = 0; S.c = 1; return v
            if name == 'LSR': S.c = v & 1; v >>= 1; S.nz8(v); S.v = S.n ^ S.c; return v
            if name == 'ROR': c = S.c; S.c = v & 1; v = v >> 1 | c << 7; S.nz8(v); S.v = S.n ^ S.c; return v
            if name == 'ASR': S.c = v & 1; v = v >> 1 | v & 0x80; S.nz8(v); S.v = S.n ^ S.c; return v
            if name == 'ASL': S.c = v >> 7; v = v << 1 & 0xff; S.nz8(v); S.v = S.n ^ S.c; return v
            if name == 'ROL': c = S.c; S.c = v >> 7; v = (v << 1 | c) & 0xff; S.nz8(v); S.v = S.n ^ S.c; return v
            if name == 'DEC': S.v = int(v == 0x80); v = (v - 1) & 0xff; S.nz8(v); return v
            if name == 'INC': S.v = int(v == 0x7F); v = (v + 1) & 0xff; S.nz8(v); return v
            if name == 'TST': S.nz8(v); S.v = 0; S.c = 0; return None
            if name == 'CLR': S.n = 0; S.z = 1; S.v = 0; S.c = 0; return 0
        if mn in RMW and mode in ('ix', 'iy', 'ext'):
            def f():
                a = EA(); v = rmw(mn, S.r8(a) if mn != 'CLR' else 0)
                if v is not None: S.w8(a, v)
                done()
            return f
        if mn[:-1] in RMW and mode == 'inh' and mn[-1] in 'AB':
            reg = mn[-1].lower(); base = mn[:-1]
            def f():
                v = rmw(base, getattr(S, reg))
                if v is not None: setattr(S, reg, v)
                done()
            return f

        # --- 8-bit ALU ---
        if mn[:-1] in ('SUB', 'CMP', 'SBC', 'AND', 'BIT', 'LDA', 'STA', 'EOR', 'ADC', 'ORA', 'ADD') and mn[-1] in 'AB' \
                and mode != 'inh':
            reg = mn[-1].lower(); base = mn[:-1]
            if base == 'STA':
                def f():
                    v = getattr(S, reg); S.w8(EA(), v); S.nz8(v); S.v = 0; done()
                return f
            def f():
                v = R8(); r = getattr(S, reg)
                if base == 'SUB': setattr(S, reg, sub8(r, v))
                elif base == 'CMP': sub8(r, v)
                elif base == 'SBC': setattr(S, reg, sub8(r, v, S.c))
                elif base == 'ADD': setattr(S, reg, add8(r, v))
                elif base == 'ADC': setattr(S, reg, add8(r, v, S.c))
                else:
                    res = {'AND': r & v, 'BIT': r & v, 'LDA': v, 'EOR': r ^ v, 'ORA': r | v}[base]
                    S.nz8(res); S.v = 0
                    if base != 'BIT': setattr(S, reg, res)
                done()
            return f

        # --- 16-bit ---
        REG16 = {'D': (getD, setD), 'X': (lambda: S.x, lambda v: setattr(S, 'x', v)),
                 'Y': (lambda: S.y, lambda v: setattr(S, 'y', v)), 'S': (lambda: S.sp, lambda v: setattr(S, 'sp', v))}
        if mn in ('LDD', 'LDX', 'LDY', 'LDS'):
            setr = REG16[mn[2]][1]
            def f():
                v = R16(); setr(v); S.nz16(v); S.v = 0; done()
            return f
        if mn in ('STD', 'STX', 'STY', 'STS'):
            getr = REG16[mn[2]][0]
            def f():
                v = getr(); S.w16(EA(), v); S.nz16(v); S.v = 0; done()
            return f
        if mn in ('CPD', 'CPX', 'CPY'):
            getr = REG16[mn[2]][0]
            def f():
                sub16(getr(), R16()); done()
            return f
        if mn == 'ADDD':
            def f(): setD(add16(getD(), R16())); done()
            return f
        if mn == 'SUBD':
            def f(): setD(sub16(getD(), R16())); done()
            return f
        if mn == 'JSR':
            def f():
                t = EA(); S.push16(nxt); S.calls.append(('call', t, S.sp)); S.pc = t; S.cyc += cyc
            return f
        if mn == 'JMP':
            def f(): S.pc = EA(); S.cyc += cyc
            return f

        # --- inherent ---
        def inh():
            if mn == 'NOP': pass
            elif mn == 'RTS':
                S.pc = S.pull16(); S.cyc += cyc
                while S.calls and S.calls[-1][2] < S.sp: S.calls.pop()
                return True
            elif mn == 'RTI': S.rti(); S.cyc += cyc; return True
            elif mn == 'INX': S.x = (S.x + 1) & 0xffff; S.z = int(S.x == 0)
            elif mn == 'DEX': S.x = (S.x - 1) & 0xffff; S.z = int(S.x == 0)
            elif mn == 'INY': S.y = (S.y + 1) & 0xffff; S.z = int(S.y == 0)
            elif mn == 'DEY': S.y = (S.y - 1) & 0xffff; S.z = int(S.y == 0)
            elif mn == 'INS': S.sp = (S.sp + 1) & 0xffff
            elif mn == 'DES':
                S.sp = (S.sp - 1) & 0xffff
                if S.sp < S.min_sp: S.min_sp = S.sp; S.deepest = S.chain()
            elif mn == 'TSX': S.x = (S.sp + 1) & 0xffff
            elif mn == 'TSY': S.y = (S.sp + 1) & 0xffff
            elif mn == 'TXS':
                S.sp = (S.x - 1) & 0xffff
                if S.sp < S.min_sp: S.min_sp = S.sp; S.deepest = S.chain()
            elif mn == 'TYS':
                S.sp = (S.y - 1) & 0xffff
                if S.sp < S.min_sp: S.min_sp = S.sp; S.deepest = S.chain()
            elif mn == 'PSHA': S.push8(S.a)
            elif mn == 'PSHB': S.push8(S.b)
            elif mn == 'PSHX': S.push16(S.x)
            elif mn == 'PSHY': S.push16(S.y)
            elif mn == 'PULA': S.a = S.pull8()
            elif mn == 'PULB': S.b = S.pull8()
            elif mn == 'PULX': S.x = S.pull16()
            elif mn == 'PULY': S.y = S.pull16()
            elif mn == 'ABX': S.x = (S.x + S.b) & 0xffff
            elif mn == 'ABY': S.y = (S.y + S.b) & 0xffff
            elif mn == 'XGDX': d = getD(); setD(S.x); S.x = d
            elif mn == 'XGDY': d = getD(); setD(S.y); S.y = d
            elif mn == 'TAB': S.b = S.a; S.nz8(S.b); S.v = 0
            elif mn == 'TBA': S.a = S.b; S.nz8(S.a); S.v = 0
            elif mn == 'TAP': S.set_ccr(S.a)
            elif mn == 'TPA': S.a = S.ccr()
            elif mn == 'CLC': S.c = 0
            elif mn == 'SEC': S.c = 1
            elif mn == 'CLV': S.v = 0
            elif mn == 'SEV': S.v = 1
            elif mn == 'CLI': S.i = 0
            elif mn == 'SEI': S.i = 1
            elif mn == 'SBA': S.a = sub8(S.a, S.b)
            elif mn == 'CBA': sub8(S.a, S.b)
            elif mn == 'ABA': S.a = add8(S.a, S.b)
            elif mn == 'MUL': d = S.a * S.b; setD(d); S.c = d >> 7 & 1
            elif mn == 'LSRD': d = getD(); S.c = d & 1; d >>= 1; setD(d); S.nz16(d); S.v = S.c
            elif mn == 'ASLD': d = getD(); S.c = d >> 15; d = d << 1 & 0xffff; setD(d); S.nz16(d); S.v = S.n ^ S.c
            elif mn == 'IDIV':
                d = getD()
                if S.x == 0: S.x = 0xffff; S.c = 1; S.event('IDIV by zero')
                else: q, rem = divmod(d, S.x); S.x = q; setD(rem); S.c = 0
                S.z = int(S.x == 0); S.v = 0
            elif mn == 'FDIV':
                d = getD()
                if S.x == 0: S.x = 0xffff; S.c = 1; S.v = 0; S.event('FDIV by zero')
                elif S.x <= d: S.x = 0xffff; S.v = 1; S.event('FDIV overflow')
                else: q, rem = divmod(d << 16, S.x); S.x = q & 0xffff; setD(rem & 0xffff); S.v = 0; S.c = 0
                S.z = int(S.x == 0)
            elif mn == 'DAA':
                a = S.a; c = S.c; cf = 0
                if S.h or (a & 0xf) > 9: cf |= 0x06
                if c or a > 0x99 or ((a >> 4) >= 9 and (a & 0xf) > 9): cf |= 0x60
                res = a + cf; S.c = int(c or res > 0xff); S.a = res & 0xff; S.nz8(S.a)
            elif mn == 'SWI':
                S.pc = nxt; S.event('SWI'); S.interrupt('SWI'); return True
            elif mn == 'WAI':
                S.pc = nxt; S.wait(); return True
            elif mn == 'STOP':
                S.event('STOP'); raise Stop('STOP at %04X' % pc)
            else: raise Stop('no semantics for %s' % mn)
            return False
        def f():
            if not inh(): done()
        return f

    def wait(self):
        """WAI: push the registers, then let time pass until an enabled interrupt is pending."""
        self.push16(self.pc); self.push16(self.y); self.push16(self.x); self.push8(self.a); self.push8(self.b)
        self.push8(self.ccr())
        for _ in range(10_000_000):
            self.cyc += 16
            if self.cyc >= self.next_oc: self.timer_update()
            if self.rx_i < len(self.rx) and self.cyc >= self.rx[self.rx_i][0]:
                t, v = self.rx[self.rx_i]; self.rx_i += 1; self.reg[0x2F] = v; self.reg[0x2E] |= 0x20
            p = self.pending()
            if p and not self.i: break
        else: raise Stop('WAI with no interrupt coming')
        # the registers are already stacked: enter the ISR without stacking again
        self.i = 1; self.pc = self.r16(VECTORS[p])
        self.isrs.append((p, self.sp, self.cyc)); self.calls.append(('irq', p, self.sp))
