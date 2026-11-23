"""Settings for the VM / Marketing weekly bot (read from the project .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


# Optional: which region each store belongs to. When every store column in the
# Excel has a region here, marketing posts also show a per-region stock summary.
STORE_REGIONS: dict[str, str] = {
    "S01": "Buxoro",
    "S04": "Buxoro",
    "S02": "Qarshi",
    "S05": "Qarshi",
    "S03": "Samarqand",
    "S06": "Samarqand",
    "S07": "Toshkent",
    "S08": "Toshkent",
    "S09": "Toshkent",
    "S10": "Qarshi",
}

DEPARTMENTS = ("VM", "MARKETING")


def _int_env(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


# Empty "Region" cell -> the bot suggests every region whose stock is MORE than this.
REGION_MIN_STOCK = _int_env("REGION_MIN_STOCK", 5)

DEPARTMENT_TITLES = {
    "VM": ("🎨", "VM"),
    "MARKETING": ("📢", "MARKETING"),
}

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "dushanba": 0, "seshanba": 1, "chorshanba": 2, "payshanba": 3,
    "juma": 4, "shanba": 5, "yakshanba": 6,
}


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


# The server may run in UTC — all dates/times in the bot use this timezone.
TZ = ZoneInfo(_env("TIMEZONE", "Asia/Tashkent"))


def now() -> datetime:
    """Current local time (naive, in TZ)."""
    return datetime.now(TZ).replace(tzinfo=None)


def _chat_id(value: str) -> int | str | None:
    value = value.strip()
    if not value:
        return None
    if value.lstrip("-").isdigit():
        return int(value)
    return value  # e.g. "@my_channel"


def _id_set(value: str) -> frozenset[int]:
    ids = set()
    for part in value.replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return frozenset(ids)


def _weekday(value: str, default: int) -> int:
    value = value.strip().lower()
    if value.isdigit():
        return int(value) % 7
    return WEEKDAYS.get(value, default)


def _time(value: str, default: time) -> time:
    try:
        hours, minutes = value.strip().split(":")
        return time(int(hours), int(minutes))
    except (ValueError, AttributeError):
        return default


@dataclass(frozen=True)
class Settings:
    bot_token: str
    base_image_url: str
    webdav_user: str
    webdav_password: str
    group_chat_id: int | str | None
    vm_chat_id: int | str | None
    marketing_chat_id: int | str | None
    report_chat_id: int | str | None
    admin_ids: frozenset[int]
    vm_team_ids: frozenset[int]
    marketing_team_ids: frozenset[int]
    excel_file: Path
    data_dir: Path
    send_delay: float
    done_button: bool
    report_enabled: bool
    report_weekday: int  # 0 = Monday ... 6 = Sunday
    report_time: time
    # Monday morning: re-send last week's report, then delete last week's posts from the group
    cleanup_enabled: bool = True
    cleanup_weekday: int = 0
    cleanup_time: time = time(9, 0)

    def target_chat(self, department: str, fallback: int | str) -> int | str:
        chat = self.vm_chat_id if department == "VM" else self.marketing_chat_id
        if chat is None:
            chat = self.group_chat_id
        return chat if chat is not None else fallback

    def weekly_report_chat(self) -> int | str | None:
        return self.report_chat_id or self.group_chat_id or self.vm_chat_id or self.marketing_chat_id

    def is_admin(self, user_id: int | None) -> bool:
        # No ADMIN_IDS configured -> everybody may use admin features.
        return not self.admin_ids or (user_id in self.admin_ids)

    def team_ids(self, department: str) -> frozenset[int]:
        return self.vm_team_ids if department == "VM" else self.marketing_team_ids

    def can_confirm(self, department: str, user_id: int) -> bool:
        """VM posts are confirmed by the VM team, marketing posts by the marketing team.
        If a team list is empty, anybody may confirm. Admins may always confirm."""
        team = self.team_ids(department)
        return not team or user_id in team or user_id in self.admin_ids

    def department_of(self, user_id: int) -> str | None:
        for dept in DEPARTMENTS:
            if user_id in self.team_ids(dept):
                return dept
        return None


def load_settings() -> Settings:
    token = _env("VM_BOT_TOKEN") or _env("BOT_TOKEN")
    if not token:
        raise ValueError("VM_BOT_TOKEN (or BOT_TOKEN) is missing in .env")

    password = _env("WEBDAV_PASSWORD")
    if not password:
        raise ValueError("WEBDAV_PASSWORD is missing in .env")

    data_dir = BASE_DIR / _env("VM_DATA_DIR", "vm_marketing_data")
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "uploads").mkdir(exist_ok=True)

    excel = Path(_env("VM_EXCEL_FILE", "VM_MARKETING TEMPLATE.xlsx"))
    if not excel.is_absolute():
        excel = BASE_DIR / excel

    try:
        # Telegram allows a bot ~20 messages per minute in one group -> 3 s between posts.
        delay = float(_env("VM_SEND_DELAY", "3.0"))
    except ValueError:
        delay = 3.0

    def flag(name: str, default: str = "1") -> bool:
        return _env(name, default).lower() not in ("0", "false", "no", "off")

    return Settings(
        bot_token=token,
        base_image_url=_env("BASE_IMAGE_URL", "https://media.aynimo.uz:8085/"),
        webdav_user=_env("WEBDAV_USER", "webdav"),
        webdav_password=password,
        group_chat_id=_chat_id(_env("GROUP_CHAT_ID")),
        vm_chat_id=_chat_id(_env("VM_CHAT_ID")),
        marketing_chat_id=_chat_id(_env("MARKETING_CHAT_ID")),
        report_chat_id=_chat_id(_env("REPORT_CHAT_ID")),
        admin_ids=_id_set(_env("ADMIN_IDS")),
        vm_team_ids=_id_set(_env("VM_TEAM_IDS")),
        marketing_team_ids=_id_set(_env("MARKETING_TEAM_IDS")),
        excel_file=excel,
        data_dir=data_dir,
        send_delay=max(0.0, delay),
        done_button=flag("VM_DONE_BUTTON"),
        report_enabled=flag("WEEKLY_REPORT"),
        report_weekday=_weekday(_env("REPORT_DAY", "sunday"), 6),
        report_time=_time(_env("REPORT_TIME", "18:00"), time(18, 0)),
        cleanup_enabled=flag("WEEKLY_CLEANUP"),
        cleanup_weekday=_weekday(_env("CLEANUP_DAY", "monday"), 0),
        cleanup_time=_time(_env("CLEANUP_TIME", "09:00"), time(9, 0)),
    )
