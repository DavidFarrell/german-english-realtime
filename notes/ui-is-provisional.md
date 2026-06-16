# READ ME FIRST (build instance): the UI is PROVISIONAL - real designs are coming

**For the Claude Code / ultracode instance that builds this app.**

David is separately producing **product wireframes and an interactive prototype in Claude
Design** (using the Paidia "Riso Zine Funky" design system). He will share those with you. They
- not anything you invent - are the **source of truth for the visuals and UX**.

## What this means for how you build

**Do NOT overcommit to visuals. Keep the UI thin, plain, and trivially swappable.**

1. **Build the core first, UI last and minimal.** The durable, valuable part is the
   audio/translation engine: `contracts.py`, the audio I/O, the Live-session wrapper, the
   latency harness. Get that working with the most minimal UI you can (even a basic window or
   CLI). Do not pour effort into styling, custom components, animations, or theming yet.
2. **Decouple presentation from logic - hard.** The UI must be a thin shell that only reads
   from / drives the core's state (devices, per-channel waveform levels, session status,
   names, language targets). No business logic in the view layer. When the real design lands,
   we should be able to replace the entire UI without touching the engine.
3. **Don't apply the Riso Zine Funky design system yet, and don't hand-roll a "nice" look to
   stand in for it.** A neutral, functional UI is correct for now. Styling comes from the
   Claude Design output later, applied as a late, isolated layer.
4. **Leave seams for the known screens, but don't finalise them.** The wireframes will cover:
   a splash screen, then an accordion-style setup wizard (Row 1 inputs / Row 2 outputs / Row 3
   channel-test / final live-test), live waveforms, device pickers with defaults, optional
   naming, per-person language. Expose the state these need; don't lock in layout/markup.
5. **If you're building in Electron:** keep the renderer dumb. All heavy lifting (Gemini Live
   sessions, PortAudio device I/O, resampling) lives in the core (main process / a Python or
   Node service) that the UI merely drives over a clean interface. That keeps a UI swap cheap.

## Why
The visuals are being designed in parallel and will change. If you bake them in now, every
design iteration becomes an expensive rewrite. Treat the UI as disposable scaffolding around a
solid engine until David shares the wireframes/prototype - then we skin it properly.

See also: [[plan-python-prototype]] (build plan + contracts) and
[[claude-design-ux-prompt]] (the brief David gave Claude Design, so you know what's coming).
