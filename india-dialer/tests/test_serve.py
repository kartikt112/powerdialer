"""Who may reach the server: loopback always, the phone page on the LAN
only with --lan and the token, and never the cockpit or the exports."""
import unittest

import _base  # noqa: F401  (puts the app on the path)
import serve


class FakeHeaders(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


def request(host, path, method="GET", cookie=""):
    h = serve.Handler.__new__(serve.Handler)
    h.client_address = (host, 50000)
    h.command = method
    h.path = path
    h.headers = FakeHeaders({"Cookie": cookie} if cookie else {})
    return h._allowed()


class Access(unittest.TestCase):
    def setUp(self):
        self._lan = dict(serve.LAN)
        serve.LAN.update(on=True, token="t0ken")

    def tearDown(self):
        serve.LAN.clear()
        serve.LAN.update(self._lan)

    def test_loopback_reaches_everything(self):
        for path in ("/", "/api/queue", "/api/export/calls.csv", "/m"):
            self.assertTrue(request("127.0.0.1", path), path)
        self.assertTrue(request("127.0.0.1", "/api/call", "POST"))

    def test_lan_needs_the_token(self):
        self.assertFalse(request("192.168.1.20", "/m"))
        self.assertFalse(request("192.168.1.20", "/m?k=wrong"))
        self.assertTrue(request("192.168.1.20", "/m?k=t0ken"))
        self.assertTrue(request("192.168.1.20", "/api/m/current", cookie="k=t0ken"))

    def test_lan_never_reaches_the_cockpit(self):
        for path in ("/?k=t0ken", "/api/queue?k=t0ken", "/api/lead?id=1&k=t0ken", "/api/export/tracker.csv?k=t0ken",
                     "/static/js/app.js?k=t0ken"):
            self.assertFalse(request("192.168.1.20", path), path)
        self.assertFalse(request("192.168.1.20", "/api/call?k=t0ken", "POST"))

    def test_lan_off_means_loopback_only(self):
        serve.LAN["on"] = False
        self.assertFalse(request("192.168.1.20", "/m?k=t0ken"))
