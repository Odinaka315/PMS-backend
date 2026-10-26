"""The arithmetic of a payslip, kept free of the database so it can be tested on its own.
All amounts are whole kobo. Percentages are in basis points (2500 = 25%) and round half up."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Component:
    code: str
    name: str
    kind: str  # earning | deduction
    method: str  # fixed | percent_of_basic | percent_of_gross
    amount_kobo: int | None
    rate_bps: int | None
    min_level: int | None
    max_level: int | None
    sort_order: int = 100


@dataclass(frozen=True)
class Extra:
    """A person-specific adjustment already filtered to the period being calculated."""
    id: str
    kind: str
    name: str
    amount_kobo: int


def percent(base_kobo: int, rate_bps: int) -> int:
    return (base_kobo * rate_bps + 5000) // 10000


def applies(c: Component, level: int | None) -> bool:
    if c.min_level is None and c.max_level is None:
        return True
    if level is None:
        return False
    return (c.min_level is None or level >= c.min_level) and (c.max_level is None or level <= c.max_level)


def compute(basic_kobo: int, level: int | None, components: list[Component], extras: list[Extra]):
    """Returns (lines, gross, deductions, net). Earnings are worked out first, then deductions, because some
    deductions are a share of gross pay."""
    ordered = sorted((c for c in components if applies(c, level)), key=lambda c: (c.sort_order, c.code))
    lines = [{"code": "BASIC", "name": "Basic salary", "kind": "earning", "amount_kobo": basic_kobo}]

    def amount(c: Component, gross: int) -> int:
        if c.method == "fixed":
            return c.amount_kobo or 0
        if c.method == "percent_of_basic":
            return percent(basic_kobo, c.rate_bps or 0)
        return percent(gross, c.rate_bps or 0)

    for c in ordered:
        if c.kind == "earning":
            lines.append({"code": c.code, "name": c.name, "kind": "earning", "amount_kobo": amount(c, 0)})
    for e in extras:
        if e.kind == "earning":
            lines.append({"code": "ADJ", "name": e.name, "kind": "earning", "amount_kobo": e.amount_kobo})
    gross = sum(line["amount_kobo"] for line in lines)

    for c in ordered:
        if c.kind == "deduction":
            lines.append({"code": c.code, "name": c.name, "kind": "deduction", "amount_kobo": amount(c, gross)})
    for e in extras:
        if e.kind == "deduction":
            lines.append({"code": "ADJ", "name": e.name, "kind": "deduction", "amount_kobo": e.amount_kobo})
    deductions = sum(line["amount_kobo"] for line in lines if line["kind"] == "deduction")
    return lines, gross, deductions, gross - deductions
