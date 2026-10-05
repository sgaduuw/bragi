"""Exercise SSRF protection through Requests, urllib3, and the socket boundary."""

from __future__ import annotations

import ipaddress
import socket

import pytest

from bragi.core import http
from bragi.core.http import SafeHTTPError, _assert_public_host


class StopConnect(BaseException):
    """Stop before any real network traffic, including retries."""


@pytest.fixture(autouse=True)
def guarded_transport(monkeypatch):
    monkeypatch.setattr(http, "_assert_public_host", _assert_public_host)
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def connects(monkeypatch):
    destinations = []

    class Socket:
        def __init__(self, *args, **kwargs):
            pass

        def setsockopt(self, *args):
            pass

        def settimeout(self, timeout):
            pass

        def close(self):
            pass

        def connect(self, address):
            destinations.append(address)
            raise StopConnect

    monkeypatch.setattr(socket, "socket", Socket)
    return destinations


def dns_answer(ip, port):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    address = (ip, port or 0, 0, 0) if family == socket.AF_INET6 else (ip, port or 0)
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", address)]


@pytest.mark.parametrize("method", [http.safe_get, http.safe_head, http.safe_post])
@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("public_ip", ["8.8.8.8", "2606:4700:4700::1111"])
def test_connection_uses_validated_ip(monkeypatch, connects, method, scheme, public_ip):
    resolutions = []

    def resolve(host, port, *args, **kwargs):
        resolutions.append((host, port))
        try:
            ipaddress.ip_address(host)
        except ValueError:
            # All URL-time checks pass. A later transport lookup of the
            # attacker-controlled hostname rebinds to loopback.
            ip = public_ip if port is None else "127.0.0.1"
        else:
            ip = host
        return dns_answer(ip, port)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with pytest.raises(StopConnect):
        method(f"{scheme}://rebound.example:8123/resource")

    assert resolutions
    assert len(connects) == 1
    assert connects[0][0] == public_ip
    assert connects[0][1] == 8123


@pytest.mark.parametrize(
    "scheme,proxy_var",
    [
        ("http", "HTTP_PROXY"),
        ("https", "HTTPS_PROXY"),
        ("http", "ALL_PROXY"),
        ("https", "ALL_PROXY"),
    ],
)
def test_configured_proxy_cannot_bypass_guard(monkeypatch, connects, scheme, proxy_var):
    monkeypatch.setenv(proxy_var, "http://proxy.example:8080")
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, port, *a, **k: dns_answer("8.8.8.8", port)
    )
    with pytest.raises(SafeHTTPError, match="prox"):
        http.safe_get(f"{scheme}://public.example/")
    assert connects == []


@pytest.mark.parametrize("ip", ["100.64.0.1", "::ffff:100.64.0.1"])
def test_shared_address_space_is_not_public(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: dns_answer(ip, None))
    with pytest.raises(SafeHTTPError, match="non-public"):
        http._validate_url("https://shared.example/")


def test_empty_dns_answer_fails_closed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [])
    with pytest.raises(SafeHTTPError, match="address"):
        http._validate_url("https://empty.example/")


@pytest.fixture
def local_https(monkeypatch, tmp_path):
    """Real TLS and HTTP over loopback, reached only after a public-IP check."""
    import datetime
    import ssl
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "public.example")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("public.example"), x509.DNSName("next.example")]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    seen, server_names, destinations = [], [], []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            seen.append((self.command, self.path, dict(self.headers), body))
            if self.path == "/other":
                location = f"https://next.example:{self.server.server_port}/ok"
            elif self.path == "/private":
                location = f"http://127.0.0.1:{self.server.server_port}/ok"
            elif self.path == "/redirect":
                location = "/ok"
            else:
                location = None
            self.send_response(302 if location else 200)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Length", "2")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(b"ok")

        do_HEAD = do_GET
        do_POST = do_GET

        def log_message(self, *args):
            pass

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_path, key_path)
    context.set_servername_callback(lambda sock, name, ctx: server_names.append(name))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real_resolve = socket.getaddrinfo
    real_connect = socket.socket.connect

    def resolve(host, port, *args, **kwargs):
        if host.endswith(".example"):
            return dns_answer("8.8.8.8", port)
        return real_resolve(host, port, *args, **kwargs)

    def connect(sock, address):
        # Only after asserting the production transport chose a public IP
        # do we route the test connection to our local server.
        assert address == ("8.8.8.8", server.server_port)
        destinations.append(address)
        return real_connect(sock, ("127.0.0.1", server.server_port))

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(cert_path))
    try:
        yield SimpleNamespace(
            url=f"https://public.example:{server.server_port}",
            port=server.server_port,
            seen=seen,
            names=server_names,
            destinations=destinations,
            connect=connect,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST"])
def test_pinned_https_preserves_request_and_tls_identity(local_https, method):
    headers = {
        "Host": f"public.example:{local_https.port}",
        "Authorization": "Signature test-signature",
    }
    url = local_https.url + "/resource?original=1"
    kwargs = {"headers": headers}
    if method == "POST":
        kwargs["data"] = b"signed body"
    response = getattr(http, f"safe_{method.lower()}")(url, **kwargs)
    assert response.status_code == 200
    assert response.url == url
    assert response.content == (b"" if method == "HEAD" else b"ok")
    assert local_https.names == ["public.example"]
    [(sent_method, path, sent_headers, body)] = local_https.seen
    assert (sent_method, path) == (method, "/resource?original=1")
    assert sent_headers["Host"] == headers["Host"]
    assert sent_headers["Authorization"] == headers["Authorization"]
    assert body == (b"signed body" if method == "POST" else b"")


def test_pinned_https_rejects_wrong_certificate_hostname(local_https):
    import requests

    url = local_https.url.replace("public.example", "wrong.example")
    with pytest.raises(requests.exceptions.SSLError, match="certificate"):
        http.safe_get(url)
    assert local_https.names == ["wrong.example"]
    assert local_https.seen == []


@pytest.mark.parametrize("path", ["/redirect", "/other"])
def test_redirect_preserves_host_validation_and_credential_rules(local_https, path):
    response = http.safe_get(
        local_https.url + path, headers={"Authorization": "Bearer test-credential"}
    )
    assert response.status_code == 200
    assert len(response.history) == 1
    assert len(local_https.seen) == 2
    target_host = "next.example" if path == "/other" else "public.example"
    assert response.url == f"https://{target_host}:{local_https.port}/ok"
    assert local_https.names == ["public.example", target_host]
    redirected_headers = local_https.seen[1][2]
    assert redirected_headers["Host"] == f"{target_host}:{local_https.port}"
    if path == "/other":
        assert "Authorization" not in redirected_headers
    else:
        assert redirected_headers["Authorization"] == "Bearer test-credential"


def test_redirect_to_private_address_is_blocked_before_connect(local_https):
    with pytest.raises(SafeHTTPError, match="non-public"):
        http.safe_get(local_https.url + "/private")
    assert len(local_https.seen) == 1
    assert len(local_https.destinations) == 1


def test_removing_proxy_recovers_guarded_fetch(monkeypatch, local_https):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    with pytest.raises(SafeHTTPError, match="prox"):
        http.safe_get(local_https.url)
    assert local_https.destinations == []
    monkeypatch.delenv("HTTPS_PROXY")
    assert http.safe_get(local_https.url).content == b"ok"
    assert len(local_https.seen) == 1


def test_no_proxy_allows_direct_validated_connection(monkeypatch, local_https):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", "public.example")
    assert http.safe_get(local_https.url).content == b"ok"
    assert len(local_https.seen) == 1


def test_unreachable_public_address_falls_back_to_another(monkeypatch, local_https):
    original_resolve = socket.getaddrinfo
    attempts = []

    def resolve(host, port, *args, **kwargs):
        if host == "public.example":
            # Duplicate answers must not repeat a failed connection attempt.
            return dns_answer("9.9.9.9", port) * 2 + dns_answer("8.8.8.8", port)
        return original_resolve(host, port, *args, **kwargs)

    def connect(sock, address):
        attempts.append(address[0])
        if address[0] == "9.9.9.9":
            raise ConnectionRefusedError("First public address is unavailable")
        return local_https.connect(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket.socket, "connect", connect)
    assert http.safe_get(local_https.url).content == b"ok"
    assert attempts == ["9.9.9.9", "8.8.8.8"]


@pytest.mark.parametrize("error", [TimeoutError, ConnectionRefusedError])
def test_connection_errors_keep_requests_exception_contract(monkeypatch, error):
    import requests

    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, port, *a, **k: dns_answer("8.8.8.8", port)
    )

    def fail(sock, address):
        raise error("Test connection failure")

    monkeypatch.setattr(socket.socket, "connect", fail)
    expected = requests.ConnectTimeout if error is TimeoutError else requests.ConnectionError
    with pytest.raises(expected):
        http.safe_get("https://unavailable.example/")


@pytest.mark.parametrize("ip", ["64:ff9b::a9fe:a9fe", "64:ff9b::7f00:1", "4000::1"])
def test_reserved_ipv6_addresses_remain_blocked(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: dns_answer(ip, None))
    with pytest.raises(SafeHTTPError, match="non-public"):
        http._validate_url("https://reserved.example/")


def test_connection_preserves_explicit_fqdn(monkeypatch, connects):
    names = []

    def resolve(host, port, *args, **kwargs):
        names.append(host)
        if host == "public.example.":
            ip = "8.8.8.8"
        elif host == "public.example":
            # Without the trailing dot, the resolver may use a search domain.
            ip = "1.1.1.1"
        else:
            ip = host
        return dns_answer(ip, port)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with pytest.raises(StopConnect):
        http.safe_get("https://public.example.:8123/resource")
    assert connects == [("8.8.8.8", 8123)]
    assert "public.example" not in names


@pytest.mark.parametrize("timeout", [3, 10])
def test_preferred_public_address_avoids_slow_fallbacks(monkeypatch, local_https, timeout):
    original_resolve = socket.getaddrinfo
    preferred = "8.8.8.8"
    fallbacks = ["1.1.1.1", "2001:4860:4860::8888"]
    attempts = []
    assert all(ip < preferred for ip in fallbacks)

    def resolve(host, port, *args, **kwargs):
        if host == "public.example":
            return [answer for ip in [preferred, *fallbacks] for answer in dns_answer(ip, port)]
        return original_resolve(host, port, *args, **kwargs)

    def connect(sock, address):
        attempts.append((address[0], sock.gettimeout()))
        if address[0] in fallbacks:
            raise TimeoutError("A lower-priority address timed out")
        return local_https.connect(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket.socket, "connect", connect)
    assert http.safe_get(local_https.url, timeout=timeout).content == b"ok"
    assert attempts == [(preferred, timeout)]
