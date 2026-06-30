"""Reference scorer: A. space_efficiency.

Grades how well the rendered UI uses the canvas: fill ratio, vertical balance,
and the largest empty "dead zone". This is the worked example every other
scorer should follow (NAME / CATEGORY / WEIGHT / pure score()).
"""

from __future__ import annotations

import numpy as np

from reward.context import Context
from reward.types import CategoryScore, make_unavailable

NAME = "space_efficiency"
CATEGORY = "A"
WEIGHT = 0.119


def _longest_run(flags) -> int:
    best = run = 0
    for v in flags:
        run = run + 1 if v else 0
        best = max(best, run)
    return best


def score(ctx: Context) -> CategoryScore:
    mask = ctx.content_mask
    if mask is None:
        return make_unavailable(NAME, CATEGORY, WEIGHT, "no screenshot")

    ch = mask.shape[0]
    fill_ratio = float(mask.mean())

    row_occ = mask.mean(axis=1)
    ys = np.arange(ch)
    occ_sum = row_occ.sum()
    com = float((ys * row_occ).sum() / occ_sum) / ch if occ_sum > 0 else 0.5
    vertical_balance = 1.0 - abs(com - 0.5) * 2.0

    dead = _longest_run(row_occ < 0.01) / ch

    # An efficient chat fills ~30-60% with content reasonably spread. Reward
    # closeness to that band; penalize a large dead zone hard.
    fill_score = 100 * (1 - min(abs(fill_ratio - 0.45) / 0.45, 1.0))
    value = (0.45 * fill_score
             + 0.35 * (vertical_balance * 100)
             + 0.20 * (100 * (1 - dead)))
    value = max(0.0, min(100.0, value))

    return CategoryScore(
        name=NAME, category=CATEGORY, weight=WEIGHT, value=round(value, 2),
        evidence={
            "fill_ratio": round(fill_ratio, 4),
            "vertical_balance": round(vertical_balance, 4),
            "dead_zone_frac": round(dead, 4),
        },
    )
