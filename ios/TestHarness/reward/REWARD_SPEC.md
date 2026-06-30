# jcode iOS UX Reward Framework

> Goal: turn "this looks ugly / inefficient" into a single, hill-climbable
> reward in [0, 100], decomposed into weighted categories, each backed by an
> objective scorer. A UI change is an improvement **only if it raises the
> weighted reward across the device x content matrix**, not one lucky screen.

## How reward is produced

```
screenshot(s) + source tree + (optional) AX tree / runtime traces
        |
        v   per category, independent scorer -> CategoryScore(0..100, evidence)
   [ scorers ]
        |
        v   weighted sum, normalized
   overall reward (0..100)  +  per-category breakdown  +  worst-cell callout
```

Everything runs headless on this machine via the existing harness
(`mock_gateway.py` scenarios + `xcrun simctl` screenshots). No LLM, no device,
no network.

## Category taxonomy (what matters, UX efficiency first)

Weights sum to 1.0. UX efficiency is weighted highest per the product goal.
Categories A-E are scaled to sum 0.85; category F (design authenticity) adds
0.15. Declared per-scorer weights below already reflect this rebalance.

### A. Space & density (weight 0.255) - "no wasted pixels"
- `space_efficiency`   canvas fill ratio, vertical balance, largest dead zone.
- `information_density` useful content vs chrome (status bar/header/composer).
- `content_safety`     no clipping/overflow/truncation; nothing under chrome.

### B. Ergonomics & interaction (weight 0.2125) - "cheap to use"
- `touch_targets`      interactive elements >= 44x44pt, adequate spacing.
- `reachability`       primary actions in the comfortable thumb zone.
- `interaction_cost`   taps/steps to complete key flows (pair, send, switch
                       session, change model, interrupt).

### C. Visual clarity (weight 0.17) - "easy to parse"
- `visual_hierarchy`   one clear focal point / salient primary action.
- `consistency`        design-token discipline (source) + palette discipline
                       (pixel): few dominant colors, aligned margins.
- `rhythm`             spacing snaps to the 8pt grid (source + pixel).

### D. Legibility & accessibility (weight 0.1275) - "everyone can read it"
- `contrast`           WCAG text/background contrast on real content.
- `accessibility`      VoiceOver labels, Dynamic Type, reduce-motion, semantic
                       roles present in source / AX tree.

### E. Responsiveness (weight 0.085) - "feels instant"
- `layout_robustness`  stable across the device x content matrix (variance of
                       per-cell scores; penalize fragile layouts).
- `perf`               cold-launch-to-first-frame + scroll smoothness signals
                       (best-effort from simctl/Instruments; degrade to N/A).

### F. Design authenticity & craft (weight 0.15) - "designed, not generated"
Higher score = MORE crafted / LESS generic. Grounded in
`reward/AI_SLOP_RESEARCH.md` (documented AI-slop tells).
- `styling`       aesthetic coherence & intent: one dominant color + sparing
                  sharp accent (not a timid rainbow), a real type scale, a
                  consistent radius/elevation system. Source + pixel.
- `simplicity`    anti-complexity: shallow view-nesting depth, few distinct UI
                  primitives per screen, minimal card-in-card nesting, generous
                  purposeful negative space. Source + pixel.
- `ai_patterns`   anti-slop (higher = less slop): penalize purple/indigo/cyan
                  slop palettes, gradient text, glassmorphism/blur overuse,
                  generic fonts (Inter/Roboto/Arial/Space Grotesk), emoji-as-UI,
                  uniform 0.1 shadows, oversized uniform radius, decorative
                  badges. 0 tells = 100. Source primary, pixel corroboration.

## Scorer contract

Every scorer is a Python module under `TestHarness/reward/scorers/<name>.py`
exposing:

```python
NAME = "space_efficiency"      # unique id, matches taxonomy
CATEGORY = "A"                 # taxonomy group letter
WEIGHT = 0.12                  # relative weight within the framework

def score(ctx: "Context") -> "CategoryScore":
    "Pure function: read ctx, return a CategoryScore. No global mutation."
```

- `Context` (provided by `reward/context.py`) gives a scorer everything it may
  need: the screenshot path + decoded numpy array, the device + scenario, the
  px-per-point scale, the source root, an optional AX-tree JSON, and an
  optional runtime-metrics dict. Scorers use only what they need.
- `CategoryScore` (in `reward/types.py`): `{name, category, weight, value:
  0..100, evidence: dict, available: bool}`. `available=False` means "could not
  measure here" (e.g. perf without Instruments); the aggregator drops it and
  renormalizes weights so missing data never silently tanks the reward.
- Scorers must be deterministic and side-effect free. Determinism is enforced
  by `reward/test_determinism.py` (same input -> same output).

## Aggregation

`reward/aggregate.py`:
- discovers all scorer modules,
- runs each over every matrix cell (device x scenario),
- per cell: weighted mean of available categories (weights renormalized),
- overall: mean across cells, plus the single worst cell and worst category,
- emits a JSON report and a human table; supports
  `--baseline-json A --candidate-json B` for regression gating in CI.

## Why this design

- **Parallel-safe:** one file per scorer means swarm workers never edit the
  same file. The contract + `types.py`/`context.py` are the only shared API.
- **Honest:** scorers read rendered pixels / real source, not the view model,
  so they can't be gamed by lying state.
- **Hill-climbable:** the matrix mean is the objective; `--baseline/--candidate`
  makes every change a measurable +/- delta.
- **Extensible:** add a category by dropping in a module; the aggregator finds
  it automatically.
