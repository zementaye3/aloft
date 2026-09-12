"""
Email service for sending transactional emails.

Uses Resend (https://resend.com) by default with SendGrid as an alternative.
Both have free tiers suitable for development and small-scale production.

Configuration:
  - RESEND_API_KEY: Your Resend API key (default)
  - SENDGRID_API_KEY: Your SendGrid API key (alternative)
  - FROM_EMAIL: The sender email address (must be verified in your email service)
"""

from __future__ import annotations

import logging

import httpx

from app.core.config import get_settings

logger = logging.getLogger("aloft.email")


class EmailError(Exception):
    """Raised when email sending fails."""


async def send_email(
    to_email: str,
    subject: str,
    html_content: str,
) -> None:
    """Send an email via Resend or SendGrid.

    Args:
        to_email: Recipient email address
        subject: Email subject line
        html_content: HTML body content

    Raises:
        EmailError: If email sending fails
    """
    settings = get_settings()
    resend_key = settings.resend_api_key
    sendgrid_key = settings.sendgrid_api_key
    from_email = settings.from_email

    if not resend_key and not sendgrid_key:
        raise EmailError(
            "No email service configured. Set RESEND_API_KEY or SENDGRID_API_KEY in environment variables."
        )

    resend_api_key = resend_key.get_secret_value() if resend_key else None
    sendgrid_api_key = sendgrid_key.get_secret_value() if sendgrid_key else None

    if resend_api_key:
        try:
            await _send_via_resend(to_email, from_email, subject, html_content, resend_api_key)
            logger.info(f"Email sent via Resend to {to_email}")
            return
        except Exception as exc:
            logger.warning(f"Resend email failed, trying SendGrid: {exc}")

    if sendgrid_api_key:
        try:
            await _send_via_sendgrid(to_email, from_email, subject, html_content, sendgrid_api_key)
            logger.info(f"Email sent via SendGrid to {to_email}")
            return
        except Exception as exc:
            logger.warning(f"SendGrid email failed: {exc}")

    raise EmailError("All email providers failed")


async def send_password_reset_email(email: str, reset_token: str, base_url: str) -> None:
    """Send a password reset email to the user.

    Args:
        email: Recipient email address
        reset_token: JWT password reset token
        base_url: Base URL of your frontend (e.g., https://aloft.app)

    Raises:
        EmailError: If email sending fails
    """
    settings = get_settings()
    resend_key = settings.resend_api_key
    sendgrid_key = settings.sendgrid_api_key
    from_email = settings.from_email

    # Construct the reset link (frontend should handle the reset flow)
    reset_link = f"{base_url}/reset-password.html?token={reset_token}"

    # Email content
    subject = "Reset your Aloft password"
    html_content = f"""
    <html>
        <body>
            <h2>Password Reset Request</h2>
            <p>You requested a password reset for your Aloft account.</p>
            <p>Click the link below to reset your password:</p>
            <p><a href="{reset_link}">Reset Password</a></p>
            <p>This link will expire in 15 minutes.</p>
            <p>If you didn't request this password reset, you can safely ignore this email.</p>
            <p>— The Aloft Team</p>
        </body>
    </html>
    """

    resend_api_key = resend_key.get_secret_value() if resend_key else None
    sendgrid_api_key = sendgrid_key.get_secret_value() if sendgrid_key else None

    if resend_api_key:
        try:
            await _send_via_resend(email, from_email, subject, html_content, resend_api_key)
            logger.info(f"Password reset email sent via Resend to {email}")
            return
        except Exception as exc:
            logger.warning(f"Resend email failed, trying SendGrid: {exc}")

    if sendgrid_api_key:
        try:
            await _send_via_sendgrid(email, from_email, subject, html_content, sendgrid_api_key)
            logger.info(f"Password reset email sent via SendGrid to {email}")
            return
        except Exception as exc:
            logger.warning(f"SendGrid email failed: {exc}")

    raise EmailError(
        "Email service not configured. Please set RESEND_API_KEY or SENDGRID_API_KEY in environment variables."
    )


async def _send_via_resend(
    to_email: str,
    from_email: str,
    subject: str,
    html_content: str,
    api_key: str,
) -> None:
    """Send email via Resend API."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": from_email,
                "to": [to_email],
                "subject": subject,
                "html": html_content,
            },
            timeout=10.0,
        )

        if response.status_code != 200:
            error_detail = response.text
            raise EmailError(f"Resend API error: {response.status_code} - {error_detail}")


async def _send_via_sendgrid(
    to_email: str,
    from_email: str,
    subject: str,
    html_content: str,
    api_key: str,
) -> None:
    """Send email via SendGrid API."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "personalizations": [
                    {
                        "to": [{"email": to_email}],
                        "subject": subject,
                    }
                ],
                "from": {"email": from_email},
                "content": [
                    {
                        "type": "text/html",
                        "value": html_content,
                    }
                ],
            },
            timeout=10.0,
        )

        if response.status_code not in (200, 202):
            error_detail = response.text
            raise EmailError(f"SendGrid API error: {response.status_code} - {error_detail}")
