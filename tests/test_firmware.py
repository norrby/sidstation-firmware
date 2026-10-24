# Run: python3 -m unittest discover tests
# Without Elektron's files only the parts that do not need them run. With SIDOS_R33=<dir containing sidos_v33_1.syx and
# sidos_v33_2.syx> the whole build runs as well, self-test included.
import os, sys, hashlib, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import osdecode, sysops, check

R33 = os.environ.get('SIDOS_R33')

class Assembler(unittest.TestCase):
    def test_disassembler_agrees(self):
        self.assertEqual(sysops.crosscheck(), [])

    def test_code_fits(self):
        self.assertLessEqual(sysops.ORG + len(sysops.CODE), sysops.LIMIT)

    def test_hooks_replace_equal_lengths(self):
        for a, old, new in sysops.HOOKS + [sysops.INTRO]: self.assertEqual(len(old), len(new), '%04X' % a)

class Format(unittest.TestCase):
    RECS = [(0x8000, list(range(32))), (0x8020, [0xFF] * 32), (0xFFFE, [0x80, 0x00]), osdecode.END]

    def test_round_trip(self):
        d = osdecode.encode(self.RECS)
        recs, bad = osdecode.decode(d)
        self.assertEqual((recs, bad), (self.RECS, 0))
        self.assertTrue(all(x < 0x80 for f in osdecode.frames(d) for x in f[1:-1]))

    def test_checksum(self):
        d = bytearray(osdecode.encode(self.RECS)); d[12] ^= 1
        self.assertEqual(osdecode.decode(bytes(d))[1], 1)

    def test_check_frames(self):
        check.check_frames(osdecode.encode(self.RECS))
        for recs, msg in ((self.RECS[:-1], 'end frame'), (self.RECS + [osdecode.END], 'end frame'),
                          ([(0x7FF0, [1])] + self.RECS, 'outside'), ([(0x8010, [1])] + self.RECS, 'twice'),
                          ([(0xFFFE, [0x90, 0x00]), osdecode.END], 'reset vector')):
            with self.assertRaisesRegex(check.CheckError, msg): check.check_frames(osdecode.encode(recs))

    def test_unknown_file_refused(self):
        with self.assertRaisesRegex(check.CheckError, 'unknown file'): check.check(osdecode.encode(self.RECS))

@unittest.skipUnless(R33, 'set SIDOS_R33 to a directory with sidos_v33_1.syx and sidos_v33_2.syx')
class Build(unittest.TestCase):
    def test_build(self):
        import build
        with tempfile.TemporaryDirectory() as out:
            build.main([os.path.join(R33, 'sidos_v33_1.syx'), os.path.join(R33, 'sidos_v33_2.syx'), out])
            d = open(os.path.join(out, 'sidos_1.12_v01_2.syx'), 'rb').read()
            self.assertEqual(hashlib.sha256(d).hexdigest(), check.V01_2)
            self.assertEqual(open(os.path.join(out, 'sidos_1.12_v01_1.syx'), 'rb').read(), osdecode.START)

if __name__ == '__main__':
    unittest.main()
