# The Whistling World Cup

A LEGO car driven by a recorder (the instrument) and claps. `whistling.py`
grabs a live microphone stream with `pyaudio`, listens for one of 6
calibrated recorder notes, and splits them between forward-speed tiers and
fixed commands:

| Sound | Command |
|---|---|
| recorder note 1 (lowest) | FORWARD, slowest |
| recorder note 2 | FORWARD, faster |
| recorder note 3 | FORWARD, fastest |
| recorder note 4 | REVERSE |
| recorder note 5 | LEFT |
| recorder note 6 (highest) | RIGHT |
| two claps | STOP |

The 6 notes aren't hardcoded to specific frequencies — they're calibrated
to *your* recorder at startup (play each one, lowest to highest, when
prompted), since the exact pitch depends on the instrument and fingering.
Which notes are speed tiers vs. fixed commands, and which fixed command
each one maps to, is controlled by `SPEED_NOTE_COUNT`/`NOTE_COMMANDS` near
the top of `whistling.py` — easy to change without touching the
calibration flow itself. On match day the car is also assigned a role —
**ball** or **goalie** — and the two cars coordinate the match over MQTT.

## Files

| File | What it does |
|---|---|
| [`whistling.py`](./whistling.py) | Main script: audio capture, note/clap policy, live display, LEGO driving, MQTT game logic |
| [`songs.py`](./songs.py) | Synthesizes and plays the success/death jingles through the computer speakers |
| [`lelib.py`](./lelib.py) | Shared LEGO Education wrapper (copied from the class library) |
| [`mqttlib.py`](./mqttlib.py) | Shared `paho-mqtt` wrapper (copied from the class library) |

## Setup

```
pip install --upgrade pip
pip install pyaudio numpy matplotlib paho-mqtt legoeducation
```

`pyaudio` needs the PortAudio C library installed first (`brew install
portaudio` on macOS) — see [Getting pyaudio installed without
Homebrew](#getting-pyaudio-installed-without-homebrew).

Before running on real hardware, fill in the `TODO`s at the top of
`whistling.py`:

- `CARD_SERIAL` / `CARD_COLOR` — your team's LEGO connection card (one
  physical card, used for both the Double Motor and the Color Sensor).
- `RESULT_TOPIC` — agree on a unique MQTT subtopic with the opposing team
  (e.g. `ME193/Rogers/<your-team-name>`) so your game-outcome messages
  don't collide with anyone else on the shared public broker.

Then, on match day:

```
python whistling.py --role ball      # on the ball's laptop
python whistling.py --role goalie    # on the goalie's laptop
```

The first run walks you through calibration (ambient noise, then your 6
recorder notes lowest to highest — 5s to hold each note, with a 1s pause
between so you can switch fingering); later runs reuse it automatically
(see [Calibration only runs once](#calibration-only-runs-once)). Both cars
then wait for a `"start"` message on `ME193/Rogers` before driving.

### Practicing before match day

You don't need the instructor's `"start"` message (or even a network
connection) to practice. `--practice` skips MQTT entirely and starts
driving live as soon as calibration finishes:

```
python whistling.py --role ball --practice
```

Everything else runs for real — the live plot, the actual note/clap
policy, and the Color Sensor's catch detection (getting "caught" in
practice mode still stops the car and plays the death song, it just has
no match to report the outcome to).

## Questions

### How does the policy make decisions?

Every audio chunk (2048 samples, ~46 ms) is checked for two independent
things — is there a *tonal* sound present (a recorder note), and is there
a *percussive* one (a clap)? Those two can't both be true at once
(tonal = narrowband, percussive = broadband), so notes and claps never
compete over the same sound.

**A note decides the command.** Rather than hardcoding frequencies,
`calibrate_notes()` asks you to play your 6 notes once, lowest to
highest, and records each one's pitch. At runtime, whichever calibrated
note the current pitch is nearest to becomes the *committed* note once 2
consecutive frames agree on it (`NoteDetector`) — this debounces one-off
noisy frames without adding much lag. A pitch too far from every
calibrated note (more than half the smallest gap between two adjacent
notes) doesn't match anything and is dropped, rather than guessed at.
`command_for_note()` then splits the 6 committed note indices: the lowest
`SPEED_NOTE_COUNT` (3) are all FORWARD, with the speed itself scaled
linearly between `MIN_FORWARD_SPEED` and `MAX_FORWARD_SPEED` by note
index; each note above that is one fixed command from `NOTE_COMMANDS`
(REVERSE, then LEFT, then RIGHT) at a fixed speed, since there's no
continuous signal to scale a single note by.

Pitch estimation for this actually runs at **two different resolutions**
for two different jobs. A coarser, Welch-averaged spectrum (see the noise
question below) decides whether a tone is present *at all* — that's a
noise-vs-signal question, not a precision one. But telling 6 close-
together recorder notes apart *is* a precision question: two adjacent
diatonic notes can be as little as ~40 Hz apart, closer together than
that coarse spectrum's own bin width. So a second, finer estimate
(`_refine_peak_freq`) runs a full-resolution FFT plus quadratic
interpolation around the peak bin — verified in testing to land within
~1 Hz of the true pitch for a clean tone, easily precise enough to tell
apart notes 40+ Hz apart.

**Claps decide STOP**, via `ClapStopDetector`: two claps in a row fire
STOP immediately on the second one. A single clap not followed by a
second within 0.5s is simply forgotten — there's no other meaning for
one clap.

All outcomes *latch* — the car keeps executing the current one (issued
to the LEGO hub over Bluetooth at most every 100 ms) until a new one
fires. You don't have to keep playing a note or keep clapping to keep the
car moving.

Catching an opponent (light sensor) and scoring a goal (a dedicated
command, not implemented yet — see the `TODO` on `handle_goal()` in
`whistling.py`, since every note and both claps are now taken) are both
handled outside this policy entirely.

### What does your code do if no sound is detected?

It does **nothing** — it keeps executing whichever command last fired
(starting from STOP, the safe default, at boot), except that the forward
speed simply stops updating rather than resetting. This is a deliberate
consequence of the latching design: a note or a clap-pair is meant to set
a new state and then leave it alone, so there's no single continuous
signal to "lose" by going silent — falling back to STOP on silence would
defeat the whole point of a "keep doing this until told otherwise"
control scheme. The car only stops when two claps actually fire, the
catch sensor trips, or the match ends over MQTT.

### How did you try to mask out unwanted noise?

Several layers, stacked:

1. **Band-pass restriction** — only 300–2000 Hz is even considered at
   all (`NOTE_LOW_HZ`/`NOTE_HIGH_HZ`), which rules out most very-low
   rumble and electrical hiss outside a recorder's realistic range.
2. **Adaptive amplitude gate** — at startup the script records ~1.5 s of
   ambient room noise and sets the noise floor to
   `mean(ambient RMS) + 4 * std(ambient RMS)`. Anything quieter than that
   is ignored outright, and the threshold adapts to however loud the room
   actually is instead of a hardcoded magic number.
3. **Tonal "peakiness" ratio, averaged across sub-windows** — a sustained
   note is narrowband: nearly all its energy sits in one FFT bin.
   Talking, clapping, and background rumble are broadband, so their
   loudest bin isn't much louder than the rest of the band. We require
   the peak bin's power to be at least 6x the band's median power before
   calling something tonal — this is what actually distinguishes "a
   note" from "loud noise," on top of the volume gate. **This one had a
   real bug worth calling out**: a single FFT of one audio chunk is a
   noisy estimate — a single periodogram of pure broadband noise has high
   per-bin variance purely by chance, so a *single-frame* peak/median
   ratio crossed our threshold 89% of the time in testing, even for plain
   white noise. Fixed by splitting each chunk into 4 sub-windows and
   averaging their spectra (Welch's method) before computing the ratio —
   that dropped the false-positive rate to ~0% in the same test while a
   real tone still crosses the threshold by a factor of billions, not a
   random few percent. This is also what makes clap detection reliable,
   since it depends on broadband sounds *not* randomly reading as tonal.
4. **Nearest-note distance limit** — a pitch has to be within half the
   smallest gap between two of your calibrated notes to match any of
   them at all; anything further off (e.g. a stray harmonic, or someone
   talking at a pitch that happens to be tonal) is dropped rather than
   forced into the nearest note regardless of how far away it actually is.
5. **Live calibration, persisted** — because the peakiness/RMS thresholds
   (and the note frequencies themselves) depend on your specific mic,
   room, and instrument, they're measured for real rather than trusting
   numbers tuned somewhere else — but only once, not every run (see
   below).

The live plot (waveform + note spectrum with each calibrated note marked
once known, the detected peak marked, plus a status readout of RMS/peak
frequency/peakiness/note/forward speed/pending claps/command) makes it
possible to see in real time which of these gates is rejecting a given
sound, which is how the thresholds above were tuned.

## Match logic (MQTT)

- `ME193/Rogers` — instructor-published `"start"` trigger. Both roles
  subscribe and hold at STOP until it arrives.
- `RESULT_TOPIC` (`ME193/Rogers/whistling-world-cup` by default — rename
  this per the setup note above) — used for the two outcome messages, each
  published as `"<event>:<role>"`:
  - `"caught:ball"` — the ball's own light sensor saw the goalie get close.
    The ball stops, publishes this, and plays its death song immediately
    (it doesn't wait for its own message to echo back). The goalie, on
    receiving it, plays the success song.
  - `"goal:ball"` — the ball gave the special scoring command (not
    implemented yet). The ball stops, publishes this, and plays its
    success song. The goalie, on receiving it, plays the death song.

  Each side reacts to the *other* role's message (a client ignores a
  message carrying its own role, since the broker echoes every publish
  back to its own subscribers too) — so both songs always come out
  opposite, as required.

## Getting `pyaudio` installed without Homebrew

This machine had no Homebrew and no PortAudio headers, so `pip install
pyaudio` normally fails with `fatal error: 'portaudio.h' file not found`.
It's working now — PortAudio was built from source into a local prefix and
`pyaudio` was linked against that:

```
curl -sLo /tmp/portaudio.tgz http://files.portaudio.com/archives/pa_stable_v190700_20210406.tgz
tar xzf /tmp/portaudio.tgz -C /tmp
cd /tmp/portaudio
./configure --prefix="$HOME/AIRobotics/.portaudio" --disable-shared --enable-static
sed -i '' 's/-Werror//' Makefile   # newer clang trips on old unused-variable warnings treated as errors
make -j4 && make install
cp include/pa_mac_core.h "$HOME/AIRobotics/.portaudio/include/"  # not installed by `make install`, but pyaudio needs it

CFLAGS="-I$HOME/AIRobotics/.portaudio/include" \
LDFLAGS="-L$HOME/AIRobotics/.portaudio/lib -framework CoreAudio -framework AudioToolbox -framework AudioUnit -framework CoreFoundation -framework CoreServices" \
pip install pyaudio
```

If you do have Homebrew, it's much simpler: `brew install portaudio && pip
install pyaudio`.

## Using AirPods (or any Bluetooth mic) as the microphone

`whistling.py`'s device picker lists every input device pyaudio can see
and, when you press Enter, defaults to whichever one has `"airpods"` in
its name (`PREFERRED_MIC_NAME_CONTAINS` at the top of the file) rather
than the OS's own default input, which often stays pinned to the built-in
mic even once a headset is connected. Verified on this machine: once the
AirPods are connected over Bluetooth they show up as a dedicated input
device (`Tasha's AirPods #2`, mono, 24 kHz native — but 44.1 kHz also
opens fine) separate from their stereo output device.

One tradeoff worth knowing: using a Bluetooth headset's mic switches
macOS to the lower-quality HFP audio profile for that device, which would
also degrade anything played back through the *same* AirPods for as long
as the mic is in use. That's why `songs.py` doesn't just use the system
default output (which follows the AirPods once connected) -- it looks for
a device with `"macbook"` in its name (`PREFERRED_OUTPUT_NAME_CONTAINS`)
and plays the success/death songs there instead, so mic input (AirPods)
and song output (MacBook speakers) are deliberately split. Verified on
this machine: input is `Tasha's AirPods #2`, output resolves to `MacBook
Air Speakers`.

## Calibration only runs once

Both the noise floor and your 6 recorder notes are saved to
`.calibration.json` (next to `whistling.py`, git-ignored) the first time
they're measured, and loaded from disk on every run after that — no more
redoing that setup every single time you start the script. Pass
`--recalibrate` if you change rooms/mics/instruments and want to redo it
for real.

## Design history (why a recorder, why claps for stop)

This control scheme went through several iterations, each teaching
something about what's actually reliable with real hardware and a real
(imprecise) human:

1. **Continuous whistle pitch → 4 zones.** Abandoned — whistling a wide,
   precise pitch range by mouth on demand turned out to be genuinely
   hard to hit reliably.
2. **Counting whistle taps.** Abandoned — timing several short whistle
   taps into one tight phrase was also fragile; real gaps between
   whistles didn't line up with a fixed timing window.
3. **Whistle duration + snap/clap by sound.** The snap half didn't work —
   this mic couldn't reliably pick up snaps above the noise floor at all.
4. **Humming for speed + whistle duration + clap counting.** Clap
   *counting* worked well (claps are crisp and repeatable, unlike
   whistle taps), which is why it's kept in every version since. Telling
   hum from whistle by pitch range, on top of everything else, got
   complicated to keep untangled.
5. **Speech recognition for left/right.** Considered, but a full ASR
   engine (cloud-dependent, or a large local model) for a two-word
   vocabulary was overkill and added real match-day risk.
6. **Phone tone generator + 4 zones + claps.** Playing a tone from a
   phone app sidesteps "can a human hold a precise pitch," which is what
   doomed version 1 — but coordinating a phone app added its own
   friction on match day.
7. **Current version: a recorder (the instrument) + claps.** A recorder
   gives the same precise-pitch benefit as the phone tone generator, but
   it's an instrument already in hand rather than a second device to
   manage — and unlike a slide-based tone, it naturally produces 6
   *discrete* notes, which maps more directly onto discrete speed levels
   than a continuous frequency zone did. Notes are calibrated per-
   instrument rather than hardcoded, the same way the noise floor already
   was, extended to cover pitch this time too. Claps stay for STOP
   because clap counting was the one piece that already proved reliable
   back in version 4.

## Known limitations / TODO

- **Scoring a goal isn't wired up yet.** `handle_goal()` still exists
  (stops the car, publishes `"goal:<role>"`, plays the success song) but
  nothing calls it yet — every note (all 6, split between FORWARD's
  speed tiers and REVERSE/LEFT/RIGHT) and 2 claps are already taken, so
  scoring needs something else entirely, e.g. three claps.
- **Note-onset transients are an untested edge case.** The very start of
  a note (breath attack before the tone stabilizes) is briefly
  broadband, in principle close to what a clap looks like.
  `PERCUSSIVE_MAX_S` (0.25s) should keep a full note from ever being read
  as percussive, but a real attack landing in its own frame right at the
  start of a note hasn't been tested on hardware -- watch for a phantom
  clap-count tick right as a note starts, and tighten `PERCUSSIVE_MAX_S`
  or raise `PERCUSSIVE_RMS_MARGIN` if it happens.
- The FFT signal-analysis math (including the Welch-averaging fix and the
  quadratic-interpolation pitch refinement above), `NoteDetector`'s
  nearest-match + debounce logic, `ClapStopDetector`'s state machine,
  calibration persistence (verified a 2nd run makes zero audio reads when
  a saved value exists), the AirPods-preferring device picker, and raw
  `pyaudio` mic capture were all verified directly with synthetic
  signals (including a realistic diatonic scale with a ~40 Hz gap between
  two adjacent notes) and real short mic captures. What's still untested
  is the full hardware loop — LEGO BLE connect/drive, an MQTT round-trip
  between two laptops, and actually playing a real recorder at a real
  microphone — which needs the LEGO hub, the recorder, and a second
  laptop in hand.
- `CATCH_REFLECTION` (the "opponent is right on top of the sensor"
  threshold) and `MIN_FORWARD_SPEED`/`MAX_FORWARD_SPEED` (the speed range
  notes map to) are starting guesses — recalibrate/retune them against
  your actual color sensor and how fast you want the slowest/fastest note
  to drive.
- **Fixed:** the live plot used to sit frozen during both calibration
  steps (only the printed terminal numbers updated) because calibration
  read raw audio directly instead of going through `classify_frame()`
  (the function that also updates the shared state the plot reads from).
  Both calibration steps now route through it, so the waveform/spectrum
  move during calibration too, not just once normal driving starts.
- If you want an independent sanity check of what your recorder's notes
  actually measure at, a standalone scrolling spectrogram (`spectogram.py`,
  using `sounddevice` instead of `pyaudio`) is a handy second opinion —
  play your 6 notes at it and watch where the bright band lands on the
  frequency axis to confirm they're inside `NOTE_LOW_HZ`-`NOTE_HIGH_HZ`.
