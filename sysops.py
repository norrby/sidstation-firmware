# sysops.py – the patch: three new SysEx operations for SidStation OS 1.10 R33, and a new boot intro.
# Builds the new OS file from Elektron's original; everything else stays byte for byte as in R33 (checked).
#
#   05 dump request      F0 00 20 3C 01 00 05 <slot> F7   slot 0..99 or 7F = current. Reply: the menu's Patch dump ($8457),
#                                                         an 02 frame, or an 03 frame for an empty slot
#   06 settings request  F0 00 20 3C 01 00 06 F7          Reply: F0 00 20 3C 01 00 46 <15 values> F7
#   07 store             F0 00 20 3C 01 00 07 7F F7       stores the edit buffer to the current slot.
#                                                         Reply: F0 00 20 3C 01 00 47 <8 values> F7
#   08 version request   F0 00 20 3C 01 00 08 F7          Reply: F0 00 20 3C 01 00 48 <count> <count values> F7 (R02)
# The replies are described in docs/protocol.md.
#
# The store does what the front panel's Store does ($AF0D-$AF2A: free the old patch, $FA08, then $99A6), except that it
# first checks that the new patch fits (the front panel frees first, so a full memory loses the old patch).
#
# Changes to the original:
#   $808E  JSR $8114 -> JSR INIT    init: no pending requests before the SysEx state is set
#   $82B1  LDD #$81F8 -> JMP DISP   the ID dispatch: an unknown id (not 01..04) is tested against 05..08
#   $A1EB  JSR $A205 -> JSR TICK    the main loop: pending requests are served before the wait for the next tick
# Fixes of bugs in R33 (R02):
#   $8232  LDX #$823B -> JMP MIDIERR  a status byte before a channel message was complete restarted the synth
#   $82B8  LDD #$0000 -> JMP CLEAR01  the 01 clear: refused while the main loop works in the patch heap
#   $83D1  JSR $F96D -> JSR IALLOC    the 02 receiver's allocation: refused while the main loop works in the heap
#   $831C  LDAB $121F -> JMP DPDATA   a status byte 80-F6 inside a DP frame (04) was taken as data: any address
#   $83F6, $8418  LDX $1217 -> JMP RAWW, NIBW   the 02 receiver wrote past the patch it allocated
#   $8D6D  JSR $CCC0 -> JSR DCSAFE    CC 16-19 with a direct control target of 83 or more jumped to $0000
#   $AEBD, $AF2B  JSR $99A6 -> JSR LSAVE   the front panel's save, with BUSY set
#   $AF27, $AFE2  JSR $FA08 -> JSR LFREE   the front panel's free (Store, Delete), with BUSY set, after a running 02
# New code at $B300, below $B600 (on an E-series HC11 $B600-$B7FF can be internal EEPROM).
# Data: the boot intro shows "OS 1.12 R02" / "norrby/2026" instead of "OS1.10 R33" / "(C) 2000". 1.12 because Elektron's
# last OS is 1.11 R34. The second line needs 4 more bytes than the original; they come from the pauses before line 1
# (00 08) and after line 2 (00 14), folded into the wait of the last character; the two extra characters cost one tick
# each (140 ticks, limit 142). The two spaces in line 1 are cursor moves (1 byte, no tick) instead of the space after
# OS1.10; the byte that leaves comes from the pause after "SidStation" (00 18), folded into the wait of its "n". The intro is a script at $E1F6-$E24E read by $E155: 80-FF = cursor, 00 d = pause,
# 01 = wipe, c d = print c then wait d ticks; nothing else reads these bytes.
#
# The file contains only the new bytes and where they go. The original bytes it lists (the hooks, the intro) are there
# to be checked before anything is replaced.
from osdecode import decode, encode, image
from hc11 import disassemble

ORG, LIMIT = 0xB300, 0xB600
FREE = (0xB0E5, 0xB7FF)
RAM = {'REQ': 0x13E0, 'TMP': 0x13E1, 'OP': 0x13E2, 'RQS': 0x13E3, 'RQW': 0x13E4,
       'SLOT': 0x13E5, 'NEWSZ': 0x13E6, 'SUM': 0x13E8, 'FREEB': 0x13EA,     # in reply order (sent as one run)
       'OLDH': 0x13EC, 'HEND': 0x13EE, 'PER': 0x13F0,                       # PER: 5 bytes
       'BUSY': 0x13F5, 'ENDP': 0x13F6, 'RQV': 0x13F8}                         # ENDP: 2 bytes
VERSION = (1, 12, 2)   # OS 1.12 R02, the 48 reply
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
ALLOC = 0xF96D         # allocates D bytes: X = handle, or 0 (Z set) if they do not fit
RX02 = 0x838B          # the 02 receiver's state, from the id byte to F7
DROP = 0x82FE          # LED off, redraw, idle state, RTI: how R33 ends a frame it does not act on
CLR01 = 0x82BB         # the 01 clear after its LDD #$0000
DCLOOK = 0xCCC0        # looks up target B in the direct control table at $D873, which covers 0..82
DCSKIP = 0x8D7A        # in the CC 16-19 handler, past the call through the table entry
DPNEXT = 0x831F        # the DP receiver after its LDAB $121F
RAWNEXT, RAWSKIP = 0x83FB, 0x83FF   # the 02 receiver's name bytes: after the store, and the count without it
NIBNEXT, NIBSKIP = 0x841D, 0x8421   # the 02 receiver's data bytes: the same

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
    'ADDA': dict(imm='8b'), 'ADCA': dict(imm='89'), 'ADDB': dict(imm='cb', ext='fb', ix='eb'), 'SUBA': dict(imm='80'),
    'SUBB': dict(imm='c0', ext='f0'),
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
    STAA {R('RQV')}
    CLR {R('BUSY')}
    JMP ${SYSEXINIT:04X}

; DISP: from $82B1, A = the id byte after the header, not 01..04
DISP:
    CMPA #$05
    BEQ D_SLOT
    CMPA #$07
    BEQ D_SLOT
    CMPA #$06
    BEQ D_END
    CMPA #$08
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
    CMPB #$08
    BNE E_W
    CLR {R('RQV')}
    BRA E_DONE
E_W:
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
    BNE T_V
    JSR STORE
T_V:
    LDX #{R('RQV')}
    BSR TAKE
    BNE T_END
    JSR OSVER
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

; OSVER: the 48 reply: the number of values that follow, then major, minor, revision. A later version may add
; values after these; a reader takes the count first and skips the values it does not know.
OSVER:
    JSR ${HEADER:04X}
    LDAA #$48
    JSR ${TX:04X}
    LDAA #3
    JSR ${TXNIB:04X}
    LDAA #{VERSION[0]}
    JSR ${TXNIB:04X}
    LDAA #{VERSION[1]}
    JSR ${TXNIB:04X}
    LDAA #{VERSION[2]}
    JSR ${TXNIB:04X}
    JMP SEND_F7

; STORE: the edit buffer to the current slot, then the 47 reply. BUSY is set from before the busy test to the reply.
STORE:
    INC {R('BUSY')}
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
    DEC {R('BUSY')}
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

; --- R02: fixes of bugs in R33 ------------------------------------------------------------------------------------
; BUSY counts the main loop's calls into the patch heap. $F96D (allocate), $F9CD (heap end) and $FA08 (free and
; compact) share the scratch bytes $1371-$1379, and $FA08 moves patches with interrupts enabled. R33's 02 receiver
; allocates in the interrupt ($83D1) and its 01 clear wipes the slot and handle tables in the interrupt ($82B8): in
; the middle of a store either left handles pointing outside the heap, and the next compaction overwrote RAM.

; LSAVE: $99A6 for the front panel ($AEBD, $AF2B), with BUSY set; registers as $99A6 leaves them
LSAVE:
    INC {R('BUSY')}
    JSR ${SAVE:04X}
    DEC {R('BUSY')}
    RTS

; LFREE: $FA08 for the front panel ($AF27 Store, $AFE2 Delete), with BUSY set. An 02 frame that allocated before
; BUSY was set still writes into its patch, which $FA08 may move: first wait for that frame to end.
LFREE:
    INC {R('BUSY')}
    BSR WAIT02
    JSR ${FREEH:04X}
    DEC {R('BUSY')}
    RTS

; WAIT02: waits while the 02 receiver is busy, at most about 1.2 s (2 x 65536 turns of 19 cycles); a frame that has
; not ended by then is dropped as R33 drops a broken one. All registers preserved.
WAIT02:
    PSHX
    PSHY
    PSHA
    PSHB
    LDY #2
W2_OUT:
    LDX #$0000
W2_IN:
    LDD ${STATE:04X}
    CPD #${RX02:04X}
    BNE W2_END
    DEX
    BNE W2_IN
    DEY
    BNE W2_OUT
    TPA
    SEI
    LDX ${STATE:04X}                ; again with interrupts off
    CPX #${RX02:04X}
    BNE W2_I
    LDX #${IDLE:04X}
    STX ${STATE:04X}
    JSR ${LEDOFF:04X}                ; preserves A
W2_I:
    TAP
W2_END:
    PULB
    PULA
    PULY
    PULX
    RTS

; IALLOC: from $83D1, the 02 receiver's allocation in the interrupt, X = the size from the frame. It fails as $F96D
; does when the memory is full (X = 0, Z set; the receiver then drops the frame at $8388) while BUSY, or when the size
; is $4000 or more (a status byte among the size bytes). On success ENDP = the end of the new patch, X = its handle.
IALLOC:
    TST {R('BUSY')}
    BNE IA_NO
    CPX #$4000
    BCC IA_NO
    JSR ${ALLOC:04X}
    BEQ IA_RET
    PSHA
    PSHB
    LDD $00,X                       ; the new patch
    ADDD $1371                      ; plus its size, as $F96D left it
    STD {R('ENDP')}                 ; not 0: Z clear
    PULB
    PULA
IA_RET:
    RTS
IA_NO:
    LDX #$0000
    RTS

; RAWW, NIBW: from $83F6 and $8418, the 02 receiver's stores (A = the byte, $1217 = where); bytes past ENDP are
; dropped, so a frame longer than its size no longer overwrites the next patch, or past $7800 the RAM vectors.
RAWW:
    LDX $1217
    CPX {R('ENDP')}
    BCC RW_NO
    STAA $00,X
    JMP ${RAWNEXT:04X}
RW_NO:
    JMP ${RAWSKIP:04X}
NIBW:
    LDX $1217
    CPX {R('ENDP')}
    BCC NW_NO
    STAA $00,X
    JMP ${NIBNEXT:04X}
NW_NO:
    JMP ${NIBSKIP:04X}

; DPDATA: from $831C, a byte 00..F6 in a DP frame (F7 and F8..FF are handled before). R33 stored 80..F6 as data, so a
; status byte in the address made the DP write anywhere in RAM; now it ends the frame and is handled as a status byte.
DPDATA:
    TSTA
    BPL DP_OK
    JMP NOTRT
DP_OK:
    LDAB $121F
    JMP ${DPNEXT:04X}

; CLEAR01: from $82B8, the 01 clear in the interrupt; while BUSY the frame is dropped
CLEAR01:
    TST {R('BUSY')}
    BNE C1_NO
    LDD #$0000
    JMP ${CLR01:04X}
C1_NO:
    JMP ${DROP:04X}

; MIDIERR: from $8232, A = a status byte 80..F7 before a channel message was complete. R33 printed "Jag skiter väl i
; MIDI." and restarted. The incomplete message is taken back out of the ring buffer $1500 (the main loop reads only
; complete messages, counted in $1205): the status byte and the $1213 - $1212 data bytes so far. Then A is handled
; as a new status byte from the idle state, as the MIDI specification asks.
MIDIERR:
    LDAB $1203
    ADDB $1212
    SUBB $1213
    DECB
    STAB $1203
    LDX #${IDLE:04X}
    STX ${STATE:04X}
    JMP ${STATUS:04X}

; DCSAFE: from $8D6D, B = the patch's direct control target for CC 16-19. R33 read past the table for 83..255 and
; jumped through a zero pointer; such a target now does nothing.
DCSAFE:
    CMPB #83
    BCC DC_NO
    JMP ${DCLOOK:04X}
DC_NO:
    INS                             ; this call's return address
    INS
    JMP ${DCSKIP:04X}
"""

CODE, L = assemble(SRC, ORG)
def typed(text, last):
    """Script bytes that print text, waiting 0 ticks after each character and last ticks after the last one."""
    return bytes([x for ch in text for x in (ch, 0)][:-1]) + bytes([last])

INTRO = (0xE21E,
         bytes.fromhex('00001801850008') + bytes.fromhex('4f00530031012e01310030002000520033003308') + b'\xc6'
         + bytes([x for ch in b'(C) 2000' for x in (ch, 0)]) + bytes.fromhex('0014'),
         bytes.fromhex('180185') + bytes.fromhex('4f005300') + b'\x88' + bytes.fromhex('31012e0131003200') + b'\x8d'
         + bytes.fromhex('5200300032') + bytes.fromhex('08') + b'\xc5' + typed(b'norrby/2026', 0x14 + 0x08))
assert len(INTRO[1]) == len(INTRO[2])
HOOKS = [  # address, original, new
    (0x808E, bytes.fromhex('bd8114'), bytes([0xbd, L['INIT'] >> 8, L['INIT'] & 0xff])),
    (0x82B1, bytes.fromhex('cc81f8'), bytes([0x7e, L['DISP'] >> 8, L['DISP'] & 0xff])),
    (0xA1EB, bytes.fromhex('bda205'), bytes([0xbd, L['TICK'] >> 8, L['TICK'] & 0xff])),
]
def jsr(op, label): return bytes([op, L[label] >> 8, L[label] & 0xff])
HOOKS += [  # R02
    (0x8232, bytes.fromhex('ce823b'), jsr(0x7e, 'MIDIERR')),
    (0x82B8, bytes.fromhex('cc0000'), jsr(0x7e, 'CLEAR01')),
    (0x831C, bytes.fromhex('f6121f'), jsr(0x7e, 'DPDATA')),
    (0x83D1, bytes.fromhex('bdf96d'), jsr(0xbd, 'IALLOC')),
    (0x83F6, bytes.fromhex('fe1217'), jsr(0x7e, 'RAWW')),
    (0x8418, bytes.fromhex('fe1217'), jsr(0x7e, 'NIBW')),
    (0x8D6D, bytes.fromhex('bdccc0'), jsr(0xbd, 'DCSAFE')),
    (0xAEBD, bytes.fromhex('bd99a6'), jsr(0xbd, 'LSAVE')),
    (0xAF27, bytes.fromhex('bdfa08'), jsr(0xbd, 'LFREE')),
    (0xAF2B, bytes.fromhex('bd99a6'), jsr(0xbd, 'LSAVE')),
    (0xAFE2, bytes.fromhex('bdfa08'), jsr(0xbd, 'LFREE')),
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
