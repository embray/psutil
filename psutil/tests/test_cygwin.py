#!/usr/bin/env python3

"""Cygwin specific tests."""

import os

from psutil import CYGWIN
from psutil._compat import b
from psutil._compat import PY3
from psutil._compat import unicode
from psutil.tests import PsutilTestCase
from psutil.tests import unittest

try:
    from psutil import _psutil_cygwin as cext
except ImportError:
    pass


@unittest.skipIf(not CYGWIN, "Cygwin only")
class CygwinSpecificTestCase(PsutilTestCase):

    def test_conv_path_a(self, s=unicode, strapi='A'):
        POSIX_TO_WIN = getattr(cext, 'CCP_POSIX_TO_WIN_%s' % strapi)
        WIN_TO_POSIX = getattr(cext, 'CCP_WIN_%s_TO_POSIX' % strapi)

        # string converters for POSIX_TO_WIN conversions, or input to
        # WIN_TO_POSIX; normally the same as s() except if s==b and strapi=='W'
        # then it will be UTF-16 encoded bytes
        def wout(pth):
            if s == b and strapi == 'W':
                pth = pth.encode('utf-16-le')
            else:
                pth = s(pth)
            return pth

        def win(pth):
            if s == b and strapi == 'W':
                pth = pth.encode('utf-16-le')
                # On Python 2 encoding as utf-16 does add second null byte
                # at the end; TBD whether this just needs to be handled in
                # the test, or also in the C code (which assumes when passed
                # a wide-char value it's properly encoded)
                if not PY3:
                    pth += '\0'
            else:
                pth = s(pth)
            return pth

        cyg_root = cext.conv_path(POSIX_TO_WIN, s('/'))
        cygdrive = os.path.dirname(
            cext.conv_path(WIN_TO_POSIX, win('C:\\')).rstrip(s('/')))

        # /cygdrive paths are special and will map directly to the equivalent
        # Windows path (even if it does not actually exist)
        path = cext.conv_path(POSIX_TO_WIN, cygdrive + s('/c/Windows'))
        self.assertEqual(path, wout('C:\\Windows'))
        path = cext.conv_path(POSIX_TO_WIN, cygdrive + s('/q/DOES_NOT_EXIST'))
        self.assertEqual(path, wout('Q:\\DOES_NOT_EXIST'))

        # If the path is relative to the Cygwin root directory it will be
        # returned relative to /
        path = cext.conv_path(WIN_TO_POSIX, cyg_root)
        self.assertEqual(path, s('/'))
        path = cext.conv_path(WIN_TO_POSIX, cyg_root + win('\\usr'))
        self.assertEqual(path, s('/usr'))
        path = cext.conv_path(WIN_TO_POSIX,
                              cyg_root + win('\\does_not_exist'))
        self.assertEqual(path, s('/does_not_exist'))

        # However, if not relative to to cyg_root it will return a path
        # with the appropriate /cygdrive prefix
        path = cext.conv_path(WIN_TO_POSIX, win('C:\\'))
        self.assertEqual(path, os.path.join(cygdrive, s('c/')))
        path = cext.conv_path(WIN_TO_POSIX, win('Q:\\DOES_NOT_EXIST'))
        self.assertEqual(path,
                         os.path.join(cygdrive, s('q'), s('DOES_NOT_EXIST')))

    def test_conv_path_w(self):
        self.test_conv_path_a(strapi='W')

    def test_conv_path_a_bytes(self):
        self.test_conv_path_a(s=b)

    def test_conv_path_w_bytes(self):
        self.test_conv_path_a(s=b, strapi='W')


if __name__ == '__main__':
    from psutil.tests.runner import run_from_name
    run_from_name(__file__)
