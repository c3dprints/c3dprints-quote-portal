import os
import resend


def send_quote_notification(to_email: str, subject: str, html_body: str, text_body: str | None = None) -> dict:
    """
    Sends quote request notification using Resend.
    Requires:
    - RESEND_API_KEY
    - FROM_EMAIL
    """

    api_key = os.getenv("RESEND_API_KEY")
    from_email = os.getenv("FROM_EMAIL", "quotes@c3dprints.com")

    if not api_key:
        print("RESEND_API_KEY not configured. Email skipped.")
        return {"sent": False, "reason": "RESEND_API_KEY not configured"}

    resend.api_key = api_key

    params = {
        "from": from_email,
        "to": [to_email],
        "subject": subject,
        "html": html_body,
    }

    if text_body:
        params["text"] = text_body

    result = resend.Emails.send(params)
    return {"sent": True, "result": result}
