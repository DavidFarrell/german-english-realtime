**Findings**

- **High: Slice 0 will fail in this local SDK as written.** I checked the installed package: `google-genai 1.56.0` has `LiveConnectConfig`, but no `types.TranslationConfig` and no `translation_config` field; Pydantic rejects the extra field. That means [live_translate_starter.py](/Users/david/git/ai-sandbox/projects/German-English-realtime/live_translate_starter.py:66) will not even construct config here. Current official docs do show `TranslationConfig`, so the plan needs an explicit first step: upgrade/pin a docs-compatible `google-genai`, or fall back to raw WebSocket `generationConfig.translationConfig`.

- **High: the plan uses stale send/receive semantics.** The architecture says `session.send` in [plan-python-prototype.md](/Users/david/git/ai-sandbox/projects/German-English-realtime/notes/plan-python-prototype.md:27), but the canonical quickstart uses `session.send_realtime_input(audio=types.Blob(...))` with `mime_type="audio/pcm;rate=16000"` at [Get_started_LiveAPI.py](/Users/david/git/ai-sandbox/projects/German-English-realtime/reference/Get_started_LiveAPI.py:299). `session.send` is deprecated in the SDK. Also, translation docs say audio chunks should be raw 16-bit little-endian PCM at 16 kHz and recommend 100 ms chunks.

- **High: receive handling in the plan/starter is wrong for the current event shape.** Do not rely on `response.data` / `response.text` from [live_translate_starter.py](/Users/david/git/ai-sandbox/projects/German-English-realtime/live_translate_starter.py:207). The canonical quickstart processes `response.server_content.model_turn.parts[].inline_data.data` and separately handles `input_transcription` / `output_transcription` at [Get_started_LiveAPI.py](/Users/david/git/ai-sandbox/projects/German-English-realtime/reference/Get_started_LiveAPI.py:181). One server event can contain audio and transcription together; process all fields, not `continue` after audio.

- **Medium: transcript ownership is underspecified.** The plan says receive returns “audio+text” and Slice 4 adds “Live transcript print”, but your context says on-screen transcript comes from parallel transcribers. If that is true, the plan omits those extra sessions, their cost, alignment, failure handling, and latency. If you instead use Live Translation’s built-in input/output transcriptions, they must be enabled in config and treated as diagnostic text, not as the translation path.

- **Medium: model id / API version needs normalization.** Official Python examples use `gemini-3.5-live-translate-preview`; raw WebSocket examples use `models/gemini-3.5-live-translate-preview`. The plan uses the prefixed form. Test exactly what `client.aio.live.connect()` accepts after the SDK upgrade. Also standardize `GEMINI_API_KEY` vs `GOOGLE_API_KEY`; the SDK can read both, but precedence can confuse debugging.

**Architecture Critique**

The asyncio + sounddevice callback bridge is reasonable, but the plan is too vague where failures happen. The callback must not resample, allocate heavily, or block. Use a bounded ring buffer/deque per channel, record overflow counters, and drop oldest audio when late. `loop.call_soon_threadsafe(queue.put_nowait, ...)` can still flood the event loop if the sender stalls.

Use streaming resampling, not independent per-buffer resampling. `soxr` is fine, but keep filter state across callbacks. Decide the actual block size around the API’s 100 ms chunk recommendation: 48 kHz input gives 4,800 stereo frames, downsampled to 1,600 mono samples, 3,200 bytes per API chunk.

Output needs more design. Blocking `stream.write()` in a thread is okay for a spike, but it hides underruns and makes latency timestamps fuzzy. For measurement, use output callbacks with jitter buffers, zero-fill on underrun, and log queue depth. Also assume many output devices will not open at 24 kHz; resample model output to each device’s actual preferred rate if needed.

Two concurrent sessions are plausible, but you need to prove quotas and connection stability before full hardware. Run two canned real-time streams concurrently as an early spike. Add explicit handling/logging for dropped WS, `goAway`, and session renewal.

**Latency**

The canned-clip `send-start -> first output byte` metric is useful, but it is not mouth-to-ear. It misses mic capture, PortAudio buffers, callback bridge, resampler delay, output device latency, and possibly Bluetooth codec latency. It also fails if you send the file faster than real time. Pace clips in 100 ms chunks.

For real mouth-to-ear on one machine, I would use a physical or loopback measurement rig: play the source clip through a small speaker/earbud physically coupled to the DJI TX mic, route the translated listener output into a recorder input or measurement mic/coupler, record both source marker/audio and translated output on the same clock, then measure waveform/forced-alignment deltas. For German verb-final testing, measure “source disambiguating word spoken” to “target equivalent audible”, not just first translated syllable.

Confounds that will bite: VAD start/end, queue drops, warm connection vs cold connection, model voice leading silence, Bluetooth latency, output underruns, resampler group delay, mic bleed, echo, API jitter, and semantic delay from long German clauses.

**Slice Ordering**

Slice 0 is the right first spike, but split it:

1. SDK/API schema spike: prove `TranslationConfig`, connect, send paced canned audio, parse current receive events.
2. Audio hardware spike: enumerate devices, record DJI stereo, verify TX1/TX2 channel mapping, open each output device.
3. One-direction live path with wired output.
4. Latency harness for one direction before two-way concurrency.
5. Two sessions with canned streams, then full DJI two-way.

Device listing does not belong in the same acceptance gate as proving the preview model.

**macOS Gotchas**

Expect device indices to change; persist names plus host API, not raw indices only. Two Bluetooth earbuds at once may fail, drift, or add large latency. For measurement, use wired USB DACs or an audio interface first. Confirm Audio MIDI Setup exposes DJI as true 2-channel 48 kHz stereo, not mono/safety/mixed mode. Terminal/iTerm/VS Code needs mic permission. Avoid selecting Bluetooth headset mics, which can force low-quality HFP mode. Independent output devices have independent clocks, so do not assume sample-perfect sync.

**Most Important Plan Changes**

1. Start with SDK compatibility: upgrade/pin `google-genai` or implement raw WebSocket fallback before any audio work.
2. Replace all `session.send` / `response.data` assumptions with `send_realtime_input` and `server_content.model_turn.parts`.
3. Define bounded queues, drop policy, streaming resampler, output jitter buffers, and instrumentation.
4. Move real latency measurement earlier and measure physical mouth-to-ear, not only send-to-receive.
5. Add macOS device/channel/output validation as its own hardware spike.
6. Decide whether transcripts come from Live Translation or separate transcribers, then plan those sessions explicitly.

Sources checked: Google’s Live Translation docs, Live API SDK guide, Session Management guide, and the official cookbook quickstart.