"""B. interaction_cost - taps/steps to complete key flows (STATIC).

"Cheap to use" means the things you do constantly cost as few taps as possible.
This scorer is *static*: it reads the SwiftUI source (no screenshot needed) and
estimates the tap depth of each key flow against a small rubric, then rewards
shallow flows and penalizes deep nesting (e.g. a flow buried behind a sheet
inside another sheet).

Flows graded (jcode iOS):
  * send         - type in the composer + tap send. The composer is always on
                   the chat screen, so 1 tap once the draft exists.
  * interrupt    - stop a running turn. 1 tap iff a visible stop button exists
                   in the composer; otherwise effectively unreachable.
  * switch_session - change the active session. Lives in the Settings sheet, so
                   open Settings (1) + tap a session row (1) = 2 taps.
  * change_model - pick a model. Same Settings sheet, model section: 2 taps,
                   plus a scroll penalty if it sits below other sections.
  * pair_server  - add a new server. Settings (1) -> "Pair new server" (1) ->
                   nested pairing sheet -> Pair (1) = 3 taps (deeper sheet).

Each flow maps to a tap-cost -> a 0..100 sub-score (fewer taps = higher). The
final value is the weighted mean of the flows. Deterministic + pure: same
source -> same score. If there is no source, returns unavailable.
"""

from __future__ import annotations

import re

from reward.context import Context
from reward.types import CategoryScore, make_unavailable

NAME = "interaction_cost"
CATEGORY = "B"
WEIGHT = 0.068

# Tap-cost -> score. 1 tap is ideal (100); each extra tap costs ~22 points;
# anything 5+ taps deep, or unreachable, bottoms out near 0.
def _taps_to_score(taps: float) -> float:
    if taps <= 0:
        return 0.0   # unreachable / not found
    return max(0.0, min(100.0, 100.0 - (taps - 1.0) * 22.0))


# Per-flow weights: the daily-driver flows (send, interrupt, switch) matter most.
FLOW_WEIGHTS = {
    "send": 0.30,
    "interrupt": 0.20,
    "switch_session": 0.22,
    "change_model": 0.18,
    "pair_server": 0.10,
}


def _has(files: dict[str, str], pattern: str) -> bool:
    rx = re.compile(pattern)
    return any(rx.search(t) for t in files.values())


def _count_sheet_nesting(files: dict[str, str], anchor: str) -> int:
    """How many `.sheet(`/`fullScreenCover(` layers wrap the file containing
    `anchor`. A control inside one sheet is depth 1, inside a sheet-in-a-sheet
    depth 2. Used to add a nesting penalty to a flow's tap cost."""
    depth = 0
    for text in files.values():
        if anchor in text:
            depth = max(depth, text.count(".sheet(") + text.count("fullScreenCover("))
    return depth


def score(ctx: Context) -> CategoryScore:
    files = ctx.source_files
    if not files:
        return make_unavailable(NAME, CATEGORY, WEIGHT, "no source files")

    # --- detect the building blocks --------------------------------------
    # Composer with an explicit send action on the chat screen.
    has_composer = _has(files, r"struct Composer\b")
    has_send = _has(files, r"onSend") and _has(files, r'Image\(systemName:\s*"arrow\.up"')
    # Visible stop/interrupt button (only shown while processing, but it is a
    # single tap when present).
    has_stop_button = _has(files, r'Image\(systemName:\s*"stop\.fill"')
    has_interrupt = _has(files, r"onInterrupt|func interrupt")
    # Settings sheet hosts session + model pickers.
    settings_is_sheet = _has(files, r"\.sheet\(isPresented:[^)]*\)\s*\{\s*\n?\s*SettingsView")
    has_session_switch = _has(files, r"switchSession")
    has_model_switch = _has(files, r"setModel|func setModel")
    has_pair_new = _has(files, r"showPairNew|Pair new server")

    # --- tap-cost rubric --------------------------------------------------
    taps: dict[str, float] = {}

    # send: composer always on chat screen -> 1 tap (draft already typed).
    taps["send"] = 1.0 if (has_composer and has_send) else 0.0

    # interrupt: visible stop button -> 1 tap. If only a programmatic interrupt
    # exists with no button, treat as harder (3 taps via a menu).
    if has_stop_button:
        taps["interrupt"] = 1.0
    elif has_interrupt:
        taps["interrupt"] = 3.0
    else:
        taps["interrupt"] = 0.0

    # switch_session: open Settings sheet (1) + tap a session row (1) = 2.
    if has_session_switch:
        base = 2.0 if settings_is_sheet else 1.0
        taps["switch_session"] = base
    else:
        taps["switch_session"] = 0.0

    # change_model: Settings (1) + model row (1) = 2, +1 if the Model section is
    # rendered below Sessions (must scroll past it in the List).
    if has_model_switch:
        base = 2.0 if settings_is_sheet else 1.0
        model_below_sessions = _model_section_below_sessions(files)
        taps["change_model"] = base + (1.0 if model_below_sessions else 0.0)
    else:
        taps["change_model"] = 0.0

    # pair_server: Settings (1) -> "Pair new server" (1) -> nested pairing sheet
    # -> Pair button (1) = 3, deeper because it is a sheet within a sheet.
    if has_pair_new:
        nesting = _count_sheet_nesting(files, "showPairNew")
        taps["pair_server"] = 2.0 + max(1.0, float(nesting - 1))
    else:
        taps["pair_server"] = 0.0

    # --- aggregate --------------------------------------------------------
    flow_scores = {k: round(_taps_to_score(v), 2) for k, v in taps.items()}
    wsum = sum(FLOW_WEIGHTS.values())
    value = sum(flow_scores[k] * FLOW_WEIGHTS[k] for k in FLOW_WEIGHTS) / wsum
    value = max(0.0, min(100.0, value))

    return CategoryScore(
        name=NAME, category=CATEGORY, weight=WEIGHT, value=round(value, 2),
        evidence={
            "taps_per_flow": {k: round(v, 1) for k, v in taps.items()},
            "flow_scores": flow_scores,
            "settings_is_sheet": bool(settings_is_sheet),
            "has_visible_stop_button": bool(has_stop_button),
            "model_below_sessions": _model_section_below_sessions(files),
            "rubric": "1 tap=100, -22/extra tap, 0 if unreachable",
        },
    )


def _model_section_below_sessions(files: dict[str, str]) -> bool:
    """True if the Model section is declared after the Sessions section in the
    Settings List, meaning the user scrolls past Sessions to reach it."""
    for text in files.values():
        s = text.find('Section("Sessions")')
        m = text.find('Section("Model")')
        if s != -1 and m != -1:
            return m > s
    return False
