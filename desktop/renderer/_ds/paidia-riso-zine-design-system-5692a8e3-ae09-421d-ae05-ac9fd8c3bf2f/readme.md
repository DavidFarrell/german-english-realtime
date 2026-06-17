# Paidia · Riso Zine Funky — Design System

> *A risograph zine that learned to give a talk. Deep purple paper, two fluoro
> spot inks - hot yellow and riso pink - Archivo Black at poster scale, and a
> deliberate hand-printed wobble. Built first for slide decks, and equally for
> the web.*

This is the canonical, self-hosted **"Riso Zine Funky"** direction David landed
on while iterating on Paidia Consulting (an AI-enablement / training / coaching
practice, with a tabletop-games studio on the side). The brief was to escape the
"generic AI-generated website" look: bold, confident, dark, massive type, an
accent that pops. The system is **deck-first** - its native medium is a
1920×1080 slide stage - but it ships a full web component layer so the same
voice can build a site or an app.

There are two cuts. This system encodes the **funky** cut as canonical (small
rotations, a halftone dot wash, hard offset block-shadows). A calmer **straight**
cut is one attribute away (`data-rz-cut="straight"`) for buttoned-up rooms. David's verdict,
and the reason it is called Funky: *"I do prefer the funky version, but I think
people aren't ready for funk."*

---

## Sources

This design system was ported from an existing, fully-authored handoff bundle
(`PaidiaRhisoZineFunky/`), itself distilled from two Claude Design sessions
(5 May and 11 May 2026). Key provenance, in case the reader has access:

| Source | What it was |
|---|---|
| `PaidiaRhisoZineFunky/reference/original_deck.html` | The funky `rhiso_zine_deck_two.html` - 31 slides, the canonical reference. |
| `PaidiaRhisoZineFunky/reference/original_deck_straight.html` | The calmer straight cut (rotations/halftone/round-corners removed). |
| `PaidiaRhisoZineFunky/reference/design-intent.md` | Provenance + the exact colour/voice decisions. |
| `PaidiaRhisoZineFunky/src/*.css` | The original token + deck + web CSS this system re-organises. |

The brand is **purely typographic** - there are no logo image files,
illustrations or photographs in the source. The "PAIDIA" wordmark is simply
Archivo Black text. See ICONOGRAPHY below.

---

## Index — what lives where

```
styles.css                  ← the ONE file web consumers link (imports only)
tokens/
  fonts.css                 @font-face — Archivo, Archivo Black, JetBrains Mono (local .woff2)
  colors.css                the four-ink palette + rules + alphas
  typography.css            type families + deck (px) and web (clamp) scales + weights
  spacing.css               8-px spacing, layout, deck geometry, radius
  funk.css                  rotation / offset-shadow / halftone + the straight-cut switch
  semantic.css              fg/bg/accent aliases over the --rz-* set
styles/
  components.css            the WEB .rz-* component classes
  base.css                  element defaults (html/body/h1–h6/p/a) for web
  deck.css                  the DECK .rz-* slide vocabulary (NOT shipped via styles.css)
fonts/                      6 woff2 (Archivo ×2, Archivo Black ×2, JetBrains Mono ×2)
runtime/
  deck-stage.js             <deck-stage> web component (auto-scale, nav, rail, Print→PDF)
  image-slot.js             <image-slot> fillable-image component
  lightbox.js               click-to-zoom image lightbox (default behaviour)
components/
  actions/                  Button, Chip
  surfaces/                 Card, Plate, Stat
  content/                  Eyebrow, Mark, BuildList, CodeBlock
guidelines/                 foundation specimen cards (Colors, Type, Spacing, Brand)
ui_kits/
  homepage/                 a Paidia marketing page built from the primitives
  deck/                     a working 11-slide funky deck (every archetype)
slides/                     individual slide-archetype cards (title, divider, build-list, …)
SKILL.md                    agent-skill manifest (download-and-use in Claude Code)
```

**Components** (read via `window.PaidiaRisoZineDesignSystem_5692a8`):
`Button`, `Chip` · `Card`, `Plate`, `Stat` · `Eyebrow`, `Mark`, `BuildList`,
`CodeBlock`.

### Two stacks — never mix them

`styles/deck.css` and `styles/components.css` share class names by design
(`.rz-display`, `.rz-list`, `.rz-chip`, `.rz-code`). Loading both on one page is
order-dependent and will drift. So:

- **Web / app:** link `styles.css` (it pulls tokens + `components.css` + `base.css`).
- **Deck:** link the deck stack directly - `tokens/fonts.css → colors → typography
  → spacing → funk → styles/deck.css` - plus the `runtime/` scripts. (See
  `ui_kits/deck/index.html`.) `styles.css` does **not** import `deck.css`.

---

## CONTENT FUNDAMENTALS

The system is **loud in the eye, quiet in the mouth.** The visuals shout; the
words stay plain.

### Casing
- **Display headlines** - sentence case, set huge and tight in Archivo Black,
  broken across lines by hand. One or two words get a spot-ink highlighter.
  *"Treat your AI like a `person`."*
- **Eyebrows / kickers** - `ALL CAPS, TRACKED` (~0.18em) in JetBrains Mono. Pink
  default, yellow alternate. Clauses separated by ` · ` (mid-dot), not commas.
- **Footers** - section name (bottom-left, yellow) + page number (bottom-right,
  tabular). Mono, all-caps. No section numbers, no top-corner chrome.
- **Body** - sentence case, Archivo, generous line-height.
- **Buttons / chips** - `UPPERCASE MONO`, tracked.

### Tone
- **Plain and declarative.** Short sentences. The visual system carries the
  energy; the copy stays calm. Dry over wry.
- **No marketing-speak.** No "unlock", "supercharge", "drop it in", "lands hard",
  "best-in-class", "synergy". If a sentence sounds like a SaaS hero, rewrite it flat.
- **Emphasis is a highlighter, not a shout.** Reach for `Mark` / `.rz-mark`,
  never bold-italic-underline pile-ups, and never an exclamation mark.
- **British English.** Colour, organise, programme.
- Numbers and concrete nouns do the persuading: *17 min*, *33 sources*, *4,300 words*.

### Mechanics — hard rules
- **No em dashes. Ever.** Use ` - ` (space-hyphen-space). (The `.rz-dash` glyph in
  a build list is page furniture, not prose punctuation.)
- **No `§` symbols. No emoji.**
- Arrow vocabulary is `→` and `›` (`&rsaquo;`) only, set in mono.

---

## VISUAL FOUNDATIONS

### Colour — the four-ink discipline
A real risograph runs a few spot inks, one pass each. This system pretends to be
exactly that: **four inks, no more.**

| Role | Token | Hex |
|---|---|---|
| Paper (default plate) | `--rz-purple` | `#2b1f4f` |
| Paper deep (alternate) | `--rz-purple-deep` | `#1a1438` |
| Off-white (type, light plates) | `--rz-paper` | `#fbf3d8` |
| Spot ink 1 (primary) | `--rz-yellow` | `#fff200` |
| Spot ink 2 (secondary) | `--rz-pink` | `#ff5fa2` |
| Ink-on-spot (type on a plate) | `--rz-ink` | `#1a1438` |
| Plum (chat "you:" only) | `--rz-plum` | `#7a1f5a` |

Type on a spot/paper plate is **always the deep purple, never black** - that keeps
the four-ink illusion intact. Yellow/pink type is for plates and for type *on
purple* only; on light surfaces they fail contrast. Dividers and muted type are
paper at low alpha (`--rz-rule`, `--rz-paper-70`…), never a separate grey.
**Never reintroduce terracotta / clay orange** - the tell-tale "AI default"
colour the whole direction was built to avoid. **Never add a third spot ink.**

### Typography
Three families, each with one job. Archivo Black is display only, set TIGHT
(line-height 0.84–0.92, tracking -0.03 to -0.045em, lines overlapping). Archivo
(variable, pushed to 700–900) is the body/UI workhorse. JetBrains Mono carries
all the chrome - eyebrows, footers, captions, chips, code. Deck sizes are
absolute px on the 1920×1080 stage (`--rz-d-*`); web sizes are fluid `clamp()`
(`--rz-fs-*`). Same proportions, two scales.

### The funk — three moves (use the tokens, don't freehand)
1. **Rotation.** Every plate sits a degree or two off-true (`--rz-rot-xs … -xl`,
   -0.6° to -3°). Tiles, chips, stamps, highlighted words.
2. **Riso offset shadow.** A hard, **un-blurred** spot-ink block behind a figure -
   the mis-registration look (`--rz-offset-pink` = `8px 8px 0 0` pink). Never a
   soft `rgba` drop shadow.
3. **Halftone wash.** A faint dot screen over a slide/section, yellow or pink, at
   8–18% opacity (`.rz-halftone` deck / `.rz-wash` web).

### Backgrounds, borders, shadows, radius
- **Backgrounds** are flat colour - the two purples, or a spot-ink plate. No
  gradients. The only texture is the halftone dot wash at low opacity.
- **No soft drop shadows anywhere.** Layering is done with the deeper purple
  plate, a spot-ink offset block, or a hairline (`--rz-rule` / `-strong`).
- **Borders** are 1–1.5px hairlines at paper-alpha on purple, or ink-alpha on a
  paper plate.
- **Corners** are square-ish: `8px` (`--rz-radius`) on plates/cards/code, full
  pill on chips, `4px` small, `0` in the straight cut.
- **Cards:** deep-purple surface + hairline border + 8px radius (`Card`), or a
  flat rotated spot-ink plate (`Plate`). No shadow on either - the Plate's depth
  comes from rotation, the figure's from the offset block.

### Motion, hover, press
- Easing is `cubic-bezier(0.2, 0.8, 0.2, 1)`; durations 0.14s / 0.28s
  (`--rz-dur-fast` / `-base`). Zeroed under `prefers-reduced-motion`.
- **Hover:** buttons un-rotate and scale up slightly (`rotate(0) scale(1.03)`);
  link-cards lift and take a playful tilt (`translateY(-4px) rotate(-0.5deg)`);
  nav/links shift to yellow.
- **Focus:** a 3px **yellow** outline with 3px offset - reads on every plate.
- There are no bounces and no decorative looping animations on content.

### Layout
- **Decks:** fixed 1920×1080, slide padding `88px 104px 64px`, content centred or
  top-aligned. Default content grid is an asymmetric `5fr / 6fr`; `grid-2`/`grid-3`
  for tiles.
- **Web:** container max `1280px`, fluid side padding, a 12-column `.rz-grid`
  (drive columns with `--rz-cols` / `--rz-cols-mobile`).

### The straight cut
Put `data-rz-cut="straight"` on `<html>`/`<body>` (web or deck).
It zeroes every funk token at once - all rotations, the skew, all radii, both
halftone opacities, and the offset block-shadows - leaving palette and type
untouched. The live-dot stays a true circle. Defined at the bottom of
`tokens/funk.css`.

---

## ICONOGRAPHY

This brand **does not use an icon set.** There are no SVG icons, no icon font, no
PNG glyphs in the source, and **no emoji** (emoji are explicitly banned). The
visual signalling is carried entirely by:

- **Mono "furniture" glyphs**, set in JetBrains Mono: the build-list dash (`—`,
  `.rz-dash`, pink) and arrow (`›` = `&rsaquo;` / `→`, `.rz-arrow`, yellow), the
  numeral markers (`01.`), and the live-dot (a CSS circle, `.rz-live::before`).
- **Numerals as markers** - big rotated Archivo Black numbers (`.rz-num-marker`)
  in place of bullet icons.
- **Spot-ink chips and highlighters** instead of badge/label icons.

If a future surface genuinely needs line icons, add a single CDN set with a thin,
geometric stroke (e.g. Lucide / Phosphor at ~1.5px) and flag the addition - it is
**not** part of the current system, which earns its structure from type and
colour alone. The "PAIDIA" wordmark is set in Archivo Black; there is no separate
logo file.

---

## How to build

**A web page** - link `styles.css`, put `class="rz"` on `<body>`, compose the
components or the `.rz-*` classes. See `ui_kits/homepage/`.

**A deck** - copy `ui_kits/deck/index.html`, edit the `<section>`s. The runtime
auto-numbers footers, drives nav (←/→, Space, Home/End, number keys), the
thumbnail rail, and Print → Save as PDF (one slide per page). Footer page numbers
are automatic - leave `<span class="rz-foot__page"></span>` empty.

**Default behaviours** (both): click-to-zoom image lightbox (`runtime/lightbox.js`,
load last; opt a single image out with `data-no-zoom`) and automatic deck footer
numbers.

---

## Do / don't
- **Do** keep to four inks: two purples + yellow + pink. One off-white for type.
- **Do** carry emphasis with a spot-ink highlighter, never italics or shouting.
- **Do** use the rotation / offset-shadow / halftone tokens for the funk.
- **Do** keep deck chrome to a clean footer; top corners stay empty.
- **Do** write flat, declarative copy. British English.
- **Don't** reintroduce terracotta / clay orange, or add a third spot ink.
- **Don't** use em dashes (` - ` instead), `§` symbols, or emoji.
- **Don't** add soft drop shadows - use the deeper plate, an offset block, or a hairline.
- **Don't** set the display face loose - Archivo Black is always tight and big.
- **Don't** set yellow or pink type on a light plate (it fails contrast).
- **Don't** load `deck.css` and `components.css` on the same page.

## A note on the fonts
All three families are **SIL Open Font License**, safe to bundle and ship in
client work, self-hosted from `fonts/` as `.woff2` (latin + latin-ext). No CDN
call. These are the real brand fonts from the source bundle - no substitution
was needed.
