"""Общие хелперы для HTML-страниц."""

from datetime import datetime

from app.models.user import TariffCategory, User, UserRole


def format_expires(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%d.%m.%Y")


def format_expires_input(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d")


def role_home_url(role: UserRole) -> str:
    if role == UserRole.admin:
        return "/admin"
    if role == UserRole.manager:
        return "/manager"
    return "/cabinet"


def category_label(category: TariffCategory) -> str:
    return "Льготная" if category == TariffCategory.preferential else "Коммерческая"


def build_account_row(user: User) -> dict:
    """Собирает данные строки таблицы для одного пользователя."""
    link = user.account_links[0] if user.account_links else None
    return {
        "user": user,
        "link": link,
        "username": link.account_username if link else user.login,
        "expires_at": link.expires_at_cache if link else None,
        "status": link.status_cache if link else "—",
        "subscription_url": link.subscription_url_cache if link else None,
        "is_marzban_active": (link.status_cache == "active") if link else user.is_active,
    }
