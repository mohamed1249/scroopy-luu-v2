import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
"""Optional local-only TLS verification (requires cryptography).

A controlled test redirect maps a validated public placeholder IP to our local
TLS fixture. Production destination checks remain unchanged.
"""
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import ssl
import tempfile
import threading
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import requests
from download_limits import fetch_bounded


def main():
    with tempfile.TemporaryDirectory(prefix="scroopy_tls_verify_") as temporary:
        root = Path(temporary)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture.example")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=1))
                .not_valid_after(now + datetime.timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("fixture.example")]), critical=False)
                .sign(key, hashes.SHA256()))
        cert_path, key_path = root / "cert.pem", root / "key.pem"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
        observed_hosts = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                observed_hosts.append(self.headers["Host"])
                body = b"<main>Local TLS fixture</main>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert_path), str(key_path))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original_dns = socket.getaddrinfo
        def dns(host, port, *args, **kwargs):
            if host in ("fixture.example", "mismatch.example"):
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
            return original_dns(host, port, *args, **kwargs)
        def local_connection(address, timeout, **kwargs):
            assert address == ("93.184.216.34", 443)
            return socket.create_connection(server.server_address, timeout)
        try:
            with patch("url_safety.socket.getaddrinfo", side_effect=dns), patch("url_safety.create_connection", side_effect=local_connection), patch("url_safety.requests.certs.where", return_value=str(cert_path)):
                result = fetch_bounded("https://fixture.example/", (2, 2))
                assert b"Local TLS fixture" in result.content
                assert observed_hosts == ["fixture.example"]
                print("PASS: pinned transport works with original Host and valid TLS hostname")
                try:
                    fetch_bounded("https://mismatch.example/", (2, 2))
                except requests.exceptions.SSLError:
                    print("PASS: wrong TLS hostname is rejected")
                else:
                    raise AssertionError("TLS hostname mismatch was accepted")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    main()
