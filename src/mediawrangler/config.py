"""Infrastructure settings from the environment.

Only settings that must exist before the database is reachable live here.
Everything else (connectors, schedules, matching rules) lives in the database
so the settings export covers it.
"""

import ipaddress
import os
from functools import cached_property
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MW_", extra="ignore")

    port: int = 8790
    # Where apps send webhooks: a second listener, separate from the login page,
    # meant for the LAN only. 0 turns it off.
    webhook_port: int = 8791
    data_dir: Path = Path("/data")
    media_dir: Path = Path("/media")
    # The share's top-level folders that are libraries, comma separated (empty: every folder).
    # The share is mounted whole so a move to the trash is a rename; this keeps Music, eBooks
    # and the rest out of the Libraries page.
    media_folders: str = ""
    secret_key: SecretStr | None = None
    secret_key_file: Path | None = None
    public_url: str = "http://localhost:8790"
    trusted_proxies: str = ""
    log_level: str = "info"

    @property
    def media_folder_names(self) -> set[str]:
        return {f.strip() for f in self.media_folders.split(",") if f.strip()}

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mediawrangler.db"

    @property
    def secure_cookies(self) -> bool:
        """https public URL means internet-facing; http means LAN-only mode."""
        return urlsplit(self.public_url).scheme == "https"

    @property
    def public_origin(self) -> str:
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}"

    @cached_property
    def trusted_proxy_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        nets = []
        for item in self.trusted_proxies.replace(";", ",").split(","):
            item = item.strip()
            if item:
                nets.append(ipaddress.ip_network(item, strict=False))
        return nets

    @cached_property
    def timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(os.environ.get("TZ") or "UTC")
        except ZoneInfoNotFoundError:
            return ZoneInfo("UTC")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def override_settings(settings: Settings | None) -> None:
    """Replace the process settings (tests and app factory)."""
    global _settings
    _settings = settings
