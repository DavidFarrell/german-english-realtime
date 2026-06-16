# Prompt for Claude Design - German<->English live translator UX

(Copy everything in the block below and give it to Claude Design.)

---

You are designing the UX and visuals for a small desktop app. Imagine and lay out the full
experience as high-fidelity screens (and the key interaction states), not code.

## Use my design system
Use the **Paidia "Riso Zine Funky"** design system you have access to
(`~/git/ai-sandbox/projects/DesignSystems/PaidiaRhisoZineFunky/` - tokens, components, fonts,
ui_kits). It's the deep-purple risograph-zine look: purple paper, two fluoro spot inks,
Archivo Black at poster scale, a deliberate hand-printed wobble. Everything below should be
rendered in that system - reuse its tokens/components, don't invent a new palette.

## What the app is
A real-time, two-person German<->English speech translator for a face-to-face conversation.
Two people each clip on a wireless lapel mic and each wear one earbud; each person speaks their
own language and hears the other person, translated, in their own ear, near-instantly. It runs
on Google's Gemini Live Translate. Think "personal UN interpreter for two people at a table."

## The hardware reality that shapes the UI (important - the screens map to this)
- The mic is a **DJI Mic 3**: one USB-C receiver + two wireless transmitters. In stereo mode it
  shows up to the Mac as ONE input device (it appears as **"Wireless Mac RX"**) with two mono
  channels: **transmitter 1 = LEFT, transmitter 2 = RIGHT**. So "left input" and "right input"
  literally are the two clip-on mics - one per person.
- Output is **AirPods (one bud per person)**: it's one stereo Bluetooth output, and we send a
  different language to each bud - **left bud and right bud go to two different people**.
- Language detection is automatic, so you probably DON'T need an "input language" picker. What
  the app genuinely needs is **which language each person should HEAR** (the target language for
  their earbud). So a per-person language choice doubles as "who is this / what do they hear."
  Design for that; only add an explicit input-language control if it makes the flow clearer.

## The experience to design
Open with a **splash screen** - a nice branded landing moment. Leave a hero image area as a
placeholder (a custom image will be dropped in later); design around it.

Then a **setup wizard**. My strong instinct is that it's **one screen made of stacked rows
(an accordion)**: the active row is large; once you finish a row it **shrinks to a compact
summary** and the next row expands; you can **click any shrunk row to expand it again**. (If
you think separate screens work better, propose that - but I like the single-expanding-canvas
feel.) The rows:

**Row 1 - INPUTS (the two microphones).** Two boxes side by side, **LEFT** and **RIGHT**, each
showing a **live waveform** of that mono input. Each box shows the **currently selected
microphone**, defaulting to **"Wireless Mac RX"** (the DJI). You can **test each mic** (see it
move / confirm it works) and adjust **overall mic gain**. Optionally **name who's who** (e.g.
"David", "Debbie") or just leave them Left/Right and clip the mics onto people. Optionally set
each person's language here (see the note above). Then advance.

**Row 2 - OUTPUTS (the two earbuds).** Row 1 shrinks to a summary; Row 2 expands. Same shape:
two boxes, **LEFT** and **RIGHT**, showing the two output channels separately, with the
**currently selected output device** (defaulting to **AirPods**), changeable. Optionally name
these people too. The point of this row is letting you get your head around which bud is which
side / which person.

**Row 3 - CHANNEL TEST (am I wired up right?).** A self-check that each person's mic + earbud
are on the correct side. It asks **person 1 to speak into their own mic**, and plays their own
voice back into **their own earbud only** with about a **half-second delay** - "Hi, my name is
David" comes back in David's ear, NOT in the other person's. Then the other person can do the
same if they want. It's just a reassuring "I can hear myself" loopback, per person.

**Final - LIVE TEST (prove the translation works).** A **Start** button spins up the live
translation. Maybe it suggests a phrase to say, or you just say something yourself; it speaks
your line, translated, into the other person's ear; they reply and you hear them translated.
That proves the two-way channel. After that you can just leave it running. This might be the
last row of the same expanding canvas, or a final state - your call.

## Things to get right
- The **live waveforms** are central - they're how a non-technical user trusts "yes, my voice
  is going in" and "yes, sound is coming out." Make them prominent and lively.
- **Clarity of left/right and who's who** throughout - this app is all about not getting the two
  people's channels crossed.
- Sensible **defaults** (Wireless Mac RX in, AirPods out) so a confident user can breeze through.
- Friendly, low-stress, "it just works" wizard tone - the users are two people about to have a
  conversation, possibly not technical (e.g. me and my German-speaking sister-in-law).

## Deliverable
Imagine and present: the splash screen; the wizard in its key states (each row both expanded
and in its shrunk-summary form); the channel-test interaction; and the live/running state. Show
the accordion expand/shrink behaviour. Desktop app, single window, macOS. Use Riso Zine Funky
throughout. The splash hero image is a placeholder to be supplied later.
