import os
import resend


def send_quote_notification(to_email: str, subject: str, html_body: str, text_body: str | None = None) -> dict:
    api_key = os.getenv("RESEND_API_KEY")
    from_email = os.getenv("FROM_EMAIL", "quotes@c3dprints.com")

    if not api_key:
        print(f"[email] SKIPPED to={to_email!r} subject={subject!r} reason=RESEND_API_KEY not configured")
        return {"sent": False, "reason": "RESEND_API_KEY not configured"}

    resend.api_key = api_key
    params = {"from": from_email, "to": [to_email], "subject": subject, "html": html_body}
    if text_body:
        params["text"] = text_body

    try:
        result = resend.Emails.send(params)
        # Resend returns {"id": "..."} (dict) or an object with .id
        msg_id = result.get("id") if isinstance(result, dict) else getattr(result, "id", None)
        print(f"[email] SENT to={to_email!r} subject={subject!r} id={msg_id} from={from_email!r}")
        return {"sent": True, "id": msg_id, "result": result}
    except Exception as exc:
        print(f"[email] FAILED to={to_email!r} subject={subject!r} from={from_email!r} error={exc}")
        return {"sent": False, "reason": str(exc)}
