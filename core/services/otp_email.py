"""Transactional OTP delivery via Resend (M11 sign-in).

Uses the raw Resend REST API through httpx — no extra SDK dependency
(httpx is already in requirements.txt). The sender address comes from
`RESEND_FROM_EMAIL`; Resend requires a verified domain (free tier
includes one). Defaults to Resend's sandbox address, which only delivers
to the account owner — set the env var once the domain is verified.

Fail-open by design: if the API key is missing, `send_otp_email`
returns False and the caller degrades gracefully (the email/OTP path
reports "not configured", Google sign-in still works).
"""

import os

import httpx

RESEND_API_URL = "https://api.resend.com/emails"


def send_otp_email(email: str, code: str) -> bool:
    """Send a 6-digit sign-in code to `email`. Returns True on accepted."""
    api_key = os.getenv("RESEND_API_KEY", "")
    if not api_key:
        return False
    sender = os.getenv("RESEND_FROM_EMAIL", "Rhodey <onboarding@resend.dev>")
    try:
        resp = httpx.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": sender,
                "to": [email],
                "subject": "Your Rhodey sign-in code",
                "html": (
                    "<div style='font-family:system-ui,sans-serif;max-width:420px;"
                    "margin:0 auto;padding:24px;background:#faf7f2;border-radius:12px'>"
                    "<h2 style='color:#2b3a42;margin:0 0 8px'>Rhodey sign-in</h2>"
                    f"<p style='color:#4a5560'>Your code is:</p>"
                    f"<div style='font-size:28px;font-weight:700;letter-spacing:8px;"
                    f"color:#0d5c75;padding:12px 16px;background:#ffffff;"
                    f"border-radius:8px;display:inline-block'>{code}</div>"
                    "<p style='color:#7a8389;font-size:13px;margin-top:16px'>"
                    "It expires in 10 minutes. If you didn't request this, "
                    "you can safely ignore it.</p>"
                    "</div>"
                ),
            },
            timeout=15,
        )
        return resp.status_code in (200, 201)
    except Exception:
        return False
