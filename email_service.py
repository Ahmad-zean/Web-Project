import logging
import smtplib
import ssl
from email.message import EmailMessage

from config import settings

logger = logging.getLogger(__name__)


class EmailDeliveryError(Exception):
    pass


def send_email(recipient: str, subject: str, body: str) -> None:
    if not settings.smtp_host or not settings.smtp_from_email:
        raise EmailDeliveryError("Email delivery is not configured.")
    username = (settings.smtp_username or "").strip()
    password = (
        settings.smtp_password.get_secret_value().strip()
        if settings.smtp_password
        else ""
    )
    if bool(username) != bool(password):
        raise EmailDeliveryError("SMTP_PASSWORD is required when SMTP_USERNAME is set.")

    message = EmailMessage()
    message["From"] = str(settings.smtp_from_email)
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)

    try:
        if settings.smtp_ssl:
            smtp_connection = smtplib.SMTP_SSL(
                settings.smtp_host,
                settings.smtp_port,
                timeout=10,
                context=ssl.create_default_context(),
            )
        else:
            smtp_connection = smtplib.SMTP(
                settings.smtp_host,
                settings.smtp_port,
                timeout=10,
            )
        with smtp_connection as server:
            server.ehlo()
            if settings.smtp_starttls and not settings.smtp_ssl:
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            if username:
                server.login(username, password)
            server.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        logger.exception("Could not deliver email.")
        raise EmailDeliveryError("The email could not be delivered.") from exc


def send_verification_email(recipient: str, token: str) -> None:
    link = f"{str(settings.site_url).rstrip('/')}/verify-email/{token}"
    send_email(
        recipient,
        "Verify your email address",
        "Use this link within 24 hours to verify your account:\n\n"
        f"{link}\n\nIf you did not create an account, you can ignore this email.",
    )


def send_password_reset_email(recipient: str, token: str) -> None:
    link = f"{str(settings.site_url).rstrip('/')}/reset-password/{token}"
    send_email(
        recipient,
        "Reset your password",
        "Use this link within 30 minutes to choose a new password:\n\n"
        f"{link}\n\nIf you did not request a reset, you can ignore this email.",
    )
