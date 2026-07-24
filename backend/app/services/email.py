"""Отправка писем через SMTP."""

import logging
import smtplib
from email.message import EmailMessage

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class EmailService:
    """Обёртка над SMTP для reset/invite/verify."""

    @property
    def is_configured(self) -> bool:
        return bool(settings.smtp_host)

    def send(self, to: str, subject: str, body: str) -> bool:
        """
        Отправляет plain-text письмо.

        Возвращает True при успехе, False если SMTP не настроен или ошибка.
        """
        if not self.is_configured:
            logger.warning("SMTP не настроен — письмо не отправлено: %s → %s", subject, to)
            return False

        message = EmailMessage()
        message["From"] = settings.smtp_from
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)

        try:
            if settings.smtp_use_tls:
                with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
                    server.starttls()
                    if settings.smtp_user:
                        server.login(settings.smtp_user, settings.smtp_password)
                    server.send_message(message)
            else:
                with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
                    if settings.smtp_user:
                        server.login(settings.smtp_user, settings.smtp_password)
                    server.send_message(message)
        except Exception:
            logger.exception("Ошибка отправки письма на %s", to)
            return False

        logger.info("Письмо отправлено: %s → %s", subject, to)
        return True

    def send_password_reset(self, to: str, reset_url: str) -> bool:
        return self.send(
            to,
            "Сброс пароля — tip My Code",
            f"Для сброса пароля перейдите по ссылке (действует 1 час):\n\n{reset_url}\n",
        )

    def send_invite(self, to: str, invite_url: str) -> bool:
        return self.send(
            to,
            "Приглашение — tip My Code",
            f"Для входа задайте пароль по ссылке (действует 72 часа):\n\n{invite_url}\n",
        )

    def send_email_verification(self, to: str, verify_url: str) -> bool:
        return self.send(
            to,
            "Подтверждение почты — tip My Code",
            f"Для подтверждения адреса почты перейдите по ссылке:\n\n{verify_url}\n",
        )


email_service = EmailService()
