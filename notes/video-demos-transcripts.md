# Embedded video demos - transcripts + detail

The Google blog post embeds four videos. Grabbed and transcribed (English via parakeet;
multilingual segments cleaned via Gemini). Source post:
<https://blog.google/innovation-and-ai/models-and-research/gemini-models/gemini-live-3-5-translate/>

---

## 1. Main launch demo - "Gemini 3.5 Live Translate" (YouTube TNwKs39uSVk, 3:29)

The flagship demo. A presenter speaks and the model live-translates into multiple target
languages at once, then the presenter switches spoken language mid-stream to show
auto-detection. Multilingual transcript (each English source line, then the live translation):

- **[EN]** "Great idea. Let's start with Spanish." -> live **Japanese** session translation.
- **[EN]** "Yeah, and let's add Tamil too." -> **Japanese**.
- **[EN]** "Yes, good idea. And Mandarin as well." -> **Japanese**.
- **[EN]** "All of the translation sessions are using the same input audio from the speaker's
  microphone." -> **Spanish**. *(Key point: many target sessions, one shared mic input.)*
- **[EN]** "...the model does a really good job of translating in real time and sounding
  super natural, even with technical terms." -> **Tamil**.
- **[EN]** "You can imagine how useful this could be at a large international conference with
  attendees from all over the world." -> **Mandarin** (on-screen Chinese caption shown too).
- **[DE - spoken]** "Und jetzt wechsle ich zu Deutsch. Das Modell erkennt den Sprachwechsel
  automatisch. Ich muss nichts konfigurieren." -> EN: *"Now I'm switching to German. The
  model recognizes the language switch automatically. I don't have to configure anything."*
  **<- the directly-relevant bit: a live German<->English switch with zero config.**
- **[Sinhala - spoken]** -> EN: *"...what I say in Sinhala is translated into English without
  any configuration."*
- **[DE - spoken]** "Und hören Sie, wie natürlich die Übersetzung klingt, keine abgehackten
  oder künstlichen Pausen. Es fliesst wie eine völlig natürliche Sprache." -> EN: *"listen to
  how natural the translation sounds. No choppiness, no artificial pauses. It flows like a
  completely natural language."*
- **[DE - spoken]** "Schalten wir nun zur japanischen Session..." -> shows a session that has
  been running since the start staying in sync, and staying synced when the speaker pauses.
- **[EN] close:** "The model is now available on the Gemini API and in AI Studio. You can
  also try it in Google Translate on iOS and Android by connecting any pair of headphones.
  And we're also rolling it out in private preview on Google Meet. ... We can't wait to hear
  what you build."

**Takeaways for us:** (a) multiple target-language sessions can run off one shared input
audio stream simultaneously; (b) mid-stream language switching is automatic, no config -
exactly the DE<->EN behaviour we want; (c) the German examples confirm DE is a first-class
demo language.

---

## 2. Google Meet demo - "Speech translation in Google Meet" (YouTube DLSLKCqahyI, 1:07)

Subtitle: "Google Meet participants use speech translation to communicate across English,
Mandarin, and Swedish." A three-way call where each person speaks their own language and
hears the others in theirs. English audio captured:

> "Okay, let's turn on speech translation. It's great to see you both. Cassie, how's the
> weather in Shanghai? ... The weather here is really nice. ... I had dinner at my favourite
> restaurant with some friends. If you visit Sweden, you must try this restaurant. ..."

**Takeaway:** this is the multi-party case - implemented as one session per target language
(confirmed in the ThursdAI interview, see [[thursdai-insights]]).

---

## 3. Partner demo - "Grab with Gemini Audio" (YouTube 16Y2DU6LJX4, 0:48)

Subtitle: "See how Grab has been testing 3.5 Live Translate to transform communication
between users." A ride-hailing scenario: a passenger and a driver who don't share a language
find each other by phone, each speaking their own language, translated both ways.

> "Good morning, sir." / [driver speaks Vietnamese] / "I am not sure where I am. Let me look
> around first. I think I'm at the main gate, but I don't see your car." / "...I'm at the
> other pickup point. Let me walk a bit further to find your car." / "Take your time, sir." /
> "Yes, I see your car. I'm coming right over." / "Thank you."

**Takeaway:** real two-way conversational use in the wild (Grab = "Uber of SE Asia"). This is
the conversational pattern closest to a DE<->EN family/phone use case.

---

## 4. "Listening Mode" product demo (direct MP4, 0:25)

`LiveTranslate_ListeningMode_Blog_1920x1080`. A short, scripted "listening mode" clip -
one-directional (listen to a speaker/tour in your own language):

> "Welcome to our tour of the beautiful city of Cartagena. We will walk through its
> cobblestone streets to admire its impressive architecture. And we will end the day enjoying
> a beautiful sunset."

**Takeaway:** the one-directional "listen to a talk/tour in your language" mode (vs two-way
conversation) - the simplest thing to prototype.
