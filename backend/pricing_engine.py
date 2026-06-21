from dataclasses import dataclass


@dataclass
class PricingSettings:
    kwh: float = 0.31
    watts: float = 180
    spool_usd: float = 20
    spool_g: float = 1000
    nozzle_cost: float = 8
    nozzle_hours: float = 400
    sheet_cost: float = 25
    sheet_prints: float = 500
    shipping: float = 5
    boxing: float = 1.50
    tax: float = 6.25
    markup: float = 300
    labor_rate: float = 35


def money(value: float) -> float:
    return round(float(value), 2)


def build_customer_quote_text(quantity, hours, grams, suggested_sell_per_unit, total_sell, include_shipping):
    if quantity > 1:
        price_line = f"Estimated quote: ${total_sell:.2f} total (${suggested_sell_per_unit:.2f} each)"
    else:
        price_line = f"Estimated quote: ${suggested_sell_per_unit:.2f}"

    shipping_line = "Shipping is included in this estimate." if include_shipping else "Shipping/pickup will be handled separately."

    return (
        f"{price_line}\n\n"
        f"Estimated print time: {hours * quantity:.1f} hours total\n"
        f"Estimated material use: {grams * quantity:.1f}g total\n"
        f"{shipping_line}\n\n"
        "This quote is based on the information provided and may change if the file requires design repair, resizing, support-heavy printing, or additional finishing."
    )


def calculate_quote(
    grams: float,
    hours: float,
    quantity: int = 1,
    fail_rate: float = 0,
    labor_minutes: float = 0,
    cad_fee: float = 0,
    rush_fee: float = 0,
    complexity_multiplier: float = 1.0,
    include_shipping: bool = True,
    settings: PricingSettings | None = None,
) -> dict:
    settings = settings or PricingSettings()
    quantity = max(1, int(quantity or 1))

    if grams <= 0:
        raise ValueError("grams must be greater than 0")
    if hours <= 0:
        raise ValueError("hours must be greater than 0")
    if fail_rate < 0 or fail_rate >= 100:
        raise ValueError("fail_rate must be between 0 and 99.99")
    if complexity_multiplier <= 0:
        raise ValueError("complexity_multiplier must be greater than 0")

    cost_per_g = settings.spool_usd / settings.spool_g
    filament_cost = grams * cost_per_g

    kwh_used = (settings.watts / 1000) * hours
    electricity_cost = kwh_used * settings.kwh

    nozzle_per_hour = settings.nozzle_cost / settings.nozzle_hours if settings.nozzle_hours > 0 else 0
    nozzle_wear = nozzle_per_hour * hours

    sheet_per_print = settings.sheet_cost / settings.sheet_prints if settings.sheet_prints > 0 else 0
    sheet_wear = sheet_per_print

    packaging_cost = settings.boxing
    shipping_cost = settings.shipping if include_shipping else 0
    labor_cost = (labor_minutes / 60) * settings.labor_rate

    direct_cost_per_unit = (
        filament_cost + electricity_cost + nozzle_wear + sheet_wear +
        packaging_cost + shipping_cost + labor_cost + cad_fee + rush_fee
    )

    fail_multiplier = 1 / (1 - fail_rate / 100)
    cost_with_fail_per_unit = direct_cost_per_unit * fail_multiplier
    fail_overhead_per_unit = cost_with_fail_per_unit - direct_cost_per_unit

    adjusted_cost_per_unit = cost_with_fail_per_unit * complexity_multiplier
    tax_per_unit = adjusted_cost_per_unit * (settings.tax / 100)
    suggested_sell_per_unit = adjusted_cost_per_unit * (1 + settings.markup / 100)
    profit_per_unit = suggested_sell_per_unit - adjusted_cost_per_unit

    total_cost = adjusted_cost_per_unit * quantity
    total_tax = tax_per_unit * quantity
    total_sell = suggested_sell_per_unit * quantity
    total_profit = profit_per_unit * quantity

    return {
        "quantity": quantity,
        "inputs": {
            "grams": grams,
            "hours": hours,
            "fail_rate": fail_rate,
            "labor_minutes": labor_minutes,
            "cad_fee": cad_fee,
            "rush_fee": rush_fee,
            "complexity_multiplier": complexity_multiplier,
            "include_shipping": include_shipping,
        },
        "per_unit": {
            "grams": round(grams, 2),
            "hours": round(hours, 2),
            "cost_to_make": money(adjusted_cost_per_unit),
            "tax": money(tax_per_unit),
            "suggested_sell_price": money(suggested_sell_per_unit),
            "profit": money(profit_per_unit),
        },
        "totals": {
            "grams": round(grams * quantity, 2),
            "hours": round(hours * quantity, 2),
            "cost_to_make": money(total_cost),
            "tax": money(total_tax),
            "suggested_sell_price": money(total_sell),
            "profit": money(total_profit),
        },
        "breakdown_per_unit": {
            "filament": money(filament_cost),
            "electricity": money(electricity_cost),
            "nozzle_wear": money(nozzle_wear),
            "print_sheet_wear": money(sheet_wear),
            "packaging": money(packaging_cost),
            "shipping": money(shipping_cost),
            "labor": money(labor_cost),
            "cad_fee": money(cad_fee),
            "rush_fee": money(rush_fee),
            "fail_overhead": money(fail_overhead_per_unit),
            "complexity_added_cost": money(adjusted_cost_per_unit - cost_with_fail_per_unit),
        },
        "customer_quote": build_customer_quote_text(
            quantity, hours, grams, suggested_sell_per_unit, total_sell, include_shipping
        ),
    }
