"""Public-web-only transport. Validates and pins DNS before connecting.

No global socket monkey-patching, environment proxies, or automatic redirects.
TLS still uses the original hostname for SNI and certificate verification.
"""
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError
from urllib3.util.connection import create_connection


class UnsafeURL(requests.exceptions.RequestException):
    pass


def public_destination(url):
    if not isinstance(url, str) or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise UnsafeURL("URL must be a string without whitespace/control characters")
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise UnsafeURL("Only absolute HTTP(S) URLs are allowed")
        if parsed.username is not None or parsed.password is not None:
            raise UnsafeURL("Credentials in URLs are not allowed")
        default_port = 443 if parsed.scheme == "https" else 80
        port = parsed.port if parsed.port is not None else default_port
        if port != default_port:
            raise UnsafeURL("Only the standard HTTP/HTTPS port is allowed")
        host = parsed.hostname.encode("idna").decode("ascii")
    except (ValueError, UnicodeError) as exc:
        raise UnsafeURL("Malformed URL") from exc
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise requests.exceptions.ConnectionError("DNS resolution failed") from exc
    if not addresses:
        raise UnsafeURL("URL has no usable address")
    ips = []
    for entry in addresses:
        ip = ipaddress.ip_address(entry[4][0])
        mapped = getattr(ip, "ipv4_mapped", None)
        if not ip.is_global or (mapped is not None and not mapped.is_global):
            raise UnsafeURL("Local, private, and special-use destinations are blocked")
        ips.append(str(ip))
    return parsed.scheme, host, port, ips[0]


def _pinned_connection(base, address):
    class PinnedConnection(base):
        def _new_conn(self):
            try:
                return create_connection((address, self.port), self.timeout,
                                         source_address=self.source_address,
                                         socket_options=self.socket_options)
            except socket.timeout as exc:
                raise ConnectTimeoutError(self, "Connection timed out") from exc
            except OSError as exc:
                raise NewConnectionError(self, "Connection failed") from exc
    return PinnedConnection


class PinnedAdapter(HTTPAdapter):
    def __init__(self, scheme, host, port, address):
        pool_type = HTTPSConnectionPool if scheme == "https" else HTTPConnectionPool
        tls = {"ssl_context": ssl.create_default_context(cafile=requests.certs.where())} if scheme == "https" else {}
        self.pinned_pool = pool_type(host, port, **tls)
        base = HTTPSConnection if scheme == "https" else HTTPConnection
        self.pinned_pool.ConnectionCls = _pinned_connection(base, address)
        super().__init__(max_retries=0)

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        return self.pinned_pool

    def get_connection(self, url, proxies=None):
        return self.pinned_pool

    def close(self):
        self.pinned_pool.close()
        super().close()


def safe_get(url, *, timeout, stream=True, allow_redirects=False):
    if allow_redirects:
        raise ValueError("Redirects must be checked individually")
    destination = public_destination(url)
    session = requests.Session()
    session.trust_env = False
    session.mount(destination[0] + "://", PinnedAdapter(*destination))
    try:
        response = session.get(url, timeout=timeout, stream=stream, allow_redirects=False)
    except Exception:
        session.close()
        raise
    original_close = response.close
    def close():
        try:
            original_close()
        finally:
            session.close()
    response.close = close
    return response
