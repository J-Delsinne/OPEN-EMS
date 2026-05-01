#!/usr/bin/env python3
from __future__ import annotations

import socket

from open_ems.tools.cert_gen import generate_cert, get_lan_ip


def main() -> None:
    hostname = socket.gethostname()
    lan_ip = get_lan_ip()
    cert_path = "certs/cert.pem"
    key_path = "certs/key.pem"
    generate_cert(hostname=hostname, lan_ip=lan_ip, cert_path=cert_path, key_path=key_path)
    print(f"TLS certificate written to {cert_path} and {key_path}")
    print(f"  CN={hostname}, SAN: {hostname}, localhost, {lan_ip}, 127.0.0.1")


if __name__ == "__main__":
    main()
