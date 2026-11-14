from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, EmailStr, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite:///./blog.db"
    secret_key: SecretStr = Field(
        min_length=32,
        validation_alias="JWT_SECRET_KEY",
    )
    algorithm: Literal["HS256"] = "HS256"
    access_token_expire_minutes: int = Field(default=30, gt=0)
    site_url: AnyHttpUrl = Field(
        default="http://localhost:8000",
        validate_default=True,
    )
    trusted_hosts: str = ""
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from_email: EmailStr | None = None
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    login_rate_limit: int = Field(default=8, ge=1)
    login_rate_window_seconds: int = Field(default=900, ge=60)

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        if self.app_env == "production":
            secret_key = self.secret_key.get_secret_value().lower()
            if "replace" in secret_key or "change-me" in secret_key:
                raise ValueError("JWT_SECRET_KEY must be replaced for production.")
            if not self.database_url.startswith(("postgresql://", "postgresql+")):
                raise ValueError("Production deployments must use PostgreSQL.")
            if self.site_url.scheme != "https":
                raise ValueError("SITE_URL must use HTTPS in production.")
            if not self.trusted_host_list:
                raise ValueError("TRUSTED_HOSTS must be set in production.")
            if not self.smtp_host or not self.smtp_from_email:
                raise ValueError("SMTP_HOST and SMTP_FROM_EMAIL are required in production.")
            if not (self.smtp_starttls or self.smtp_ssl):
                raise ValueError("SMTP must use STARTTLS or implicit TLS in production.")
            if self.smtp_starttls and self.smtp_ssl:
                raise ValueError("Choose either SMTP_STARTTLS or SMTP_SSL, not both.")
            smtp_password = (
                self.smtp_password.get_secret_value().strip()
                if self.smtp_password
                else ""
            )
            if bool(self.smtp_username and self.smtp_username.strip()) != bool(
                smtp_password
            ):
                raise ValueError("SMTP_USERNAME and SMTP_PASSWORD must be set together.")
            if "*" in self.trusted_host_list:
                raise ValueError("TRUSTED_HOSTS cannot contain a wildcard in production.")
        return self

    @property
    def trusted_host_list(self) -> list[str]:
        return [
            host.strip()
            for host in self.trusted_hosts.split(",")
            if host.strip()
        ]

settings = Settings()