import unittest

from fastapi.testclient import TestClient

import web


class TestWebSecurity(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_token = web.PANEL_TOKEN
        self._orig_local_only = web.PANEL_LOCAL_ONLY
        web.PANEL_LOCAL_ONLY = False
        self.client = TestClient(web.app)

    def tearDown(self) -> None:
        web.PANEL_TOKEN = self._orig_token
        web.PANEL_LOCAL_ONLY = self._orig_local_only

    def test_write_operation_requires_configured_panel_token(self) -> None:
        web.PANEL_TOKEN = ""

        resp = self.client.post(
            "/api/scalp/refresh",
            headers={"host": "127.0.0.1", "origin": "http://127.0.0.1"},
        )

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["error"], "panel_token_required")

    def test_write_operation_rejects_cross_origin_even_with_token(self) -> None:
        web.PANEL_TOKEN = "secret"

        resp = self.client.post(
            "/api/scalp/refresh",
            headers={
                "host": "127.0.0.1",
                "origin": "http://evil.local",
                web.PANEL_TOKEN_HEADER: "secret",
            },
        )

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"], "csrf_check_failed")

    def test_write_operation_accepts_custom_header_token_without_browser_origin(self) -> None:
        web.PANEL_TOKEN = "secret"

        resp = self.client.post(
            "/api/scalp/refresh",
            headers={"host": "127.0.0.1", web.PANEL_TOKEN_HEADER: "secret"},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["status"], "error")


if __name__ == "__main__":
    unittest.main()
