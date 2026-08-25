# LocalDictation

Hold a key, talk, let go. Your words are transcribed, cleaned up, and typed
wherever your cursor already is.

This is a self-hosted replacement for a Wispr Flow subscription. It matches the
feature surface — the AI cleanup, the per-app writing styles, the custom
dictionary, snippets, command mode, the floating recording bar, the stats — and
runs entirely on your own machine. There is no account, no server, and no
monthly bill.

---

## What it does

### The dictation loop

Hold **Ctrl+Win** anywhere and speak. Let go, and about a second later the
finished text appears at your cursor.

- **Push-to-talk** — hold to talk, release to insert.
- **Hands-free** — double-tap the same key *while already talking* to lock
  recording on and take your hands off the keyboard. Or bind Ctrl+Win+Space.
- **Esc cancels** an in-flight dictation. Nothing is transcribed or inserted.
- **Tap protection** — a hold shorter than 0.35s is treated as a stray keypress
  and silently discarded, so brushing the key never records.
- **Session cap** — long recordings warn at 19 minutes and end gracefully at 20,
  transcribing what you said rather than discarding it.

### It writes, rather than transcribing

Raw Whisper output is faithful to the audio, which is exactly what makes it
unpleasant to read. The cleanup pipeline closes that gap:

| You say | You get |
|---|---|
| `um so i think we should uh ship it on friday` | `So I think we should ship it on Friday.` |
| `the the report is is done` | `The report is done.` |
| `go to the store, no wait, the bank` | `Go to the bank.` |
| `send it to john at gmail dot com` | `Send it to john@gmail.com.` |
| `revenue grew forty two percent` | `Revenue grew 42%.` |
| `about twenty five thousand dollars` | `About $25,000.` |
| `let's meet at three thirty p m on the twenty first` | `Let's meet at 3:30 PM on the 21st.` |
| `call me at five five five one two one two` | `Call me at 555-1212.` |
| `we shipped version two point one point three` | `We shipped v2.1.3.` |

All of that is deterministic and offline. An optional AI pass on top handles the
things rules cannot: sentence segmentation from meaning, ambiguous
self-corrections, and tone.

**Auto Cleanup** is a single dial with four settings:

- **None** — verbatim, mistakes included. For when you need the exact words.
- **Light** — filler words and stutters removed, your wording untouched.
- **Medium** (default) — adds punctuation, capitalization, numbers, symbols,
  and self-correction handling.
- **High** — everything above plus AI polish matched to the app you are in.

Every transcript is stored **both raw and cleaned**, so **Undo AI edit** on any
history row puts the original back. The cleanup can never lose your words.

### It knows where you are typing

The app reads the foreground window's program name and title, maps it to a
category, and writes accordingly — a Slack message and an email should not come
out the same.

| Category | Apps | Default style |
|---|---|---|
| Personal messaging | iMessage, WhatsApp, Telegram, Discord | Casual |
| Work messaging | Slack, Teams, Zoom chat | Casual |
| Email | Gmail, Outlook, Superhuman, Thunderbird | Formal |
| Code & terminal | VS Code, Cursor, terminals, JetBrains | Technical |
| Documents & other | Word, Notion, Docs, browsers | Formal |

Browsers are resolved by window title, so a Gmail tab is treated as email while
a GitHub tab in the same browser is treated as code. Any app can be remapped.

**This only ever reads the window's program name and title.** It does not read
screen contents, does not capture screenshots, and nothing leaves your machine.

### It learns your words

The **Dictionary** holds names, jargon, and acronyms. Entries are fed to the
recognizer as decoding hints — this is what actually stops "Kubernetes" from
coming out as "kubernetties" — and a fuzzy pass repairs anything the model still
mangles. Star an entry to keep it prioritised when the list outgrows the
prompt budget.

**Auto-learn** watches for the case where you retype a word it produced. Fix
"Anthropik" to "Anthropic" once and it is remembered. Only single-word
substitutions similar to what was emitted are learned, which is what separates
"fixed my spelling" from "rewrote the sentence".

### Snippets and replacements

Say a short phrase, get a saved block of text. `my work email` becomes your
address; `standup` becomes your template. Supports `{date}`, `{time}`,
`{datetime}`, `{iso_date}`, and `{clipboard}`.

**Text replacements** on the Styles page are the blunter tool: literal
find-and-replace applied to every dictation. Use the dictionary for names and
jargon — it also improves recognition, rather than only fixing the text
afterwards.

### Command mode

Select text anywhere, hold **Ctrl+Win+Alt**, and say what to do with it —
"make this more concise", "turn this into bullet points", "translate to
Spanish". The selection is replaced in place.

Case changes and bullet conversion work with no AI backend at all. Anything
needing real rewriting requires a backend to be configured.

Named **transforms** are the same mechanic with a pre-written instruction on its
own hotkey, so no speaking is needed. Polish, Make formal, Make casual, Bullet
points, and Fix grammar ship built in.

Command mode is deliberately kept on a separate key from dictation. Dictation
must never summarize or reword; that guarantee only holds if the code that *can*
do so is unreachable from the dictation path.

### The Flow Bar

A floating pill shows what is happening while you speak. It carries two separate
signals at once, which is what makes it a diagnostic rather than decoration:

- **Bar height** follows amplitude — proof the audio stream is open.
- **Bar colour** turns teal when it hears *voice* — proof it is hearing you.

Bars that move but stay grey mean the mic is picking up a room, not a person. No
bar at all means the hotkey never fired.

Drag it to dock at the bottom, left, or right edge; it reflows vertically on the
sides and remembers where you put it. Right-click for **Hide for 1 hour**, or
turn it off entirely — dictation keeps working, with the animated tray icon as
the status signal.

### Insights

Words dictated, words per minute, time saved against typing, words the cleanup
fixed for you, dictionary and snippet replacements, a six-month activity
heatmap, and which apps you dictate into most.

---

## Install

Requires **Python 3.10+** on **Windows**.

```bash
git clone https://github.com/teelap/local-dictation
cd local-dictation
pip install -r requirements.txt
python main.py
```

The first launch walks you through picking a microphone, confirming your
hotkeys, choosing a cleanup level, and dictating once to prove it works.

The speech model downloads automatically the first time it is used. `base.en` is
a good default; `small.en` is noticeably more accurate if you have the CPU or a
GPU for it.

### Optional: the AI cleanup backend

Everything above works with no backend. Cleanup level **High** and the rewriting
half of command mode need one.

| Backend | Where it runs | Setup |
|---|---|---|
| **Off** (default) | Nowhere — rules only | Nothing |
| **Ollama** | Your machine | `ollama pull llama3.2:3b`, then pick it in Settings |
| **OpenAI-compatible** | Wherever you point it | Set the base URL (LM Studio, llama.cpp, any host) |
| **Anthropic** | Anthropic's API | `pip install anthropic`, set `ANTHROPIC_API_KEY` |

**Off and Ollama keep everything on your machine.** The other two send
transcript text to whatever host you configure — the app tells you this in
Settings before you choose one.

API keys are read from the environment first (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`) so they never have to be written to `config.json`.

---

## Default hotkeys

| Action | Binding |
|---|---|
| Push to talk | `Ctrl+Win` |
| Hands-free toggle | `Ctrl+Win+Space` (or double-tap push-to-talk) |
| Command mode | `Ctrl+Win+Alt` |
| Paste last transcript | `Shift+Alt+Z` |
| Scratchpad | `Win+Alt+S` |
| Cancel | `Esc` |

All rebindable in Settings → Shortcuts.

---

## How it fits together

```
audio.py          capture, level metering, mic watchdog, session cap
   |
transcription.py  faster-whisper, biased by your dictionary, language pool
   |
dictionary.py     fuzzy correction of terms the model still mangled
   |
snippets.py       trigger phrase -> saved text
   |
formatter.py      fillers, stutters, self-corrections, numbers, times,
   |              symbols, punctuation, capitalization (+ optional LLM)
   |
injector.py       clipboard paste with restore, or synthesized typing
```

Around that: `context.py` decides the writing style from the foreground window,
`llm.py` is the pluggable backend, `history.py` / `stats.py` are the record, and
`ui/` is the interface.

Every stage after transcription is optional and degrades to a no-op, so a
failure anywhere still ends with your words in the text field. If the paste
itself fails, the text stays on the clipboard and `Shift+Alt+Z` re-inserts it.

### Files it writes

All next to the app, all plain JSON, all gitignored:

`config.json`, `history.json`, `dictionary.json`, `snippets.json`,
`stats.json`, `transforms.json`, `dictation.log`.

Nothing is sent anywhere unless you explicitly configure a cloud AI backend.

---

## Tests

```bash
python -m unittest test_dictation -v
```

106 tests over the text pipeline, vocabulary, snippets, history, stats, config
migration, and context resolution. Every case is either a behaviour from the
spec or a bug found during the build, so a failure means real user-visible
output changed. A further set covering the hotkey state machine runs where the
audio and hotkey dependencies are installed, and skips cleanly where they are not.

Two of the tests are performance guards. The app supports 20-minute hands-free
sessions, and both the formatter and the vocabulary pass were quadratic on
exactly that input — several thousand words of run-on speech with no punctuation
to anchor on.

---

## Deliberate differences from Wispr Flow

A few behaviours were changed on purpose rather than copied:

- **Command mode with nothing selected** inserts what you said as ordinary
  dictation. Wispr Flow sends the utterance to a web search; silently making a
  network request is the wrong default for an offline app.
- **The Flow Bar is shown by default.** Wispr Flow hides it after complaints
  about occlusion; here it is draggable, edge-dockable, snoozable, and
  disableable, which addresses the complaint without giving up the status signal.
- **Apps can be remapped between categories.** Wispr Flow assigns them for you.
- **No usage metering, word caps, or paid tiers.** That is the point.

### Not included

**Meeting Notetaker** — recording and summarizing calls — is a separate product
surface rather than part of the dictation loop, and is not implemented here. If
you use it, that is the one part of the subscription this does not replace.

---

## Known limits

- **Windows only** for hotkeys, text injection, and the startup entry. The text
  pipeline itself is platform-independent and its tests run anywhere.
- **Sentence segmentation** from prosody alone is not something rules can do. A
  long run-on utterance gets its punctuation from the AI pass; at Medium and
  below it may come out as one long sentence.
- **Elevated windows.** Windows blocks synthesized input into a window running
  at higher privilege than this app. Use `Shift+Alt+Z` with that window focused,
  or run the app elevated too.
- **First dictation after launch** waits for the speech model to load.
- **Insertion does not read the text around your cursor.** Continuing a sentence
  you had already started will capitalize as if it were a new one. Matching that
  seam needs to read the focused text field, which is exactly the screen-reading
  this app avoids.
