"""Shared test setup: a throwaway data dir and the real config."""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)

import common  # noqa: E402

CFG = common.load_config()


class DbCase(unittest.TestCase):
    def setUp(self):
        import db
        self.tmp = tempfile.mkdtemp()
        self._path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp, "india.db")
        db.init()

    def tearDown(self):
        import db
        db.DB_PATH = self._path
        shutil.rmtree(self.tmp, ignore_errors=True)
