from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Global settings for scholar-pdf-kit."""
    mailto: str = "student@university.edu"

    # Download configurations
    download_dir: Path = Path("downloads")
    max_concurrent_downloads: int = 5
    download_timeout: int = 30  # seconds

    # Institutional proxy support
    # EZproxy:   https://proxy.university.edu/login?url=
    # Subdomain: https://www.sndl1.arn.dz   (host-prefix style, auto-detected)
    # Prefix:    http://proxy:3128
    proxy_url: str = ""
    # auto | ezproxy | subdomain | prefix
    proxy_style: str = "auto"

    # Optional structural (pypdf) validation on ingest, beyond the binary
    # %PDF- header + %%EOF trailer signature check.
    pdf_structural_validation: bool = False

    # Publisher direct-PDF patterns
    # When True, compute direct-PDF URLs for IEEE/Elsevier/Springer/arXiv/MDPI
    # to bypass Cloudflare/WAF on landing pages.
    enable_publisher_direct_patterns: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
