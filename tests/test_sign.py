import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import sign  # noqa: E402
from tests.helpers import keypair, tmpdir  # noqa: E402


@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class Sign(unittest.TestCase):
    def test_roundtrip_and_tamper(self):
        d = tmpdir(self)
        key, allowed = keypair(d)
        v = {"kind": "handoff", "run_id": "RUN-PAY-1-1", "sha": "a" * 40, "note": "中文也行"}
        s = sign.sign(v, key)
        self.assertTrue(sign.verify(v, s, allowed))
        self.assertFalse(sign.verify(dict(v, sha="b" * 40), s, allowed))
        self.assertFalse(sign.verify(v, s, allowed, principal="someone-else"))

    def test_other_key_rejected(self):
        d1, d2 = tmpdir(self), tmpdir(self)
        key1, _ = keypair(d1)
        _, allowed2 = keypair(d2)
        v = {"x": 1}
        self.assertFalse(sign.verify(v, sign.sign(v, key1), allowed2))


if __name__ == "__main__":
    unittest.main()
