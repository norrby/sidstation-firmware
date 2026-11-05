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
# Three channels (R03): byte 23 of a patch (PATCH_MODE2, $17 in the edit buffer, unused before) gives osc 2 and osc 3 their own
# MIDI channels, counted from the base channel: low nibble osc 2, high nibble osc 3, mod 16; osc 1 stays on the base channel.
# 0 and 0, as every older patch has, is R02 exactly. A voice plays the note on and note off of its channel as in Single mode
# (its own sound, delay and Active); everything else stays on the base channel, and Poly counts as off.
#   $81A4  LDX $1206 -> JMP ICH       the MIDI interrupt's channel test: note on/off also on osc 2's and osc 3's channels
#   $8D0A, $8D0D  JMP $8E34/$8E72 -> JMP N3OFF/N3ON   the consumer's note off/on: per voice with three channels
#   $8B11, $8B2A, $8B60, $8BE7  BRSET $16,Y #$08 -> JSR POLYQ + BCS   Poly as the engine sees it: off with three channels
#   $8F13  BRSET $16,X #$08 + BRCLR $2D,X #$D0 -> JMP Q8F13            the same in the event dispatch
#   $8DA9  LDX $1220 -> JSR V3CLR     CC 123 (all notes off) also clears the voices' held notes
# New code from $B0E5 (the gap R33 leaves), below $B600 (on an E-series HC11 $B600-$B7FF can be internal EEPROM).
# Data: the boot intro shows "OS 1.12 R03" / "norrby/2026" instead of "OS1.10 R33" / "(C) 2000". 1.12 because Elektron's
# last OS is 1.11 R34. The second line needs 4 more bytes than the original; they come from the pauses before line 1
# (00 08) and after line 2 (00 14), folded into the wait of the last character; the two extra characters cost one tick
# each (140 ticks, limit 142). The two spaces in line 1 are cursor moves (1 byte, no tick) instead of the space after
# OS1.10; the byte that leaves comes from the pause after "SidStation" (00 18), folded into the wait of its "n". The intro is a script at $E1F6-$E24E read by $E155: 80-FF = cursor, 00 d = pause,
# 01 = wipe, c d = print c then wait d ticks; nothing else reads these bytes.
#
# The file contains only the new bytes and where they go. The original bytes it lists (the hooks, the intro) are there
# to be checked before anything is replaced.
import re
from osdecode import decode, encode, image
from hc11 import disassemble

ORG, LIMIT = 0xB0E5, 0xB600
FREE = (0xB0E5, 0xB7FF)
RAM = {'REQ': 0x13E0, 'TMP': 0x13E1, 'OP': 0x13E2, 'RQS': 0x13E3, 'RQW': 0x13E4,
       'SLOT': 0x13E5, 'NEWSZ': 0x13E6, 'SUM': 0x13E8, 'FREEB': 0x13EA,     # in reply order (sent as one run)
       'OLDH': 0x13EC, 'HEND': 0x13EE, 'PER': 0x13F0,                       # PER: 5 bytes
       'BUSY': 0x13F5, 'ENDP': 0x13F6, 'RQV': 0x13F8,                         # ENDP: 2 bytes
       # R03, three channels (RAM no run of the emulator touched, $13F5-$14FF): each voice's held notes (4 per voice, FF = none),
       # then the message being handled and the saved filter envelope
       'V3L': 0x1420, 'V3LE': 0x142C, 'V3K': 0x142C, 'V3ST': 0x142D, 'V3NT': 0x142E, 'V3VL': 0x142F, 'V3D': 0x1430,
       'V3EV': 0x1431, 'V3E': 0x1432, 'V3P': 0x1433, 'V3M': 0x1435, 'V3F': 0x1436, 'V32D': 0x143A}   # V3P 2, V3F 4 bytes
VERSION = (1, 12, 3)   # OS 1.12 R03, the 48 reply
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
    'BITA': dict(imm='85'), 'ANDA': dict(imm='84', ix='a4'), 'ANDB': dict(imm='c4', ix='e4'), 'ORAA': dict(imm='8a', ext='ba'),
    'ADDA': dict(imm='8b'), 'ADCA': dict(imm='89'), 'ADDB': dict(imm='cb', ext='fb', ix='eb'), 'SUBA': dict(imm='80'),
    'SUBB': dict(imm='c0', ext='f0', ix='e0'),
    'CLR': dict(ext='7f', ix='6f', iy='186f'), 'TST': dict(ext='7d', ix='6d', iy='186d'),
    'INC': dict(ext='7c', ix='6c'), 'DEC': dict(ext='7a', ix='6a'),
    'JSR': dict(ext='bd', ix='ad'), 'JMP': dict(ext='7e', ix='6e'),
    'BSET': dict(ix='1c'), 'BCLR': dict(ix='1d'),
}
INH = {'INX': '08', 'DEX': '09', 'INY': '1808', 'DEY': '1809', 'XGDX': '8f', 'XGDY': '188f', 'ABX': '3a', 'ABY': '183a',
       'CLRA': '4f', 'CLRB': '5f', 'TSTA': '4d', 'TSTB': '5d', 'TAB': '16', 'TBA': '17', 'PSHA': '36', 'PSHB': '37',
       'PULA': '32', 'PULB': '33', 'PSHX': '3c', 'PULX': '38', 'PSHY': '183c', 'PULY': '1838', 'TPA': '07', 'TAP': '06',
       'SEI': '0f', 'CLI': '0e', 'SEC': '0d', 'CLC': '0c', 'RTS': '39', 'RTI': '3b', 'NOP': '01', 'LSRA': '44', 'LSRB': '54', 'ASLA': '48',
       'ASLB': '58', 'ROLA': '49', 'ROLB': '59', 'INCA': '4c', 'INCB': '5c', 'DECA': '4a', 'DECB': '5a', 'ASLD': '05',
       'LSRD': '04', 'IDIV': '02', 'CBA': '11', 'ABA': '1b', 'COMA': '43', 'NEGA': '40', 'TSX': '30', 'TXS': '35', 'MUL': '3d',
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
            elif op in ('BRSET', 'BRCLR'):                      # BRSET $16,X #$08 label (indexed X only)
                a, m, t2 = arg.split(' ')
                off = (num(t2, L) - (pc + 4)) if pas else 0
                assert -128 <= off <= 127, 'branch out of range: ' + t
                code = bytes([0x1e if op == 'BRSET' else 0x1f, num(a.split(',')[0], L), num(m.lstrip('#'), L), off & 0xff])
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
    JSR V3CLR                       ; R03: no voice holds a note
    CLR {R('V3M')}                   ; and the channels are R02's, as $17 = 0 is
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
SRC += re.sub(r"\b(V3[A-Z0-9]+)(\+\d)?\b", lambda m: "$%04X" % (RAM[m.group(1)] + int(m.group(2) or 0)) if m.group(1) in RAM else m.group(0),
              r"""
; --- R03: three channels ----------------------------------------------------------------------------------------------
; PATCH_MODE2 ($17 in the edit buffer): low nibble = how many channels above the base channel osc 2 listens on, high nibble =
; osc 3 (mod 16); osc 1 listens on the base channel. With $17 = 0 every path below hands over to R33's code unchanged.

; ICH: from $81A4, the MIDI interrupt's channel test for 8x, 9x, Bx and Ex, A = B = the status byte. The base channel passes as
; before; note on and off also pass on osc 2's and osc 3's channels. Accepted -> $81AD (into the ring buffer), else $81C4.
ICH:
    LDX $1206
    SUBB $00,X
    ANDB #$0F                       ; B = channels above the base channel
    BEQ IC_OK
    PSHA
    ANDA #$E0
    CMPA #$80                       ; 8x or 9x
    BNE IC_NO
    LDX $1220
    LDAA $17,X
    ANDA #$0F
    CBA
    BEQ IC_OK2
    LDAA $17,X
    LSRA
    LSRA
    LSRA
    LSRA
    CBA
    BEQ IC_OK2
IC_NO:
    PULA
    JMP $81C4
IC_OK2:
    PULA
IC_OK:
    JMP $81AD

; POLYQ: C = Poly as the engine sees it: the patch's Poly bit, but off with three channels. A, B, X, Y kept (BRCLR and PULX do
; not touch the flags, TST clears C). Called where R33 tests BRSET $16,Y #$08 (then BCS to the same place); no caller reads
; another flag.
POLYQ:
    PSHX
    LDX $1220
    CLC
    BRCLR $16,X #$08 PQ_X
    TST $17,X
    BNE PQ_X
    SEC
PQ_X:
    PULX
    RTS

; Q8F13: from $8F13 in the event dispatch $8F0D, X = the edit buffer, A = $2D (not 0)
Q8F13:
    JSR POLYQ
    BCC Q_S
    JMP $8F4A
Q_S:
    BITA #$D0                       ; as BRCLR $2D,X #$D0 $8F12
    BNE Q_GO
    RTS
Q_GO:
    JMP $8F1B

; V3CLR: no voice holds a note. Ends with X = the edit buffer, as the LDX $1220 it replaces at $8DA9 (CC 123).
V3CLR:
    LDX #V3L
    LDAA #$FF
VC_L:
    STAA $00,X
    INX
    CPX #V3LE
    BNE VC_L
    LDX $1220
    RTS

; N3OFF, N3ON: from the consumer's dispatch ($8D0A, $8D0D), X = the status byte in the ring buffer ($15xx); A and B are dead
; (both R33 handlers start with XGDX). Without three channels on to R33's note off ($8E34) and note on ($8E72).
N3OFF:
    BSR N3Q
    JMP $8E34
N3ON:
    BSR N3Q
    JMP $8E72

; N3Q: returns when $17 = 0 and was 0 at the last note (R02's path), else does not return. When $17 changed since the last note
; (a program change, a DP frame), the message goes back to the ring buffer and CC 123's code runs first ($8DA9): every held note
; is forgotten, R33's and the voices', and all three oscillators get released. Then the message is read again, on the new channels.
N3Q:
    PSHX
    LDX $1220
    LDAA $17,X
    PULX
    CMPA V3M
    BNE NQ_NEW
    TSTA
    BNE NQ_3
    RTS
NQ_NEW:
    STAA V3M
    INC $1205                       ; the message is unread again (count only; $1204 was not moved)
    INS
    INS
    JMP $8DA9
NQ_3:
    INS
    INS

; N3: the message (status, note, velocity) to every voice whose channel it is on. Afterwards $2D gets $80 after a note on and
; $08 after a note off, as R33's handlers leave it for the LFOs ($9181), and a note on ends the tick as R33's does ($8EF4), so
; the dispatch at $8CCC never sees the $80.
N3:
    LDAA $00,X
    STAA V3ST
    BSR NEXTB
    LDAA $00,X
    STAA V3NT
    BSR NEXTB
    LDAA $00,X
    STAA V3VL
    BSR NEXTB
    XGDX
    STAB $1204                      ; the message is read
    LDX $1220
    LDAA $2D,X
    STAA V32D
    LDX $1206
    LDAB V3ST
    SUBB $00,X
    ANDB #$0F
    STAB V3D                        ; channels above the base channel
    CLR V3EV
    CLR V3K
N3_V:
    LDX $1220
    LDAA $17,X
    LDAB V3K
    BEQ N3_C0
    CMPB #$01
    BNE N3_3
    ANDA #$0F
    BRA N3_C
N3_3:
    LSRA
    LSRA
    LSRA
    LSRA
    BRA N3_C
N3_C0:
    CLRA
N3_C:
    CMPA V3D
    BNE N3_NX
    JSR VOICE
N3_NX:
    INC V3K
    LDAA V3K
    CMPA #$03
    BNE N3_V
    LDX $1220
    LDAA V32D
    ORAA V3EV
    STAA $2D,X
    LDAA V3EV
    BITA #$C0
    BNE N3_END                      ; a note on (new or legato) ends the tick, as R33's ($8EF4)
    JMP $8CC5                       ; next message
N3_END:
    RTS
NEXTB:
    XGDX
    INCB                            ; within the ring buffer's page
    XGDX
    RTS

; VOICE: the message for voice V3K: its held notes (4) at V3L + 4 * V3K.
VOICE:
    LDAB V3K
    ASLB
    ASLB
    LDX #V3L
    ABX
    STX V3P
    LDAA V3ST
    BITA #$10                       ; 9x
    BEQ V_OFF
    TST V3VL
    BEQ V_OFF
    LDX $1228
    LDAA V3VL
    STAA $02,X                      ; the velocity, as R33 ($8E80)
    JSR TOTAL
    TSTB
    BNE V_ON1
    JSR $8840                       ; the first key held: LED, as R33 ($8E91)
V_ON1:
    LDX V3P
    JSR COUNT4
    LDAA #$80                       ; no key held on this voice: a new note
    TSTB
    BEQ V_ON2
    LDX $1220
    BRSET $19,X #$40 V_RE           ; Force NoteOn ReInit: a new note even so
    LDAA #$40                       ; legato
    BRA V_ON2
V_RE:
    LDAB #$FE                       ; and the gates drop for a tick, as R33 ($8E9D); $123D is all voices', as in R33
    STAB $123D
V_ON2:
    STAA V3E
    LDX V3P                         ; remember the note: held already, or a free place (a fifth note plays, unremembered)
    LDAB #$04
V_F:
    LDAA $00,X
    CMPA V3NT
    BEQ V_GO
    INX
    DECB
    BNE V_F
    LDX V3P
    LDAB #$04
V_E:
    TST $00,X
    BMI V_PUT
    INX
    DECB
    BNE V_E
    BRA V_GO
V_PUT:
    LDAA V3NT
    STAA $00,X
V_GO:
    LDAA V3EV
    ORAA V3E                        ; $80 or $40: a legato note does not restart the LFOs
    STAA V3EV
    JMP V_SEND
V_OFF:
    LDX V3P
    LDAB #$04
V_O:
    LDAA $00,X
    CMPA V3NT
    BEQ V_DEL
    INX
    DECB
    BNE V_O
    RTS                             ; a note this voice does not hold
V_DEL:
    LDAA #$FF
    STAA $00,X
    LDAA V3EV
    ORAA #$08
    STAA V3EV
    LDX V3P
    JSR COUNT4
    TSTB
    BNE V_RTS                       ; other keys still held on this voice: nothing, as R33
    JSR TOTAL
    TSTB
    BNE V_SAVE
    JSR $8849                       ; the last key: LED, as R33 ($8E65)
V_SAVE:
    LDX $1220                       ; the filter envelope, kept if another voice still holds a key
    LDAA $2E,X
    STAA V3F
    LDAA $2F,X
    STAA V3F+1
    LDD $32,X
    STD V3F+2
    LDAA #$10                       ; released
    STAA V3E
    JSR V_SEND
    JSR TOTAL
    TSTB
    BEQ V_RTS
    LDX $1220
    LDAA V3F
    STAA $2E,X
    LDAA V3F+1
    STAA $2F,X
    LDD V3F+2
    STD $32,X
V_RTS:
    RTS

; V_SEND: event V3E ($80 note, $40 legato, $10 released) for voice V3K through R33's single-mode routine $8BE7: the note in $46,
; the event in $2D for the call, $120E = the voice's patch part, X = its oscillator. $2D as it was afterwards.
V_SEND:
    LDX $1220
    LDAA V3NT
    STAA $46,X
    LDAA V3E
    STAA $2D,X
    LDAB V3K
    LDAA #$15
    MUL
    ADDD #$0047
    ADDD $1220
    STD $120E
    LDAB V3K
    LDAA #$29
    MUL
    ADDD $1222
    XGDX
    LDY $1220
    JSR $8BE7
    LDX $1220
    LDAA V32D
    STAA $2D,X
    RTS

; TOTAL: B = the notes all voices hold; COUNT4: B = the notes held at X (4)
TOTAL:
    LDX #V3L
    LDAA #$0C
    BRA CNT
COUNT4:
    LDAA #$04
CNT:
    CLRB
CN_L:
    TST $00,X
    BMI CN_N
    INCB
CN_N:
    INX
    DECA
    BNE CN_L
    RTS
""")

CODE, L = assemble(SRC, ORG)
def typed(text, last):
    """Script bytes that print text, waiting 0 ticks after each character and last ticks after the last one."""
    return bytes([x for ch in text for x in (ch, 0)][:-1]) + bytes([last])

INTRO = (0xE21E,
         bytes.fromhex('00001801850008') + bytes.fromhex('4f00530031012e01310030002000520033003308') + b'\xc6'
         + bytes([x for ch in b'(C) 2000' for x in (ch, 0)]) + bytes.fromhex('0014'),
         bytes.fromhex('180185') + bytes.fromhex('4f005300') + b'\x88' + bytes.fromhex('31012e0131003200') + b'\x8d'
         + bytes.fromhex('5200300033') + bytes.fromhex('08') + b'\xc5' + typed(b'norrby/2026', 0x14 + 0x08))
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
HOOKS += [  # R03: three channels
    (0x81A4, bytes.fromhex('fe1206'), jsr(0x7e, 'ICH')),
    (0x8D0A, bytes.fromhex('7e8e34'), jsr(0x7e, 'N3OFF')),
    (0x8D0D, bytes.fromhex('7e8e72'), jsr(0x7e, 'N3ON')),
    (0x8DA9, bytes.fromhex('fe1220'), jsr(0xbd, 'V3CLR')),
    (0x8F13, bytes.fromhex('1e1608'), jsr(0x7e, 'Q8F13')),
] + [(a, bytes.fromhex('181e1608') + bytes([off]), jsr(0xbd, 'POLYQ') + bytes([0x25, off]))   # BRSET $16,Y #$08 -> JSR POLYQ; BCS
     for a, off in ((0x8B11, 0x09), (0x8B2A, 0x05), (0x8B60, 0x09), (0x8BE7, 0x37))]

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
