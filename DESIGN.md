# Design: Field Blue
**Date:** 2026-06-13 · **Status:** confirmed
**Archetype:** Hero (× Everyman streak) · **Register:** brand (landing) / product (app)
**DNA:** Swiss / International base · **Dominant axis:** layout discipline
**Generated with:** design-for-ai `palette.mjs` — `--seed 250 --chroma vivid --harmony mono`

## Direction
Sharp, confident, no-nonsense — a serious tool a pro would trust. Ruthless clarity:
hierarchy comes from scale, weight, and white space, not decoration. We beat the
clean-SaaS competition at their own game by being *more* disciplined. **AI-powered is
stated up front**, not buried.

## Signature moment  (PROTECTED)
Oversized Archivo numerals on a strict baseline grid (how-it-works steps + feature
indices) — built via large clamp() display weights (800/900) sharing the body baseline
grid, no decoration. The blue accent appears **only** on primary CTAs, the active nav
item, and the logo mark — nowhere else.
PROTECTED: ui-audit enforces this move's a11y / perf / correctness but may NOT remove or
simplify it on taste grounds. If it can't be made conformant, replace it with a different
move of equal boldness — never a safe fallback.

## Type
- Family: **Archivo** (single grotesque — Swiss discipline). Google Fonts.
- Display: Archivo 800/900, tight tracking (-0.02 to -0.03em).
- Body: Archivo 400/500, 17px, leading 1.6.
- Scale: clamp() fluid display; fixed rem body.

## Color tokens (light — primary)
```
--neutral-1:#fcfdfd; --neutral-2:#f8f9fa; --neutral-3:#eef1f3; --neutral-6:#cdd3d9;
--neutral-7:#bec4cb; --neutral-8:#a4acb4; --neutral-11:#5f6469; --neutral-12:#2b2e31;
--accent-9:#0095fe; --accent-10:#0083e0; --accent-11:#2966a0; --accent-on-solid:#070e16;
--success-9:#00dd3e; --success-11:#03791f; --error-9:#ff002b; --error-11:#a93433;
```
(Full ramps + dark scheme in `research/palette-fieldblue.css`.)
Contrast: all pairs PASS WCAG (text 4.5:1, strong 7:1) — solved by construction.

## Space, shape, depth
- Radius: **0** everywhere.
- Depth: **hairline borders only** (`--neutral-6`), NO drop shadows.
- Cards replaced by ruled regions (top/right hairlines).

## Motion
- Timing: micro 120ms, reveal 250ms. Easing: ease-out. Sharp, near-instant.
- Allowed: short opacity/translate reveals; button color states. Never: floating/ambient
  motion, bounce, glow.
- prefers-reduced-motion: disable reveals.

## Never (this project's kill list)
- No dark-mode-by-default + glow (the prior tell).
- No icon+heading+text feature-card grid (use ruled regions + index numerals).
- No floating glassy cards. No drop shadows. No rounded corners.
- Blue used decoratively anywhere beyond CTA / active nav / logo.
- Inter/Roboto/Open Sans. Pure #000/#fff.
