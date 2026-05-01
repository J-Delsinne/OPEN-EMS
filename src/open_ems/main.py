from __future__ import annotations

import uvicorn

from open_ems.settings import get_settings
from open_ems.web.app import create_app

app = create_app()

if __name__ == "__main__":
    settings = get_settings()
    if bool(settings.tls_cert_path) != bool(settings.tls_key_path):
        raise ValueError("TLS_CERT_PATH and TLS_KEY_PATH must both be set or both omitted")
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.port,
        ssl_certfile=settings.tls_cert_path,
        ssl_keyfile=settings.tls_key_path,
        reload=False,
    )
