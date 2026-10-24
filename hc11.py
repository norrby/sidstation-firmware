# hc11.py – a minimal 68HC11 disassembler (the common opcodes), for reading the SidStation OS. Read-only.
# Independent of the assembler in sysops.py; the build uses it to cross-check the new code.
# Usage: python3 hc11.py <image.bin> <start hex> <end hex>   (image: a 64K file, e.g. from osdecode.py)
import sys
INH={0x01:'NOP',0x02:'IDIV',0x03:'FDIV',0x04:'LSRD',0x05:'ASLD',0x06:'TAP',0x07:'TPA',0x08:'INX',0x09:'DEX',0x0a:'CLV',0x0b:'SEV',0x0c:'CLC',0x0d:'SEC',0x0e:'CLI',0x0f:'SEI',0x10:'SBA',0x11:'CBA',0x16:'TAB',0x17:'TBA',0x19:'DAA',0x1b:'ABA',0x30:'TSX',0x31:'INS',0x32:'PULA',0x33:'PULB',0x34:'DES',0x35:'TXS',0x36:'PSHA',0x37:'PSHB',0x38:'PULX',0x39:'RTS',0x3a:'ABX',0x3b:'RTI',0x3c:'PSHX',0x3d:'MUL',0x3e:'WAI',0x3f:'SWI',0x8f:'XGDX',0xcf:'STOP',
0x40:'NEGA',0x43:'COMA',0x44:'LSRA',0x46:'RORA',0x47:'ASRA',0x48:'ASLA',0x49:'ROLA',0x4a:'DECA',0x4c:'INCA',0x4d:'TSTA',0x4f:'CLRA',
0x50:'NEGB',0x53:'COMB',0x54:'LSRB',0x56:'RORB',0x57:'ASRB',0x58:'ASLB',0x59:'ROLB',0x5a:'DECB',0x5c:'INCB',0x5d:'TSTB',0x5f:'CLRB'}
BR={0x20:'BRA',0x21:'BRN',0x22:'BHI',0x23:'BLS',0x24:'BCC',0x25:'BCS',0x26:'BNE',0x27:'BEQ',0x28:'BVC',0x29:'BVS',0x2a:'BPL',0x2b:'BMI',0x2c:'BGE',0x2d:'BLT',0x2e:'BGT',0x2f:'BLE',0x8d:'BSR'}
MEM1={0x60:'NEG',0x63:'COM',0x64:'LSR',0x66:'ROR',0x67:'ASR',0x68:'ASL',0x69:'ROL',0x6a:'DEC',0x6c:'INC',0x6d:'TST',0x6e:'JMP',0x6f:'CLR'}
ACC={0x0:'SUB',0x1:'CMP',0x2:'SBC',0x4:'AND',0x5:'BIT',0x6:'LDA',0x7:'STA',0x8:'EOR',0x9:'ADC',0xa:'ORA',0xb:'ADD'}

def disassemble(b, pc, end):
    """Lines "addr  bytes  mnemonic" for b[pc:end]."""
    out = []
    def dis(pc,pre=None):
        op=b[pc]
        if op in (0x18,0x1a,0xcd) and pre is None:
            return dis(pc+1,op)
        def w(a): return (b[a]<<8)|b[a+1]
        idx={None:'X',0x18:'Y',0x1a:'X',0xcd:'X'}[pre]
        if op in (0x08,0x09,0x30,0x35,0x38,0x3a,0x3c,0x8f) and pre==0x18:
            m=INH[op].replace('X','Y'); return pc+1,m
        if op in INH: return pc+1,INH[op]
        if op in BR: off=b[pc+1]; off=off-256 if off>127 else off; return pc+2,'%s $%04X'%(BR[op],pc+2+off)
        if op in (0x12,0x13,0x1e,0x1f):  # BRSET/BRCLR dir/ind
            n={0x12:'BRSET',0x13:'BRCLR',0x1e:'BRSET',0x1f:'BRCLR'}[op]; off=b[pc+3]; off=off-256 if off>127 else off
            a='$%02X'%b[pc+1] if op<0x1e else '$%02X,%s'%(b[pc+1],idx)
            return pc+4,'%s %s #$%02X $%04X'%(n,a,b[pc+2],pc+4+off)
        if op in (0x14,0x15,0x1c,0x1d):
            n={0x14:'BSET',0x15:'BCLR',0x1c:'BSET',0x1d:'BCLR'}[op]
            a='$%02X'%b[pc+1] if op<0x1c else '$%02X,%s'%(b[pc+1],idx)
            return pc+3,'%s %s #$%02X'%(n,a,b[pc+2])
        if 0x60<=op<=0x7f and (op&0xf) in [k&0xf for k in MEM1]:
            n=MEM1[0x60|(op&0xf)]
            return (pc+2,'%s $%02X,%s'%(n,b[pc+1],idx)) if op<0x70 else (pc+3,'%s $%04X'%(n,w(pc+1)))
        if op>=0x80:
            lo=op&0xf; mode=(op>>4)&3; reg='A' if op<0xc0 else 'B'
            name=None; wide=False
            if lo in ACC: name=ACC[lo]+reg
            elif lo==0x3: name='SUBD' if op<0xc0 else 'ADDD'; wide=True
            elif lo==0xc: name=('CPX' if pre not in (0x18,0x1a) else ('CPY' if pre==0x18 else 'CPD')) if op<0xc0 else 'LDD'; wide=True
            elif lo==0xd: name='JSR' if op<0xc0 else 'STD'; wide=True
            elif lo==0xe: name=('LDS' if op<0xc0 else ('LDX' if pre!=0x18 else 'LDY')); wide=True
            elif lo==0xf: name=('STS' if op<0xc0 else ('STX' if pre!=0x18 else 'STY')); wide=True
            if pre==0xcd and lo==0xc and op<0xc0: name='CPY'
            if pre==0x1a and lo==0x3 and op<0xc0: name='CPD'
            if name:
                if mode==0: return (pc+3,'%s #$%04X'%(name,w(pc+1))) if wide else (pc+2,'%s #$%02X'%(name,b[pc+1]))
                if mode==1: return pc+2,'%s $%02X'%(name,b[pc+1])
                if mode==2: return pc+2,'%s $%02X,%s'%(name,b[pc+1],'Y' if pre in(0x18,0xcd) else 'X')
                return pc+3,'%s $%04X'%(name,w(pc+1))
        return pc+1,'db $%02X'%op
    while pc < end:
        n, t = dis(pc); out.append('%04X  %-12s %s' % (pc, b[pc:n].hex(' '), t)); pc = n
    return out

if __name__ == '__main__':
    if len(sys.argv) != 4: raise SystemExit('usage: python3 hc11.py <image.bin> <start hex> <end hex>')
    print('\n'.join(disassemble(open(sys.argv[1], 'rb').read(), int(sys.argv[2], 16), int(sys.argv[3], 16))))
