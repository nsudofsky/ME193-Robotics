# Tomorrow morning checklist

Everything below is tested in software (unit tests + real MQTT broker
tests), but **nothing has touched real LEGO hardware yet** — that's the
whole point of tomorrow morning. Work top to bottom; each section assumes
the one above it works.

## 1. Basic connection (no driving yet)

- [ ] `source /Users/tashasudofsky/AIRobotics/.venv/bin/activate` (the
      top-level venv, **not** `in class test/.venv`)
- [ ] `cd /Users/tashasudofsky/AIRobotics/whistlingworldcup`
- [ ] Charge/power on the LEGO hub, have the recorder and a working mic
      (AirPods) ready
- [ ] `python whistling.py --role ball --practice --recalibrate`
- [ ] Confirm it connects to the Double Motor ("Connected to motor hub.")
- [ ] Confirm it connects to the Color Sensor ("Connected to color
      sensor.") — if this fails, check `CARD_SERIAL`/`CARD_COLOR` at the
      top of `whistling.py` still match your actual card
- [ ] Confirm the AirPods get picked as the mic (or pick them manually)

## 2. Calibration, for real

- [ ] Noise floor calibration completes (stay quiet for the ~1.5s)
- [ ] Play all 6 recorder notes when prompted, lowest to highest, each
      held the full 5s
- [ ] Watch the printed `Got note X: ___ Hz` lines — confirm each is
      **higher** than the last (no "Warning: that's not higher..." lines)
- [ ] Confirm the live plot window actually opens and the waveform/
      spectrum move while you're playing (not frozen)

## 3. Each command, one at a time

For each of these, check the *decision* on-screen (`command:` field)
before checking that the motors follow.

- [ ] Play note 1, 2, **or** 3 → command shows `REVERSE`, car reverses
- [ ] Play note 4 → command shows `FORWARD`, car drives forward — is
      `FORWARD_SPEED`/`REVERSE_SPEED` (currently 50/50) the speed you
      actually want? Adjust in `whistling.py` if not.
- [ ] Play note 5 → car turns left ~90° once, then stops (not a
      continuous spin) — is 90° visually accurate? Tune `TURN_DEGREES`/
      `TURN_SPEED` if not.
- [ ] Play note 6 → car turns right ~90° once, then stops
- [ ] Clap once → car stops immediately, from whatever it was doing
- [ ] Play note 6 three times quickly (within ~8s) → after the 3rd turn,
      terminal prints `GOAL!` and the success song plays — **if this
      fires by accident while you're just turning right normally, that's
      the known tradeoff we flagged; tighten `GOAL_TAP_WINDOW_S` if it's
      a problem**

## 4. The catch sensor

- [ ] With the ball's color sensor facing forward and open, bring
      something (a hand, the goalie car) close to it
- [ ] Confirm it takes noticeably close proximity to trigger (not too
      sensitive, not too insensitive) — tune `CATCH_REFLECTION` if not
- [ ] Confirm the car stops, prints `caught:ball`-style output, and the
      death song plays (both the computer speakers and — new — the hub's
      own speaker; listen for both)

## 5. With Mohammed — real match simulation

- [ ] Both of you `git pull` the latest from
      `https://github.com/nsudofsky/ME193-Robotics.git` first
- [ ] Both run `mqtt_chat.py --topic ME193/tasha` once more just to
      reconfirm connectivity is still good this morning
- [ ] One of you runs `python whistling.py --role ball`, the other
      `python whistling.py --role goalie` (real mode, not `--practice`)
      — confirm both print "Waiting for 'start'..." and neither drives yet
- [ ] From a third terminal (or either laptop), run `python send_mqtt.py`
      to publish `"start"` on `ME193/Rogers` — confirm **both** cars
      start responding to sound within a second or two
- [ ] Run an actual catch: goalie approaches ball's sensor → confirm
      ball plays death song + publishes, **and** goalie plays success
      song (on receipt, not locally) — this tests the full MQTT round
      trip, not just local sound
- [ ] Reset both scripts, run again, this time have the ball do the
      3-right-turns GOAL signal → confirm the ball plays success song,
      **and** the goalie plays death song on receipt
- [ ] Confirm neither of you can accidentally "catch" or "score" on
      *yourself* — goalie's own sensor/turns should never end the match

## 6. Match-day environment differences

- [ ] Recalibrate (`--recalibrate`) in the **actual competition room**,
      not wherever you tested this morning — ambient noise and mic
      distance will differ, and the noise floor / note frequencies are
      saved from whatever room you last calibrated in
- [ ] Double check `RESULT_TOPIC` in `whistling.py` still matches
      whatever Mohammed's own script (if separate from this repo) is
      actually using

## 7. Submission logistics (not code, but graded)

- [ ] Group Notion site exists, is laid out clearly, and links to
      `https://github.com/nsudofsky/ME193-Robotics` (the working fork —
      **not** the archived `mohdalmheiri/ME193-3-AI-in-Mobile-Robots`)
- [ ] Notion site explains what the code does and directly answers the
      three required questions (policy description, no-sound behavior,
      noise-masking approach) — all three are already written up in
      `The Whistling World Cup/README.md` in the repo, so this can mostly
      be a copy/link job rather than writing from scratch
- [ ] Final `git push` of anything touched during testing above
