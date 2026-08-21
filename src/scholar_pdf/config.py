from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """Global settings for scholar-pdf-kit."""
    mailto: str = "student@university.edu"
    
    # Download configurations
    download_dir: Path = Path("downloads")
    max_concurrent_downloads: int = 5
    download_timeout: int = 30  # seconds
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()
