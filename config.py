"""Конфигурация бота (pydantic-settings, чтение из .env / переменных окружения)."""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Telegram ---
    bot_token: SecretStr
    # Чаты/пользователи, которые получают сигналы и могут управлять ботом (через запятую)
    telegram_chat_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # --- Стратегия ---
    order_size_usdc: float = 1.0          # строго $1 на сделку
    max_price: float = 0.01               # <= 0.01 => коэффициент >= 100x
    max_open_positions: int = 10          # лимит одновременно активных сделок
    min_time_to_end_minutes: int = 15     # не раньше чем через 15 минут
    max_time_to_end_hours: float = 24.0   # не позже чем через 24 часа
    # Предфильтр по цене Gamma (mid/last), чтобы не дергать стакан для всех токенов
    prefilter_price: float = 0.03

    # --- Фоновые задачи ---
    scan_interval_sec: int = 60
    resolution_check_interval_sec: int = 120
    max_event_pages: int = 30             # страниц по 100 событий у Gamma

    # --- Режим торговли ---
    trading_mode: Literal["signals", "auto"] = "signals"
    private_key: SecretStr | None = None
    # 0 = EOA, 1 = Magic/email proxy, 2 = браузерный кошелек (Phantom/MetaMask) proxy-safe
    signature_type: int = 0
    funder_address: str | None = None     # адрес профиля Polymarket (для signature_type 1/2)
    order_type: Literal["FOK", "GTC"] = "FOK"

    # --- API ---
    gamma_host: str = "https://gamma-api.polymarket.com"
    clob_host: str = "https://clob.polymarket.com"
    polymarket_web: str = "https://polymarket.com"
    chain_id: int = 137
    http_timeout_sec: float = 20.0
    http_max_retries: int = 5
    gamma_max_rps: float = 4.0
    clob_max_rps: float = 8.0

    # --- Прочее ---
    db_path: str = "data/bot.db"
    timezone: str = "UTC"
    log_level: str = "INFO"

    @field_validator("telegram_chat_ids", mode="before")
    @classmethod
    def _split_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return [int(x) for x in v.replace(";", ",").split(",") if x.strip()]
        if isinstance(v, int):
            return [v]
        return v

    @field_validator("private_key", "funder_address", mode="before")
    @classmethod
    def _empty_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        if not self.telegram_chat_ids:
            raise ValueError("TELEGRAM_CHAT_IDS не задан — некуда отправлять сигналы")
        if not 0 < self.max_price < 1:
            raise ValueError("MAX_PRICE должен быть в диапазоне (0, 1)")
        if self.order_size_usdc <= 0:
            raise ValueError("ORDER_SIZE_USDC должен быть > 0")
        if self.max_open_positions <= 0:
            raise ValueError("MAX_OPEN_POSITIONS должен быть > 0")
        if self.signature_type not in (0, 1, 2):
            raise ValueError("SIGNATURE_TYPE должен быть 0, 1 или 2")
        if self.signature_type in (1, 2) and self.private_key and not self.funder_address:
            raise ValueError("Для SIGNATURE_TYPE 1/2 нужен FUNDER_ADDRESS (адрес профиля Polymarket)")
        ZoneInfo(self.timezone)  # бросит исключение при неверной зоне
        return self

    @property
    def auto_trading(self) -> bool:
        return self.trading_mode == "auto" and self.private_key is not None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
