# sysops.py – the patch: three new SysEx operations for SidStation OS 1.10 R33, and a new boot intro.
# Builds the new OS file from Elektron's original; everything else stays byte for byte as in R33 (checked).
#
#   05 dump request      F0 00 20 3C 01 00 05 <slot> F7   slot 0..99 or 7F = current. Reply: the menu's Patch dump ($8457),
#                                                         an 02 frame, or an 03 frame for an empty slot
#   06 settings request  F0 00 20 3C 01 00 06 F7          Reply: F0 00 20 3C 01 00 46 <15 values> F7
#   07 store             F0 00 20 3C 01 00 07 7F F7       stores the edit buffer to the current slot.
#                                                         Reply: F0 00 20 3C 01 00 47 <8 values> F7
# The replies are described in docs/protocol.md.
#
# The store does what the front panel's Store does ($AF0D-$AF2A: free the old patch, $FA08, then $99A6), except that it
# first checks that the new patch fits (the front panel frees first, so a full memory loses the old patch).
#
# Changes to the original:
#   $808E  JSR $8114 -> JSR INIT    init: no pending requests before the SysEx state is set
#   $82B1  LDD #$81F8 -> JMP DISP   the ID dispatch: an unknown id (not 01..04) is tested against 05..07
#   $A1EB  JSR $A205 -> JSR TICK    the main loop: pending requests are served before the wait for the next tick
# New code at $B400, below $B600 (on an E-series HC11 $B600-$B7FF can be internal EEPROM).
# Data: the boot intro shows "OS1.12R01" / "norrby/2026" instead of "OS1.10 R33" / "(C) 2000". 1.12 because Elektron's
# last OS is 1.11 R34. The second line needs 4 more bytes than the original; they come from the pauses before line 1
# (00 08) and after line 2 (00 14), folded into the wait of the last character; the two extra characters cost one tick
# each (140 ticks, limit 142). The intro is a script at $E1F6-$E24E read by $E155: 80-FF = cursor, 00 d = pause,
# 01 = wipe, c d = print c then wait d ticks; nothing else reads these bytes.
#
# The file contains only the new bytes and where they go. The original bytes it lists (the hooks, the intro) are there
# to be checked before anything is replaced.
from osdecode import decode, encode, image
from hc11 import disassemble

ORG, LIMIT = 0xB400, 0xB600
FREE = (0xB0E5, 0xB7FF)
RAM = {'REQ': 0x13E0, 'TMP': 0x13E1, 'OP': 0x13E2, 'RQS': 0x13E3, 'RQW': 0x13E4,
       'SLOT': 0x13E5, 'NEWSZ': 0x13E6, 'SUM': 0x13E8, 'FREEB': 0x13EA,     # in reply order (sent as one run)
       'OLDH': 0x13EC, 'HEND': 0x13EE, 'PER': 0x13F0}                       # PER: 5 bytes
IDLE = 0x81F8          # the SysEx idle state (state pointer $7FFC)
STATE = 0x7FFC
STATUS = 0x8142        # handles A as a new status byte (does its own TAB)
REALTIME = 0x8147      # handles B as a realtime byte (F8/FA/FB), RTI, state untouched
DUMP = 0x8457          # the single-patch dump: Y = $1A00 + 2*slot, X = (Y)
WAITTICK = 0xA205
SYSEXINIT = 0x8114
LEDOFF = 0x8849        # BSET $1000 bit 5: SysEx LED off when a frame is done ($8297 turned it on)
HEADER = 0x84D5        # sends F0 00 20 3C 01 00, X preserved
TX = 0x9A63            # sends A
TXNIB = 0x84F0         # sends A as low nibble, high nibble; A preserved
SIZE = 0x9909          # X = size the edit buffer needs when stored; D preserved
HEAPEND = 0xF9CD       # D = end of the patch heap; X preserved
FREEH = 0xFA08         # frees handle X and compacts the heap
SAVE = 0x99A6          # stores the edit buffer to slot B; X = data pointer, or 0 if it does not fit
REDRAW = 0x1323        # CLR = redraw the display (as the front panel after a store)
HEAPTOP = 0x7800

# --- a small HC11 assembler (one table, used for assembling and for the emulator; the listing is cross-checked
# against the independent disassembler hc11.py) -------------------------------------------------------------------
W16 = {'LDD', 'STD', 'LDX', 'STX', 'LDY', 'STY', 'ADDD', 'SUBD', 'CPD', 'CPX', 'CPY', 'LDS'}
OPS = {
    'LDAA': dict(imm='86', ext='b6', ix='a6', iy='18a6'), 'LDAB': dict(imm='c6', ext='f6', ix='e6', iy='18e6'),
    'STAA': dict(ext='b7', ix='a7', iy='18a7'), 'STAB': dict(ext='f7', ix='e7', iy='18e7'),
    'LDD': dict(imm='cc', ext='fc', ix='ec', iy='18ec'), 'STD': dict(ext='fd', ix='ed', iy='18ed'),
    'LDX': dict(imm='ce', ext='fe', ix='ee', iy='cdee'), 'STX': dict(ext='ff', ix='ef', iy='cdef'),
    'LDY': dict(imm='18ce', ext='18fe', ix='1aee', iy='18ee'), 'STY': dict(ext='18ff', ix='1aef', iy='18ef'),
    'ADDD': dict(imm='c3', ext='f3', ix='e3', iy='18e3'), 'SUBD': dict(imm='83', ext='b3', ix='a3', iy='18a3'),
    'CPD': dict(imm='1a83', ext='1ab3', ix='1aa3', iy='cda3'), 'CPX': dict(imm='8c', ext='bc', ix='ac', iy='cdac'),
    'CPY': dict(imm='188c', ext='18bc', ix='1aac', iy='18ac'),
    'CMPA': dict(imm='81', ext='b1', ix='a1', iy='18a1'), 'CMPB': dict(imm='c1', ext='f1', ix='e1', iy='18e1'),
    'ANDA': dict(imm='84', ix='a4'), 'ANDB': dict(imm='c4', ix='e4'), 'ORAA': dict(imm='8a'),
    'ADDA': dict(imm='8b'), 'ADCA': dict(imm='89'), 'ADDB': dict(imm='cb', ix='eb'), 'SUBA': dict(imm='80'),
    'CLR': dict(ext='7f', ix='6f', iy='186f'), 'TST': dict(ext='7d', ix='6d', iy='186d'),
    'INC': dict(ext='7c', ix='6c'), 'DEC': dict(ext='7a', ix='6a'),
    'JSR': dict(ext='bd', ix='ad'), 'JMP': dict(ext='7e', ix='6e'),
    'BSET': dict(ix='1c'), 'BCLR': dict(ix='1d'),
}
INH = {'INX': '08', 'DEX': '09', 'INY': '1808', 'DEY': '1809', 'XGDX': '8f', 'XGDY': '188f', 'ABX': '3a', 'ABY': '183a',
       'CLRA': '4f', 'CLRB': '5f', 'TSTA': '4d', 'TSTB': '5d', 'TAB': '16', 'TBA': '17', 'PSHA': '36', 'PSHB': '37',
       'PULA': '32', 'PULB': '33', 'PSHX': '3c', 'PULX': '38', 'PSHY': '183c', 'PULY': '1838', 'TPA': '07', 'TAP': '06',
       'SEI': '0f', 'CLI': '0e', 'RTS': '39', 'RTI': '3b', 'NOP': '01', 'LSRA': '44', 'LSRB': '54', 'ASLA': '48',
       'ASLB': '58', 'ROLA': '49', 'ROLB': '59', 'INCA': '4c', 'INCB': '5c', 'DECA': '4a', 'DECB': '5a', 'ASLD': '05',
       'LSRD': '04', 'IDIV': '02', 'CBA': '11', 'ABA': '1b', 'COMA': '43', 'NEGA': '40', 'TSX': '30', 'TXS': '35',
       'INS': '31', 'DES': '34'}
REL = {'BRA': '20', 'BNE': '26', 'BEQ': '27', 'BCC': '24', 'BCS': '25', 'BMI': '2b', 'BPL': '2a', 'BHI': '22',
       'BLS': '23', 'BSR': '8d', 'BVS': '29', 'BVC': '28', 'BGE': '2c', 'BLT': '2d', 'BGT': '2e', 'BLE': '2f'}

def num(s, labels):
    s = s.strip()
    if s.startswith('$'): return int(s[1:], 16)
    if s[0].isdigit(): return int(s)
    return labels[s] if labels is not None else 0

def assemble(src, org):
    """Two passes over 'label:' / 'OP operand' lines; ';' starts a comment."""
    lines = []
    for raw in src.splitlines():
        t = raw.split(';')[0].strip()
        if t: lines.append(t)
    labels = {}
    for pas in (0, 1):
        pc, out = org, bytearray()
        for t in lines:
            if t.endswith(':'):
                if pas == 0: assert t[:-1] not in labels, t; labels[t[:-1]] = pc
                continue
            op, _, arg = t.partition(' '); arg = arg.strip(); L = labels if pas else None
            if op in INH:
                assert not arg, t; code = bytes.fromhex(INH[op])
            elif op in REL:
                tgt = num(arg, L) if pas else pc
                off = tgt - (pc + 2)
                assert -128 <= off <= 127 or not pas, 'branch out of range: ' + t
                code = bytes.fromhex(REL[op]) + bytes([off & 0xff])
            elif op in ('BSET', 'BCLR'):
                a, m = arg.split(' ')
                code = bytes.fromhex(OPS[op]['ix']) + bytes([num(a.split(',')[0], L), num(m.lstrip('#'), L)])
            else:
                if arg.startswith('#'): mode, v = 'imm', num(arg[1:], L)
                elif arg.endswith(',X'): mode, v = 'ix', num(arg[:-2], L)
                elif arg.endswith(',Y'): mode, v = 'iy', num(arg[:-2], L)
                else: mode, v = 'ext', num(arg, L)
                code = bytes.fromhex(OPS[op][mode])
                if mode in ('ix', 'iy'): assert 0 <= v <= 255, t; code += bytes([v])
                elif mode == 'imm' and op not in W16: assert 0 <= v <= 255, t; code += bytes([v])
                else: code += bytes([v >> 8 & 0xff, v & 0xff])
            out += code; pc += len(code)
    return bytes(out), labels

def R(n): return '$%04X' % RAM[n]

SRC = f"""
; INIT: called from $808E with interrupts disabled (SEI at $8000, CLI at $8091)
INIT:
    LDAA #$FF
    STAA {R('REQ')}
    STAA {R('RQS')}
    STAA {R('RQW')}
    JMP ${SYSEXINIT:04X}

; DISP: from $82B1, A = the id byte after the header, not 01..04
DISP:
    CMPA #$05
    BEQ D_SLOT
    CMPA #$07
    BEQ D_SLOT
    CMPA #$06
    BEQ D_END
IDLE_RTI:
    LDX #${IDLE:04X}                 ; unknown id: idle state, as the original
    STX ${STATE:04X}
    RTI
D_SLOT:
    STAA {R('OP')}
    LDX #ST_SLOT
    STX ${STATE:04X}
    RTI
D_END:
    STAA {R('OP')}
    LDX #ST_END
    STX ${STATE:04X}
    RTI

; ST_SLOT: waiting for the slot byte (05: 0..99 or 7F; 07: 7F only)
ST_SLOT:
    LDAA $102E                      ; SCSR, acknowledges together with the read of SCDR
    LDAA $102F
    BMI STAT
    CMPA #$7F
    BEQ S_OK
    LDAB {R('OP')}
    CMPB #$07
    BEQ ABORT                       ; store: only the current slot
    CMPA #100
    BCC ABORT
S_OK:
    STAA {R('TMP')}
    LDX #ST_END
    STX ${STATE:04X}
    RTI

; STAT: status byte in the middle of a frame. Realtime (F8..FF) is handled without changing the state;
; anything else (80..F7) aborts: idle state, and unless it was F7 the byte is handled as a new message.
STAT:
    CMPA #$F8
    BCS NOTRT
    TAB
    JMP ${REALTIME:04X}
NOTRT:
    JSR ${LEDOFF:04X}                ; frame done, like the original receivers; preserves A
    LDX #${IDLE:04X}
    STX ${STATE:04X}
    CMPA #$F7
    BEQ RTI1
    JMP ${STATUS:04X}                ; A = the status byte
RTI1:
    RTI
ABORT:
    JSR ${LEDOFF:04X}
    BRA IDLE_RTI

; ST_END: waiting for F7; only then is the request registered (one byte each, atomic for the main loop)
ST_END:
    LDAA $102E
    LDAA $102F
    CMPA #$F7
    BNE E_NOT
    LDAB {R('OP')}
    CMPB #$05
    BNE E_6
    LDAB {R('TMP')}
    STAB {R('REQ')}
    BRA E_DONE
E_6:
    CMPB #$06
    BNE E_7
    CLR {R('RQS')}
    BRA E_DONE
E_7:
    CLR {R('RQW')}
E_DONE:
    BRA ABORT                       ; frame done: LED off, idle state
E_NOT:
    TSTA                            ; N after CMPA reflects the comparison, not the byte
    BMI STAT
    BRA ABORT                       ; data byte instead of F7: malformed frame, no request

; TICK: from the main loop $A1EB instead of JSR $A205 (no registers are live there)
TICK:
    PSHX
    PSHY
    LDX #{R('REQ')}
    BSR TAKE
    BMI T_S                         ; >= 80: no dump request
    CMPB #$7F
    BNE T_CHK
    LDX $1206
    LDAB $01,X                      ; current slot, as $B061
T_CHK:
    CMPB #100
    BCC T_S                         ; invalid slot: no dump
    LDY #$1A00
    ABY
    ABY
    LDX $00,Y                       ; as the menu at $B07E
    JSR ${DUMP:04X}
T_S:
    LDX #{R('RQS')}
    BSR TAKE
    BNE T_W
    JSR SETTINGS
T_W:
    LDX #{R('RQW')}
    BSR TAKE
    BNE T_END
    JSR STORE
T_END:
    PULY
    PULX
    JMP ${WAITTICK:04X}

; TAKE: B = the request byte at X, which is set to FF (none); read and clear with interrupts off. Flags from TSTB.
TAKE:
    TPA
    SEI
    LDAB $00,X
    PSHA
    LDAA #$FF
    STAA $00,X
    PULA
    TAP                             ; I flag as before
    TSTB
    RTS

; SETTINGS: the 46 reply
SETTINGS:
    LDX $1206
    TPA                             ; the period is two bytes written by the menu, read by the timer interrupt
    SEI
    PSHA
    LDD $08,X
    STD {R('PER')}
    LDD $0A,X
    STD ${RAM['PER'] + 2:04X}
    LDAA $1238
    STAA ${RAM['PER'] + 4:04X}
    PULA
    TAP
    JSR ${HEADER:04X}
    LDAA #$46
    JSR ${TX:04X}
    LDAA #$01                       ; format version
    JSR ${TXNIB:04X}
    LDAB #7                         ; system block +0..+6: BaseCh, current slot, loaded slot, Sourc, Speed, HCut, SYSEXDump
    JSR ${0x84E6:04X}
    LDX $1220
    LDAB #$22
    ABX
    LDAB #2                         ; patch Speed, patch HCut
    JSR ${0x84E6:04X}
    LDX #{R('PER')}
    LDAB #5
    JSR ${0x84E6:04X}               ; B bytes from (X)+ as nibbles: period, ExtCl period, lock counter
    JMP SEND_F7

; STORE: the edit buffer to the current slot, then the 47 reply
STORE:
    CLR {R('NEWSZ')}
    CLR ${RAM['NEWSZ'] + 1:04X}
    CLR {R('SUM')}
    CLR ${RAM['SUM'] + 1:04X}
    JSR HFREE
    LDX $1206
    LDAB $01,X
    STAB {R('SLOT')}
    LDAA #$03
    LDX ${STATE:04X}
    CPX #${IDLE:04X}
    BNE REPLY                       ; busy: a SysEx frame is being received
    LDAA #$02
    CMPB #100
    BCC REPLY                       ; invalid current slot
    JSR ${SIZE:04X}                 ; X = size needed, D preserved
    STX {R('NEWSZ')}
    LDY #$1A00
    ABY
    ABY
    LDX $00,Y                       ; old handle, 0 = empty slot
    STX {R('OLDH')}
    JSR ${HEAPEND:04X}              ; D = heap end, X preserved
    CPX #$0000
    BEQ W_FIT
    XGDX
    ADDD #$0200                     ; the handle's size entry
    XGDX
    SUBD $00,X                      ; end after freeing the old patch
W_FIT:
    ADDD {R('NEWSZ')}
    CPD #${HEAPTOP:04X}             ; the same test as $F96D
    LDAA #$01
    BCC REPLY                       ; does not fit: nothing changed
    LDX {R('OLDH')}
    BEQ W_SAVE
    LDY #$1A00
    LDAB {R('SLOT')}
    ABY
    ABY
    CLR $00,Y                       ; as the front panel at $AF16-$AF27
    CLR $01,Y
    JSR ${FREEH:04X}
W_SAVE:
    LDAB {R('SLOT')}
    JSR ${SAVE:04X}
    LDAA #$01
    CPX #$0000
    BEQ REPLY                       ; cannot happen after the check above
    CLR ${REDRAW:04X}
    LDD #$0000                      ; checksum over the stored bytes
    LDY {R('NEWSZ')}
W_SUM:
    ADDB $00,X
    ADCA #$00
    INX
    DEY
    BNE W_SUM
    STD {R('SUM')}
    JSR HFREE
    CLRA
REPLY:
    PSHA
    JSR ${HEADER:04X}               ; clobbers A
    LDAA #$47
    JSR ${TX:04X}
    PULA
    JSR ${TXNIB:04X}                ; status
    LDX #{R('SLOT')}
    LDAB #7
    JSR ${0x84E6:04X}               ; slot, size, checksum, free bytes
SEND_F7:
    LDAA #$F7
    JMP ${TX:04X}

; HFREE: FREEB = $7800 - heap end
HFREE:
    JSR ${HEAPEND:04X}
    STD {R('HEND')}
    LDD #${HEAPTOP:04X}
    SUBD {R('HEND')}
    STD {R('FREEB')}
    RTS
"""

CODE, L = assemble(SRC, ORG)
def typed(text, last):
    """Script bytes that print text, waiting 0 ticks after each character and last ticks after the last one."""
    return bytes([x for ch in text for x in (ch, 0)][:-1]) + bytes([last])

INTRO = (0xE223,
         bytes.fromhex('0008') + bytes.fromhex('4f00530031012e01310030002000520033003308') + b'\xc6'
         + bytes([x for ch in b'(C) 2000' for x in (ch, 0)]) + bytes.fromhex('0014'),
         bytes.fromhex('4f00530031012e0131003200520030003108') + b'\xc5' + typed(b'norrby/2026', 0x14 + 0x08))
assert len(INTRO[1]) == len(INTRO[2])
HOOKS = [  # address, original, new
    (0x808E, bytes.fromhex('bd8114'), bytes([0xbd, L['INIT'] >> 8, L['INIT'] & 0xff])),
    (0x82B1, bytes.fromhex('cc81f8'), bytes([0x7e, L['DISP'] >> 8, L['DISP'] & 0xff])),
    (0xA1EB, bytes.fromhex('bda205'), bytes([0xbd, L['TICK'] >> 8, L['TICK'] & 0xff])),
]

def build(orig):
    recs, bad = decode(orig)
    assert bad == 0, 'checksum error in the original'
    img, cov = image(recs)
    assert FREE[0] <= ORG and ORG + len(CODE) <= LIMIT <= FREE[1] + 1, 'the code does not fit below $%04X' % LIMIT
    assert not any(cov[ORG:ORG + len(CODE)]), 'the new code lands on addresses present in the file'
    for a, old, new in HOOKS + [INTRO]:
        assert bytes(img[a:a + len(old)]) == old, 'unexpected original bytes at %04X' % a
        assert all(cov[a:a + len(old)]) and max(cov[a:a + len(old)]) == 1
    out = []
    for r in recs:
        if isinstance(r, bytes): out.append(r); continue
        a, bs = r
        bs = list(bs)
        for h, old, new in HOOKS + [INTRO]:
            for j in range(len(old)):
                if a <= h + j < a + len(bs): bs[h + j - a] = new[j]
        out.append((a, bs))
        if a + len(bs) == 0xB0E5:    # new frames in address order, after the last frame before the gap
            for k in range(0, len(CODE), 32): out.append((ORG + k, list(CODE[k:k + 32])))
    assert sum(1 for r in out if not isinstance(r, bytes) and r[0] == ORG) == 1
    assert out[-1] == bytes.fromhex('f000450169f7'), 'the end frame must be last'
    return encode(out)

def check(orig, new):
    """Decodes the new file and requires it to differ from the original exactly where intended."""
    r0, b0 = decode(orig); r1, b1 = decode(new)
    assert b0 == 0 and b1 == 0
    i0, c0 = image(r0); i1, c1 = image(r1)
    diff = [a for a in range(0x8000, 0x10000) if i0[a] != i1[a] or bool(c0[a]) != bool(c1[a])]
    want = set(range(ORG, ORG + len(CODE)))
    for a, old, nb in HOOKS + [INTRO]: want |= {a + j for j in range(len(old)) if old[j] != nb[j]}
    assert set(diff) == want, 'unexpected differences: %s' % sorted(set(diff) ^ want)
    assert bytes(i1[ORG:ORG + len(CODE)]) == CODE
    for a, old, nb in HOOKS + [INTRO]: assert bytes(i1[a:a + len(nb)]) == nb
    assert bytes(i1[0xFD5D:0xFEBD]) == bytes(i0[0xFD5D:0xFEBD]), 'the flash routine must be untouched'
    assert len([r for r in r1 if isinstance(r, bytes)]) == 1 and new.endswith(bytes.fromhex('f000450169f7'))
    assert all(x < 0x80 for f in new.split(b'\xf7')[:-1] for x in f[1:]), 'data byte >= 80 in a frame'
    return sorted(diff)

def listing():
    """The new code as the independent disassembler hc11.py reads it."""
    img = bytearray(0x10000); img[ORG:ORG + len(CODE)] = CODE
    return disassemble(img, ORG, ORG + len(CODE))

def crosscheck():
    """hc11.py must see the same instructions as the assembler; returns the disagreements."""
    mine = []
    for raw in SRC.splitlines():
        t = raw.split(';')[0].strip()
        if t and not t.endswith(':'): mine.append(t.split(' ')[0])
    theirs = []
    for l in listing():
        rest = [x for x in l.split()[1:] if not all(ch in '0123456789abcdef' for ch in x)]   # skip the opcode bytes
        theirs.append(rest[0] if rest else '?')
    if len(mine) != len(theirs): return [('count', len(mine), len(theirs))]
    return [(i, a, b) for i, (a, b) in enumerate(zip(mine, theirs)) if a != b]
