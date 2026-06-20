from dataclasses import dataclass
from math import isfinite


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
    """
    Pricing engine based on the existing C3D Prints calculator logic.

    This returns both cost-to-make and suggested sell price.
    """

    settings = settings or PricingSettings()
    quantity = max(1, int(quantity or 1))

    if grams <= 0:
        raise ValueError("grams must be greater than 0")
    if hours <= 0:
        raise ValueError("hours must be greater than 0")
    if fail_rate < 0 or fail_rate >= 100:
        raise ValueError("fail_rate must be between 0 and 99.99")

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

    base_cost_per_unit = (
        filament_cost
        + electricity_cost
        + nozzle_wear
        + sheet_wear
        + packaging_cost
        + shipping_cost
        + labor_cost
        + cad_fee
        + rush_fee
    )

    fail_multiplier = 1 / (1 - fail_rate / 100)
    adjusted_cost_per_unit = base_cost_per_unit * fail_multiplier
    fail_overhead_per_unit = adjusted_cost_per_unit - base_cost_per_unit

    cost_after_complexity_per_unit = adjusted_cost_per_unit * complexity_multiplier

    tax_per_unit = cost_after_complexity_per_unit * (settings.tax / 100)
    suggested_sell_per_unit = cost_after_complexity_per_unit * (1 + settings.markup / 100)
    profit_per_unit = suggested_sell_per_unit - cost_after_complexity_per_unit

    return {
        "quantity": quantity,
        "grams_per_unit": round(grams, 2),
        "hours_per_unit": round(hours, 2),
        "total_grams": round(grams * quantity, 2),
        "total_hours": round(hours * quantity, 2),
        "cost_per_unit": round(cost_after_complexity_per_unit, 2),
        "cost_total": round(cost_after_complexity_per_unit * quantity, 2),
        "tax_per_unit": round(tax_per_unit, 2),
        "tax_total": round(tax_per_unit * quantity, 2),
        "suggested_sell_per_unit": round(suggested_sell_per_unit, 2),
        "suggested_sell_total": round(suggested_sell_per_unit * quantity, 2),
        "profit_per_unit": round(profit_per_unit, 2),
        "profit_total": round(profit_per_unit * quantity, 2),
        "breakdown_per_unit": {
            "filament": round(filament_cost, 2),
            "electricity": round(electricity_cost, 2),
            "nozzle_wear": round(nozzle_wear, 2),
            "sheet_wear": round(sheet_wear, 2),
            "packaging": round(packaging_cost, 2),
            "shipping": round(shipping_cost, 2),
            "labor": round(labor_cost, 2),
            "cad_fee": round(cad_fee, 2),
            "rush_fee": round(rush_fee, 2),
            "fail_overhead": round(fail_overhead_per_unit, 2),
            "complexity_multiplier": complexity_multiplier,
        },
    }
