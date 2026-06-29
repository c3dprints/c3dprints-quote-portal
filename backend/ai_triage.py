import os
import json
from openai import OpenAI


def build_plain_summary(data: dict) -> str:
    return f"""
New C3D Prints quote request

Customer: {data.get("name")} <{data.get("email")}>
Phone: {data.get("phone") or "Not provided"}

Project:
{data.get("project_description")}

Quantity: {data.get("quantity")}
Approx size: {data.get("approx_size") or "Not provided"}
Material: {data.get("material_preference") or "Not sure"}
Color: {data.get("color_preference") or "Not provided"}
Use case: {data.get("use_case") or "Not provided"}
Deadline: {data.get("deadline") or "Not provided"}
Delivery: {data.get("delivery_method") or "Not provided"}
Shipping location: {data.get("shipping_location") or "Not provided"}

Requirements:
{", ".join(data.get("requirements", [])) if data.get("requirements") else "None selected"}

Customer notes:
{data.get("additional_notes") or "None"}
""".strip()


def ai_triage_summary(data: dict) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

    if not api_key:
        return build_plain_summary(data)

    try:
        client = OpenAI(api_key=api_key)
        prompt = f"""
You are the internal quoting assistant for C3D Prints, a custom 3D printing shop.

Analyze this quote request and produce an internal summary.

Include:
1. Project type
2. Complexity: Low / Medium / High
3. Recommended material
4. Red flags or missing info
5. Questions to ask customer
6. Suggested next action
7. Short draft reply to customer

Quote request:
{data}
"""
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You help triage custom 3D printing quote requests."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.25,
        )
        return response.choices[0].message.content.strip()
    except Exception as exc:
        print(f"AI triage failed: {exc}")
        return build_plain_summary(data)


# Keys the admin dashboard reads off `ai_quote_structured` (see admin.html
# renderAiStructured / applyAiStructuredToCalculator / useAiCustomerReply).
def _empty_structured() -> dict:
    return {
        "recommended_material": None,
        "complexity": None,            # Low / Medium / High
        "confidence": None,            # Low / Medium / High
        "estimated_grams": None,
        "estimated_hours": None,
        "fail_rate": None,             # percent, e.g. 10
        "price_min": None,
        "price_max": None,
        "complexity_multiplier": None,
        "risk_flags": [],
        "questions_for_customer": [],
        "customer_reply": None,
    }


def ai_quote_assist(data: dict) -> dict:
    """Admin "Generate AI Quote Assist" generator.

    Returns {"text": <internal summary>, "structured": <dict matching the keys
    the admin dashboard renders>}. Degrades gracefully to a plain summary and an
    empty structured block when no OpenAI key is configured or the call fails.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

    if not api_key:
        return {"text": build_plain_summary(data), "structured": _empty_structured()}

    try:
        client = OpenAI(api_key=api_key)
        prompt = f"""
You are the internal quoting assistant for C3D Prints, a custom 3D printing shop
(custom prints, cosplay parts, made-to-order STL jobs).

Analyze the quote request below and respond with a single JSON object using EXACTLY
these keys:

- "internal_summary": string. A concise internal write-up covering project type,
  complexity, recommended material, red flags / missing info, suggested next action.
- "recommended_material": string (e.g. "PLA", "PETG", "ABS", "Resin").
- "complexity": one of "Low", "Medium", "High".
- "confidence": one of "Low", "Medium", "High" (your confidence in this estimate).
- "estimated_grams": number. Best estimate of filament/resin grams for the whole order.
- "estimated_hours": number. Estimated total print hours for the whole order.
- "fail_rate": number. Expected failure/reprint rate as a percent (0-100).
- "complexity_multiplier": number >= 1.0 (1.0 simple, up to ~2.5 very risky).
- "price_min": number. Suggested customer price range, low end, in USD.
- "price_max": number. Suggested customer price range, high end, in USD.
- "risk_flags": array of short strings (thin walls, supports, bridging, overhangs, etc.).
- "questions_for_customer": array of short strings to clarify scope before quoting.
- "customer_reply": string. A short, friendly draft reply to the customer.

Pricing guidance: higher print risk and finishing labor should push the price range up.
If information is missing, estimate conservatively and note it in risk_flags/questions.
Use numbers (not strings) for all numeric fields. Respond with JSON only.

Quote request:
{data}
"""
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You help quote custom 3D printing jobs. Respond ONLY with a valid JSON object.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        parsed = json.loads(response.choices[0].message.content)

        structured = _empty_structured()
        for key in structured:
            if key in parsed and parsed[key] is not None:
                structured[key] = parsed[key]
        # Frontend spreads these with [...], so they must be lists.
        for list_key in ("risk_flags", "questions_for_customer"):
            value = structured[list_key]
            if not isinstance(value, list):
                structured[list_key] = [value] if value else []

        text = (parsed.get("internal_summary") or "").strip() or build_plain_summary(data)
        return {"text": text, "structured": structured}
    except Exception as exc:
        print(f"AI quote assist failed: {exc}")
        return {"text": build_plain_summary(data), "structured": _empty_structured()}
