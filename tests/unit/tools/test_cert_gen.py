from __future__ import annotations

import datetime
import ipaddress
import pathlib

from cryptography import x509

from open_ems.tools.cert_gen import generate_cert


def _load_cert(cert_path: pathlib.Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(cert_path.read_bytes())


def test_cert_files_created(tmp_path: pathlib.Path) -> None:
    cert_p = tmp_path / "cert.pem"
    key_p = tmp_path / "key.pem"
    generate_cert("testhost", "192.168.1.5", str(cert_p), str(key_p))
    assert cert_p.exists()
    assert key_p.exists()


def test_cert_has_correct_cn(tmp_path: pathlib.Path) -> None:
    cert_p = tmp_path / "cert.pem"
    generate_cert("myhost", "10.0.0.1", str(cert_p), str(tmp_path / "key.pem"))
    cert = _load_cert(cert_p)
    cn_attrs = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    assert len(cn_attrs) == 1
    assert cn_attrs[0].value == "myhost"


def test_cert_has_san_dns_hostname(tmp_path: pathlib.Path) -> None:
    cert_p = tmp_path / "cert.pem"
    generate_cert("devbox", "192.168.0.10", str(cert_p), str(tmp_path / "key.pem"))
    cert = _load_cert(cert_p)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    dns_names = san.value.get_values_for_type(x509.DNSName)
    assert "devbox" in dns_names


def test_cert_has_san_dns_localhost(tmp_path: pathlib.Path) -> None:
    cert_p = tmp_path / "cert.pem"
    generate_cert("devbox", "192.168.0.10", str(cert_p), str(tmp_path / "key.pem"))
    cert = _load_cert(cert_p)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    dns_names = san.value.get_values_for_type(x509.DNSName)
    assert "localhost" in dns_names


def test_cert_has_san_ip_lan(tmp_path: pathlib.Path) -> None:
    cert_p = tmp_path / "cert.pem"
    generate_cert("devbox", "192.168.0.10", str(cert_p), str(tmp_path / "key.pem"))
    cert = _load_cert(cert_p)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    ip_addresses = san.value.get_values_for_type(x509.IPAddress)
    assert ipaddress.IPv4Address("192.168.0.10") in ip_addresses


def test_cert_has_san_ip_loopback(tmp_path: pathlib.Path) -> None:
    cert_p = tmp_path / "cert.pem"
    generate_cert("devbox", "192.168.0.10", str(cert_p), str(tmp_path / "key.pem"))
    cert = _load_cert(cert_p)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    ip_addresses = san.value.get_values_for_type(x509.IPAddress)
    assert ipaddress.IPv4Address("127.0.0.1") in ip_addresses


def test_cert_valid_period(tmp_path: pathlib.Path) -> None:
    cert_p = tmp_path / "cert.pem"
    generate_cert("testhost", "10.0.0.1", str(cert_p), str(tmp_path / "key.pem"))
    cert = _load_cert(cert_p)
    now = datetime.datetime.now(datetime.UTC)
    delta = cert.not_valid_after_utc - now
    assert delta.days >= 364


def test_generate_cert_creates_parent_dirs(tmp_path: pathlib.Path) -> None:
    cert_p = tmp_path / "nested" / "deep" / "cert.pem"
    key_p = tmp_path / "nested" / "deep" / "key.pem"
    generate_cert("testhost", "10.0.0.1", str(cert_p), str(key_p))
    assert cert_p.exists()
    assert key_p.exists()
