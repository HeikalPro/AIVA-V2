from backend.config import get_settings
from backend.services.email.base import EmailMessage
from backend.services.email.smtp_mail import SmtpMailSender, get_smtp_mail_sender
from backend.services.email.zoho_mail import ZohoMailSender, get_zoho_mail_sender

__all__ = [
    "EmailMessage",
    "SmtpMailSender",
    "ZohoMailSender",
    "get_mail_sender",
    "get_smtp_mail_sender",
    "get_zoho_mail_sender",
]


def get_mail_sender() -> SmtpMailSender | ZohoMailSender:
    settings = get_settings()
    if settings.smtp_host and settings.smtp_password and settings.smtp_from_email:
        return get_smtp_mail_sender()
    return get_zoho_mail_sender()
