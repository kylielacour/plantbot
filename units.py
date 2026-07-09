#!/usr/bin/env python3
"""Small shared unit helpers (kept dependency-free so any component can import)."""
from __future__ import annotations

ML_PER_CUP = 236.588
ML_PER_TBSP = 14.7868

FRACTIONS = [
    (0.0, ""),
    (0.25, "¼"),
    (1 / 3, "⅓"),
    (0.5, "½"),
    (2 / 3, "⅔"),
    (0.75, "¾"),
    (1.0, ""),
]


def ml_to_cups_str(ml: float) -> str:
    """Render millilitres as a kitchen-friendly measure (ml / tbsp / cups)."""
    if ml < 0.25 * ML_PER_CUP:
        tbsp = ml / ML_PER_TBSP
        if tbsp < 1:
            return f"{int(round(ml))} ml"
        return f"{int(round(tbsp))} tbsp"

    cups = ml / ML_PER_CUP
    whole = int(cups)
    remainder = cups - whole

    frac_value, frac_str = min(FRACTIONS, key=lambda f: abs(remainder - f[0]))

    if frac_value >= 0.99:
        whole += 1
        frac_str = ""

    if whole == 0 and frac_str:
        return f"{frac_str} cup"
    if whole > 0 and frac_str:
        return f"{whole}{frac_str} cups"
    if whole > 0:
        return f"{whole} cup" if whole == 1 else f"{whole} cups"
    return "0 cups"
