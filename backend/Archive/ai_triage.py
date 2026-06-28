import os
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
