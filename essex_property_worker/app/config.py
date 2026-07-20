from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Settings:
    press_base_url: str = "https://press.essexregister.com/prodpress/clerk/ClerkHome.aspx?op=basic"
    press_first_record_date: date = date(1996, 10, 1)
    press_max_window_days: int = 90
    default_total_records: int = 750
    default_page_size: int = 100
    headless: bool = True
    slow_mo_ms: int = 0
    browser_timeout_ms: int = 45_000


settings = Settings()

