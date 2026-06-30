"""Scorer A. information_density - useful content vs. chrome.

Space efficiency asks "is the canvas filled?"; this asks "is what fills it
*useful*?". It estimates the share of the content area devoted to the actual
transcript versus fixed chrome (header band at the top, composer band at the
bottom). A dense, efficient screen spends most of its non-empty pixels on the
conversation, not on a tall header or an oversized composer.
"""

from __future__ import annotations

import numpy as np

from reward.context import Context
from reward.types import CategoryScore, make_unavailable

NAME = "information_density"
CATEGORY = "A"
WEIGHT = 0.08

# Approximate chrome band heights as a fraction of the content area. The header
# (title + status pill) and the composer are fixed overhead.
HEADER_FRAC = 0.11
COMPOSER_FRAC = 0.11


def score(ctx: Context) -> CategoryScore:
    mask = ctx.content_mask
    if mask is None:
        return make_unavailable(NAME, CATEGORY, WEIGHT, "no screenshot")

    ch = mask.shape[0]
    header_end = int(ch * HEADER_FRAC)
    composer_start = int(ch * (1 - COMPOSER_FRAC))

    row_occ = mask.mean(axis=1)
    total = float(row_occ.sum()) + 1e-9

    header_content = float(row_occ[:header_end].sum())
    composer_content = float(row_occ[composer_start:].sum())
    transcript_content = float(row_occ[header_end:composer_start].sum())

    # Share of all content pixels that live in the transcript region.
    transcript_share = transcript_content / total
    chrome_share = (header_content + composer_content) / total

    # A healthy chat spends most ink on the transcript. But a totally empty
    # transcript (everything in chrome) is bad; reward transcript_share while
    # requiring the transcript region to actually contain something.
    transcript_region_occ = float(row_occ[header_end:composer_start].mean())

    # Blend: 70% transcript share of ink, 30% transcript region occupancy.
    value = 100 * (0.7 * transcript_share + 0.3 * min(transcript_region_occ * 3, 1.0))
    value = max(0.0, min(100.0, value))

    return CategoryScore(
        name=NAME, category=CATEGORY, weight=WEIGHT, value=round(value, 2),
        evidence={
            "transcript_share": round(transcript_share, 4),
            "chrome_share": round(chrome_share, 4),
            "transcript_region_occ": round(transcript_region_occ, 4),
        },
    )
