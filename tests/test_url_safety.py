import socket
import unittest
from unittest.mock import patch, MagicMock

import requests
from download_limits import fetch_bounded
from url_safety import UnsafeURL, public_destination, safe_get, PinnedAdapter


def answer(ip):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return (family, socket.SOCK_STREAM, 6, "", (ip, 443))


class UrlSafetyTests(unittest.TestCase):
    def test_rejects_private_loopback_and_special_addresses(self):
        for ip in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254",
                   "0.0.0.0", "100.64.0.1", "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1"):
            with self.subTest(ip=ip), patch("url_safety.socket.getaddrinfo", return_value=[answer(ip)]):
                with self.assertRaises(UnsafeURL):
                    public_destination("https://test.example/")

    def test_mixed_public_private_dns_is_rejected(self):
        with patch("url_safety.socket.getaddrinfo", return_value=[answer("93.184.216.34"), answer("127.0.0.1")]):
            with self.assertRaises(UnsafeURL):
                public_destination("https://test.example/")

    def test_invalid_schemes_credentials_and_ports_rejected_before_dns(self):
        for url in ("file:///C:/secret", "ftp://example.com", "https://user:password@example.com",
                    "http://example.com:27017", "http://example.com:0", "http://[invalid", "http://example.com/\n"):
            with self.subTest(url=url), patch("url_safety.socket.getaddrinfo") as dns:
                with self.assertRaises(UnsafeURL):
                    public_destination(url)
                dns.assert_not_called()

    def test_connection_pins_address_and_retains_tls_hostname(self):
        adapter = PinnedAdapter("https", "test.example", 443, "93.184.216.34")
        connection = adapter.pinned_pool._new_conn()
        self.assertEqual(connection.host, "test.example")
        with patch("url_safety.create_connection") as connect:
            connection._new_conn()
        self.assertEqual(connect.call_args.args[0], ("93.184.216.34", 443))
        adapter.close()

    def test_environment_proxy_and_automatic_redirects_disabled(self):
        with patch("url_safety.socket.getaddrinfo", return_value=[answer("93.184.216.34")]), patch("url_safety.requests.Session") as factory:
            session = factory.return_value
            response = safe_get("https://test.example", timeout=(1, 2))
            self.assertFalse(session.trust_env)
            session.get.assert_called_once_with("https://test.example", timeout=(1, 2), stream=True, allow_redirects=False)
            response.close()
            session.close.assert_called_once()

    def test_redirect_to_private_host_is_revalidated(self):
        first = MagicMock(status_code=302, headers={"Location": "http://127.0.0.1/secret"})
        with patch("url_safety.socket.getaddrinfo", side_effect=[[answer("93.184.216.34")], [answer("127.0.0.1")]]), patch("url_safety.requests.Session") as factory:
            factory.return_value.get.return_value = first
            with self.assertRaises(UnsafeURL):
                fetch_bounded("https://test.example", (1, 2))
            self.assertEqual(factory.call_count, 1)

    def test_redirect_loop_is_bounded(self):
        with patch("download_limits.request_get", side_effect=lambda *a, **k: MagicMock(status_code=302, headers={"Location": "/again"})) as get:
            with self.assertRaises(requests.exceptions.TooManyRedirects):
                fetch_bounded("https://test.example", (1, 2))
        self.assertEqual(get.call_count, 6)


if __name__ == "__main__":
    unittest.main()
