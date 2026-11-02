"""`mediawrangler` / `python -m mediawrangler`: run the server."""

import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "mediawrangler.main:create_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104  (inside the container)
        port=settings.port,
        log_level=settings.log_level.lower(),
        # Client IPs come from MW_TRUSTED_PROXIES handling, not uvicorn's.
        proxy_headers=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
