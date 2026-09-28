"""
The Whistling World Cup
========================

Sound-controlled LEGO car. A live microphone feed (captured with pyaudio)
drives the car using a recorder (the instrument) and claps:

    notes 1-3 (any of the 3 lowest)  -> REVERSE, one fixed speed (latched -- keeps going)
    note 4                          -> FORWARD, faster than REVERSE (latched -- keeps going)
    note 5                          -> LEFT, a bounded 90 degree turn, then STOP
    note 6 (highest)                -> RIGHT, a bounded 90 degree turn, then STOP
    one clap                        -> STOP
    3 quick RIGHT turns (ball only) -> GOAL: publishes over MQTT and ends the match

Notes 1-3 all mean the same thing (REVERSE) rather than 3 distinct speed
tiers, since they weren't reliably distinguishable from each other in
practice -- see the NOTE_COMMANDS comment near the top of the file if you
want a different mapping. The 6 notes are whatever your recorder actually
produces -- calibrated to your instrument at startup rather than
hardcoded, since exact pitches vary by instrument and fingering. Both the
noise floor and the note calibration are saved to disk after the first
run and reused on every run after that, so you don't have to redo that
setup every time (see --recalibrate).

On match day the car is also assigned a role -- "ball" or "goalie" -- and
the two cars coordinate the match over MQTT (start trigger, catch/goal
outcomes, and matching victory/death songs). Only the ball can be "caught"
(by the goalie's proximity, via its own light sensor) or score a goal (the
3-right-turns signal above); the goalie's job is just to drive into range
of the ball's sensor using the same sound controls -- its win comes
automatically from the ball's own "caught" MQTT message, not from
anything the goalie actively signals itself.

Run:
    python whistling.py --role ball
    python whistling.py --role goalie

See README.md for the full write-up (policy description, noise-masking
approach, and what happens with no sound detected) and for the constants
you need to fill in before running on real hardware (LEGO connection
cards, MQTT result topic).
"""

import argparse
import json
import os
import threading
import time
from collections import deque

import numpy as np
import pyaudio
import matplotlib.pyplot as plt
import matplotlib.animation as animation

import legoeducation as le
from lelib import doubleMotor, colorSensor
from mqttlib import MQTTClient
import songs

# --------------------------------------------------------------------------
# Hardware / MQTT configuration -- fill these in for your team's hardware.
# --------------------------------------------------------------------------

# One shared physical connection card, tapped for both hubs -- a card is a
# single printed (serial, color) pair, so the Double Motor and Color Sensor
# use the exact same values here rather than two separately-configured ones.
CARD_SERIAL = 1142
CARD_COLOR = le.LEGO_COLOR_ORANGE

MOTOR_CARD_SERIAL = CARD_SERIAL
MOTOR_CARD_COLOR = CARD_COLOR

SENSOR_CARD_SERIAL = CARD_SERIAL
SENSOR_CARD_COLOR = CARD_COLOR

START_TOPIC = "ME193/Rogers"    # instructor-assigned start trigger (do not change)
RESULT_TOPIC = "ME193/tasha"    # agreed with Mohammed and confirmed working via mqtt_chat.py

# --------------------------------------------------------------------------
# Audio / policy configuration
# --------------------------------------------------------------------------

SAMPLE_RATE = 44100
CHUNK = 2048
PREFERRED_MIC_NAME_CONTAINS = "airpods"  # case-insensitive substring match for the mic picker's default

# The recorder's notes are expected somewhere in here. Widen this if your
# instrument's actual range falls outside it.
NOTE_LOW_HZ = 300
NOTE_HIGH_HZ = 2000
NOTE_COUNT = 6              # how many recorder notes to calibrate and recognize, lowest to highest
NOTE_VOTE_FRAMES = 2        # consecutive frames that must agree on a note before it's committed to
NOTE_HOLD_S = 5.0           # how long to actively listen for each note during calibration
NOTE_GAP_S = 1.0            # pause between notes during calibration, to switch fingering

# One command per calibrated note, lowest to highest -- command_for_note()
# just looks up this list by index. FORWARD and REVERSE are both fixed
# speeds (FORWARD_SPEED/REVERSE_SPEED below), so however many notes you
# assign to either one, they all just mean "go" at that one speed --
# notes 1-3 weren't reliably distinguishable from each other on the actual
# recorder, so all three share one outcome (REVERSE) rather than needing
# 3 clean tiers.
NOTE_COMMANDS = ["REVERSE", "REVERSE", "REVERSE", "FORWARD", "LEFT", "RIGHT"]

# A single periodogram (one FFT of one chunk) of pure broadband noise is
# naturally spiky -- each bin's magnitude has high variance even though no
# bin is really "louder" than the rest on average -- so a single-frame
# peak/median ratio badly over-reports random bins as "tonal". Splitting
# each chunk into WELCH_SEGMENTS sub-windows and averaging their spectra
# (Welch's method) cancels that per-bin noise while a genuinely sustained
# tone stays peaked in every sub-window, so it's still detected with
# enormous margin. Verified: with 1 segment, white noise crosses
# PEAKINESS_MIN 89% of the time; with 4, essentially never. This coarser,
# Welch-averaged spectrum is used only to decide *whether* a tone is
# present -- a separate, higher-resolution estimate (see
# _refine_peak_freq) is used to tell which of the 6 close-together notes
# it actually is, since adjacent recorder notes can be closer together
# than the Welch spectrum's bin width.
WELCH_SEGMENTS = 4
SUB_CHUNK = CHUNK // WELCH_SEGMENTS
PEAKINESS_MIN = 6.0        # peak power must be >= this many x the in-band median power to count as tonal
CALIBRATION_S = 1.5        # seconds of ambient noise sampled at startup to set the noise floor
NOISE_FLOOR_K = 1.0        # noise floor = ambient_rms_mean + K * ambient_rms_std -- kept low
                           # because the peakiness gate (not volume) is what actually tells a
                           # note from noise, so this doesn't need a big safety margin; lower
                           # further if a quiet instrument still can't clear it

FORWARD_SPEED = 50          # fixed -- however many notes map to FORWARD, they all drive at this one speed
REVERSE_SPEED = 50          # fixed and deliberately slower than FORWARD -- reversing blind is riskier
TURN_SPEED = 55             # turn speed %% for the LEFT/RIGHT rotation below
TURN_DEGREES = 90           # LEFT/RIGHT are a bounded turn-and-stop, not a continuous spin -- this many
                            # degrees, via the IMU-confirmed movement_turn_for_degrees() (see audio_worker)

# The ball's special "I reached the goal" signal: this many RIGHT turns in
# a row (each one a real, visible 90-degree turn -- see audio_worker),
# within GOAL_TAP_WINDOW_S of each other. Only meaningful for role=="ball";
# the goalie can turn right freely without ever triggering this. The window
# has to be generous -- each turn itself blocks for close to a second, on
# top of the time it takes to re-aim and replay the note.
GOAL_TAP_COUNT = 3
GOAL_TAP_WINDOW_S = 8.0

# Claps: STOP, on just one clap. A clap is loud and broadband -- the
# opposite of a recorder note's narrowband pitch -- so it's never
# confused with a note no matter how the thresholds above are tuned.
PERCUSSIVE_RMS_MARGIN = 1.5   # a clap must be this many x the noise floor (claps are loud)
PERCUSSIVE_MAX_S = 0.25       # a broadband burst longer than this isn't a clap (e.g. a scrape) -- ignored

COMMAND_PERIOD_S = 0.1     # minimum time between motor commands sent over Bluetooth

CATCH_REFLECTION = 200     # colorSensor.reflection() at/above this = something is right up on the sensor
CATCH_HOLD_FRAMES = 3      # consecutive high readings required before declaring a catch

# The noise floor and the 6 recorder notes are saved here after the first
# run's calibration and reused on every run after that, so you don't have
# to redo either every time -- pass --recalibrate to force a fresh one.
CALIBRATION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".calibration.json")

# Frequencies/window for the coarse, Welch-averaged tonal-vs-noise gate.
FREQS = np.fft.rfftfreq(SUB_CHUNK, d=1.0 / SAMPLE_RATE)
BAND_MASK = (FREQS >= NOTE_LOW_HZ) & (FREQS <= NOTE_HIGH_HZ)
BAND_FREQS = FREQS[BAND_MASK]
SUB_WINDOW = np.hanning(SUB_CHUNK).astype(np.float32)

# Frequencies/window for the fine, full-resolution pitch estimate used to
# tell the 6 notes apart (see _refine_peak_freq).
FULL_FREQS = np.fft.rfftfreq(CHUNK, d=1.0 / SAMPLE_RATE)
FULL_BAND_MASK = (FULL_FREQS >= NOTE_LOW_HZ) & (FULL_FREQS <= NOTE_HIGH_HZ)
FULL_BAND_FREQS = FULL_FREQS[FULL_BAND_MASK]
FULL_WINDOW = np.hanning(CHUNK).astype(np.float32)


# --------------------------------------------------------------------------
# Shared state -- audio thread, sensor thread, MQTT thread, and the GUI
# thread (matplotlib) all touch this through the lock.
# --------------------------------------------------------------------------

class SharedState:
    def __init__(self, role):
        self.lock = threading.Lock()
        self.role = role

        self.samples = np.zeros(CHUNK, dtype=np.float32)
        self.spectrum = np.zeros_like(FREQS)
        self.rms = 0.0
        self.noise_floor = 0.0
        self.peak_freq = 0.0           # coarse (Welch) peak, for the gate / display
        self.refined_freq = 0.0        # fine peak, for note matching
        self.peakiness = 0.0
        self.tonal_detected = False
        self.note_freqs = []           # the 6 calibrated note frequencies, lowest to highest
        self.committed_note = None     # index (0-5) of the currently-committed note, or None
        self.clap_in_progress = False  # for the live display -- a percussive burst is currently being heard
        self.forward_speed = FORWARD_SPEED  # for display only now -- always the same fixed value
        self.command = "STOP"          # the currently-latched drive command

        self.reflection = 0

        self.start_received = False
        self.game_over = False
        self.outcome = None  # "won" / "lost"


# --------------------------------------------------------------------------
# Signal analysis
# --------------------------------------------------------------------------

def _analyze_frame(samples, noise_floor):
    """Pure signal analysis for one audio chunk, no shared-state side effects.
    Returns (rms, spectrum, peak_freq, peakiness, tonal).

    The magnitude spectrum is Welch-averaged across WELCH_SEGMENTS
    sub-windows rather than a single FFT of the whole chunk (see the
    WELCH_SEGMENTS comment above) -- this is what keeps loud broadband
    noise (talking, claps) from randomly reading as "tonal"."""
    rms = float(np.sqrt(np.mean(samples ** 2)))
    sub_mags = [np.abs(np.fft.rfft(samples[i * SUB_CHUNK:(i + 1) * SUB_CHUNK] * SUB_WINDOW))
                for i in range(WELCH_SEGMENTS)]
    spectrum = np.mean(sub_mags, axis=0)
    band_mag = spectrum[BAND_MASK]

    if band_mag.size == 0 or rms < noise_floor:
        return rms, spectrum, 0.0, 0.0, False

    # A sustained tone is narrowband: nearly all its energy sits in one FFT
    # bin. Background noise (talking, claps) is broadband, so its peak bin
    # isn't much louder than the rest of the band. That ratio -- not just
    # loudness -- is what tells a tone from noise.
    peak_idx = int(np.argmax(band_mag))
    peak_freq = float(BAND_FREQS[peak_idx])
    peak_power = float(band_mag[peak_idx]) ** 2
    median_power = float(np.median(band_mag)) ** 2 + 1e-12
    peakiness = peak_power / median_power
    tonal = peakiness >= PEAKINESS_MIN

    return rms, spectrum, peak_freq, peakiness, tonal


def _refine_peak_freq(samples):
    """A higher-resolution pitch estimate than the Welch-averaged spectrum
    can give: a full-chunk FFT (finer bins) plus quadratic interpolation
    around the peak bin (sub-bin precision). Needed because two adjacent
    recorder notes can sit closer together than the coarse spectrum's bin
    width, which would otherwise make them indistinguishable."""
    spectrum = np.abs(np.fft.rfft(samples * FULL_WINDOW))
    band_mag = spectrum[FULL_BAND_MASK]
    if band_mag.size == 0:
        return 0.0

    k = int(np.argmax(band_mag))
    bin_width = FULL_FREQS[1] - FULL_FREQS[0]
    if 0 < k < len(band_mag) - 1:
        left, center, right = float(band_mag[k - 1]), float(band_mag[k]), float(band_mag[k + 1])
        denom = left - 2 * center + right
        delta = 0.5 * (left - right) / denom if denom != 0 else 0.0
        delta = max(-1.0, min(1.0, delta))  # guard against a degenerate/flat neighborhood
    else:
        delta = 0.0

    return float(FULL_BAND_FREQS[k] + delta * bin_width)


def classify_frame(state, samples):
    """Update state with this frame's signal, and return (peak_freq, tonal, rms, spectrum)."""
    with state.lock:
        noise_floor = state.noise_floor

    rms, spectrum, peak_freq, peakiness, tonal = _analyze_frame(samples, noise_floor)

    with state.lock:
        state.samples = samples
        state.spectrum = spectrum
        state.rms = rms
        state.peak_freq = peak_freq
        state.peakiness = peakiness
        state.tonal_detected = tonal

    return peak_freq, tonal, rms, spectrum


def is_percussive(rms, tonal, noise_floor):
    """Loud + broadband (not tonal) -- a candidate clap transient."""
    return rms >= noise_floor * PERCUSSIVE_RMS_MARGIN and not tonal


# --------------------------------------------------------------------------
# Command detectors
# --------------------------------------------------------------------------

def forward_speed_for_note(_index):
    """However many notes map to FORWARD, they all drive at the same
    fixed FORWARD_SPEED -- index is unused, kept only so the call site
    doesn't need to change if per-note speed tiers come back later."""
    return FORWARD_SPEED


def command_for_note(index):
    """Maps a committed note index (0-based, lowest to highest) to a
    command via NOTE_COMMANDS. Returns None for an index beyond
    NOTE_COMMANDS (only possible if NOTE_COUNT > len(NOTE_COMMANDS))."""
    return NOTE_COMMANDS[index] if index < len(NOTE_COMMANDS) else None


class NoteDetector:
    """Classifies a detected pitch by nearest match against the calibrated
    note frequencies, debounced over NOTE_VOTE_FRAMES consecutive frames
    before a new note commits (so a single noisy frame can't flip between
    two adjacent notes). A pitch too far from every calibrated note (more
    than half the smallest gap between adjacent notes) doesn't match
    anything and is dropped, the same "ambiguous -> ignore" philosophy
    used everywhere else in this policy.

    A confirmed gap of silence (NOTE_VOTE_FRAMES frames with no tonal
    match) resets the committed note back to None. Without this, playing
    the *same* note twice in a row -- silence, then that note again --
    would never re-fire, since a "new" commit only used to mean "different
    from last time": replaying a one-shot command like a turn wouldn't do
    anything the second time. Two calibrated notes are never adjacent
    enough to blur into this reset (max_distance already keeps stray
    pitches from matching any note at all, silence or not).
    """

    def __init__(self, note_freqs):
        self.note_freqs = note_freqs
        gaps = [b - a for a, b in zip(note_freqs, note_freqs[1:])]
        self.max_distance = (min(gaps) / 2.0) if gaps else 40.0
        self.recent = deque(maxlen=NOTE_VOTE_FRAMES)
        self.committed_index = None

    def _nearest_index(self, freq):
        diffs = [abs(freq - f) for f in self.note_freqs]
        idx = int(np.argmin(diffs))
        return idx if diffs[idx] <= self.max_distance else None

    def update(self, tonal, freq):
        """Returns the newly-committed note index if it just changed, else None."""
        idx = self._nearest_index(freq) if tonal else None
        self.recent.append(idx)
        if len(self.recent) == NOTE_VOTE_FRAMES and len(set(self.recent)) == 1:
            candidate = self.recent[-1]
            if candidate is None:
                self.committed_index = None  # confirmed silence -- allow the same note to re-fire next time
            elif candidate != self.committed_index:
                self.committed_index = candidate
                return candidate
        return None


class ClapStopDetector:
    """One clap fires STOP, the instant the clap burst ends. update(now,
    percussive) is called once per audio frame and returns "STOP" or
    None. A broadband burst longer than PERCUSSIVE_MAX_S (e.g. a scrape,
    not a clap) is ignored rather than firing STOP."""

    def __init__(self):
        self.burst_active = False
        self.burst_start = None

    def update(self, now, percussive):
        command = None

        if percussive:
            if not self.burst_active:
                self.burst_active = True
                self.burst_start = now
        elif self.burst_active:
            self.burst_active = False
            duration = now - self.burst_start
            if duration <= PERCUSSIVE_MAX_S:
                command = "STOP"

        return command


def apply_command(dm, command, forward_speed):
    """Handles the *latched* commands -- STOP/FORWARD/REVERSE, each
    re-issued every COMMAND_PERIOD_S for as long as they're current. LEFT/
    RIGHT are NOT handled here: a turn is a bounded, one-shot action, fired
    directly in audio_worker the instant its note commits, not something
    to keep re-applying on a timer."""
    if command == "STOP":
        dm.movement_move_tank(0, 0)
    elif command == "FORWARD":
        dm.movement_move_tank(forward_speed, forward_speed)
    elif command == "REVERSE":
        dm.movement_move_tank(-REVERSE_SPEED, -REVERSE_SPEED)


# --------------------------------------------------------------------------
# Game logic (MQTT)
# --------------------------------------------------------------------------

def stop_robot(dm):
    try:
        dm.movement_move_tank(0, 0)
    except Exception:
        pass


def handle_caught(state, dm, mqtt_client):
    """Our own light sensor sees the opponent right on top of us."""
    with state.lock:
        if state.game_over:
            return
        state.game_over = True
        state.outcome = "lost"
    stop_robot(dm)
    if mqtt_client is not None:
        mqtt_client.publish(RESULT_TOPIC, f"caught:{state.role}")
    songs.play_death_song()
    songs.play_death_song_on_hub(dm)


def handle_goal(state, dm, mqtt_client):
    """We (the ball) gave the special scoring command -- GOAL_TAP_COUNT
    quick RIGHT turns in a row (see the goal-tap tracking in
    audio_worker, role-gated to state.role == "ball" there)."""
    with state.lock:
        if state.game_over:
            return
        state.game_over = True
        state.outcome = "won"
    stop_robot(dm)
    if mqtt_client is not None:
        mqtt_client.publish(RESULT_TOPIC, f"goal:{state.role}")
    songs.play_success_song()
    songs.play_success_song_on_hub(dm)


def on_start_message(state, topic, payload):
    if payload.strip().lower() == "start":
        with state.lock:
            state.start_received = True
        print(f"[{topic}] start received -- driving is live")


def on_result_message(state, dm, _topic, payload):
    with state.lock:
        if state.game_over:
            return
    try:
        event, other_role = payload.split(":", 1)
    except ValueError:
        return
    if other_role == state.role:
        return  # this is the broker echoing our own publish back to us -- already handled locally

    with state.lock:
        state.game_over = True
        state.outcome = "won" if event == "caught" else "lost"
    stop_robot(dm)
    if event == "caught":
        # the other car got caught -- we win the point
        songs.play_success_song()
        songs.play_success_song_on_hub(dm)
    elif event == "goal":
        # the other car scored -- we lose the point
        songs.play_death_song()
        songs.play_death_song_on_hub(dm)


# --------------------------------------------------------------------------
# Calibration (persisted to disk so it doesn't repeat every run)
# --------------------------------------------------------------------------

def _load_calibration():
    try:
        with open(CALIBRATION_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_calibration(**updates):
    data = _load_calibration()
    data.update(updates)
    try:
        with open(CALIBRATION_FILE, "w") as f:
            json.dump(data, f)
    except OSError as exc:
        print(f"  (could not save calibration for next time: {exc})")


def calibrate_noise_floor(state, stream, force=False):
    if not force:
        saved = _load_calibration().get("noise_floor")
        if saved is not None:
            with state.lock:
                state.noise_floor = saved
            print(f"Using saved noise floor {saved:.5f} from a previous run (pass --recalibrate to redo it).")
            return

    print(f"Calibrating ambient noise floor ({CALIBRATION_S:.1f}s) -- stay quiet...")
    n_frames = max(1, int(CALIBRATION_S * SAMPLE_RATE / CHUNK))
    rms_samples = []
    for _ in range(n_frames):
        raw = stream.read(CHUNK, exception_on_overflow=False)
        samples = np.frombuffer(raw, dtype=np.float32)
        rms = float(np.sqrt(np.mean(samples ** 2)))
        rms_samples.append(rms)
        with state.lock:  # so the waveform panel moves during this step too, not just after
            state.samples = samples
            state.rms = rms
    mean, std = float(np.mean(rms_samples)), float(np.std(rms_samples))
    floor = mean + NOISE_FLOOR_K * std
    with state.lock:
        state.noise_floor = floor
    _save_calibration(noise_floor=floor)
    print(f"Noise floor set to {floor:.5f} (ambient mean={mean:.5f}, std={std:.5f}) -- saved for next time.")


def _capture_one_note(state, stream, hold_s=NOTE_HOLD_S):
    """Actively listen for hold_s seconds (a fixed, predictable window --
    not "until you stop playing") and return the median refined pitch of
    whatever tonal frames occurred during it, or None if nothing was ever
    tonal. Routes every frame through classify_frame() -- the same
    function normal driving uses -- so the live plot actually moves during
    calibration instead of sitting frozen while only the printed numbers
    update. Also prints a live rms/peakiness readout every ~0.5s while
    listening, so "nothing captured" is diagnosable (too quiet vs. wrong
    pitch range) instead of a total black box."""
    with state.lock:
        noise_floor = state.noise_floor

    deadline = time.monotonic() + hold_s
    freqs = []
    frames_since_print = 0
    print_every = max(1, int(0.5 * SAMPLE_RATE / CHUNK))  # ~every 0.5s
    while time.monotonic() < deadline:
        raw = stream.read(CHUNK, exception_on_overflow=False)
        samples = np.frombuffer(raw, dtype=np.float32)
        peak_freq, tonal, rms, _spectrum = classify_frame(state, samples)
        with state.lock:
            peakiness = state.peakiness

        frames_since_print += 1
        if frames_since_print >= print_every:
            frames_since_print = 0
            print(f"    listening... rms={rms:.4f} (need >= {noise_floor:.4f})"
                  f"  peak={peak_freq:.0f}Hz  peakiness={peakiness:.1f}x (need >= {PEAKINESS_MIN:.1f}x)"
                  f"  tonal={'YES' if tonal else 'no'}")

        if tonal:
            freqs.append(_refine_peak_freq(samples))

    return float(np.median(freqs)) if freqs else None


def calibrate_notes(state, stream, force=False):
    if not force:
        saved = _load_calibration().get("note_freqs")
        if saved is not None and len(saved) == NOTE_COUNT:
            print(f"Using saved note frequencies from a previous run: "
                  f"{[f'{f:.0f}Hz' for f in saved]} (pass --recalibrate to redo it).")
            with state.lock:
                state.note_freqs = saved
            return saved

    print(f"Now calibrating your {NOTE_COUNT} recorder notes -- play them ONE AT A TIME, lowest to highest.")
    print(f"Each note: hold it for {NOTE_HOLD_S:.0f}s when prompted, then a {NOTE_GAP_S:.0f}s pause before the next.")
    note_freqs = []
    for i in range(NOTE_COUNT):
        print(f"  Play note {i + 1}/{NOTE_COUNT} now, hold it for {NOTE_HOLD_S:.0f}s...")
        freq = _capture_one_note(state, stream)
        if freq is None:
            freq = NOTE_LOW_HZ + i * (NOTE_HIGH_HZ - NOTE_LOW_HZ) / (NOTE_COUNT - 1)
            print(f"  Didn't catch note {i + 1} -- using a placeholder ({freq:.0f} Hz). "
                  f"Recalibrate before match day.")
        else:
            print(f"  Got note {i + 1}: {freq:.0f} Hz")
            if note_freqs and freq <= note_freqs[-1]:
                print(f"  Warning: that's not higher than note {i} ({note_freqs[-1]:.0f} Hz) -- "
                      f"make sure you're playing lowest to highest, or speeds will be out of order.")
        note_freqs.append(freq)
        if i < NOTE_COUNT - 1:
            print(f"  ({NOTE_GAP_S:.0f}s pause -- get ready for the next note)")
            time.sleep(NOTE_GAP_S)

    with state.lock:
        state.note_freqs = note_freqs
    _save_calibration(note_freqs=note_freqs)
    return note_freqs


# --------------------------------------------------------------------------
# Worker threads
# --------------------------------------------------------------------------

def audio_worker(state, dm, mqtt_client, pa, device_index, stop_event, force_recalibrate):
    stream = pa.open(format=pyaudio.paFloat32, channels=1, rate=SAMPLE_RATE,
                      input=True, input_device_index=device_index,
                      frames_per_buffer=CHUNK)
    try:
        calibrate_noise_floor(state, stream, force=force_recalibrate)
        with state.lock:
            noise_floor = state.noise_floor
        note_freqs = calibrate_notes(state, stream, force=force_recalibrate)

        note_detector = NoteDetector(note_freqs)
        clap_detector = ClapStopDetector()
        current_command = "STOP"  # safe default until the first recognized command
        current_forward_speed = FORWARD_SPEED
        last_command_time = 0.0
        right_tap_times = deque()  # for the ball's "3 right turns in a row" goal signal

        while not stop_event.is_set():
            raw = stream.read(CHUNK, exception_on_overflow=False)
            samples = np.frombuffer(raw, dtype=np.float32)
            _coarse_peak_freq, tonal, rms, _spectrum = classify_frame(state, samples)
            now = time.monotonic()

            with state.lock:
                start_received = state.start_received
                game_over = state.game_over

            refined_freq = _refine_peak_freq(samples) if tonal else 0.0
            newly_committed = note_detector.update(tonal, refined_freq)
            if newly_committed is not None:
                resolved = command_for_note(newly_committed)
                if resolved == "FORWARD":
                    current_command = "FORWARD"
                    current_forward_speed = forward_speed_for_note(newly_committed)
                    print(f"note {newly_committed + 1}/{NOTE_COUNT} ({refined_freq:.0f} Hz) "
                          f"-> FORWARD @ {current_forward_speed:.0f}%")
                elif resolved in ("LEFT", "RIGHT"):
                    # A turn is a bounded, one-shot action -- not a state to
                    # latch and keep re-issuing every COMMAND_PERIOD_S like
                    # FORWARD/REVERSE/STOP are. Fire it once, right here,
                    # then fall back to STOP so nothing keeps re-turning.
                    print(f"note {newly_committed + 1}/{NOTE_COUNT} ({refined_freq:.0f} Hz) "
                          f"-> {resolved} ({TURN_DEGREES}° turn)")
                    if start_received and not game_over:
                        direction = (le.MOVEMENT_TURN_DIRECTION_LEFT if resolved == "LEFT"
                                     else le.MOVEMENT_TURN_DIRECTION_RIGHT)
                        # Blocking by default -- the IMU confirms the turn
                        # actually completed before this returns, which is
                        # exactly what we want for a bounded turn. It does
                        # mean no audio is read for the ~1s the turn takes;
                        # acceptable for a deliberate, occasional action.
                        dm.movement_turn_for_degrees(TURN_DEGREES, direction=direction, speed=TURN_SPEED)

                        # The ball's goal signal: GOAL_TAP_COUNT RIGHT turns
                        # within GOAL_TAP_WINDOW_S. Only the ball can score --
                        # the goalie can turn right as much as it wants.
                        if resolved == "RIGHT" and state.role == "ball":
                            tap_time = time.monotonic()
                            right_tap_times.append(tap_time)
                            while right_tap_times and tap_time - right_tap_times[0] > GOAL_TAP_WINDOW_S:
                                right_tap_times.popleft()
                            if len(right_tap_times) >= GOAL_TAP_COUNT:
                                right_tap_times.clear()
                                print(f"GOAL! ({GOAL_TAP_COUNT} quick right turns)")
                                handle_goal(state, dm, mqtt_client)
                    current_command = "STOP"
                elif resolved is not None:
                    current_command = resolved
                    print(f"note {newly_committed + 1}/{NOTE_COUNT} ({refined_freq:.0f} Hz) -> {current_command}")

            percussive = is_percussive(rms, tonal, noise_floor)
            clap_command = clap_detector.update(now, percussive)
            if clap_command is not None:
                current_command = clap_command
                print(f"clap command: {current_command}")

            with state.lock:
                state.refined_freq = refined_freq
                state.committed_note = note_detector.committed_index
                state.clap_in_progress = clap_detector.burst_active
                state.forward_speed = current_forward_speed
                state.command = current_command

            if start_received and not game_over and now - last_command_time >= COMMAND_PERIOD_S:
                apply_command(dm, current_command, current_forward_speed)
                last_command_time = now
    finally:
        stream.stop_stream()
        stream.close()


def sensor_worker(state, cs, dm, mqtt_client, stop_event):
    """Only the ball can be "caught" -- the goalie catching the ball is a
    win for the goalie, but that comes from the ball's own MQTT message
    (see on_result_message), not from the goalie's own sensor. Without
    this role gate, the goalie's sensor firing would declare *itself*
    caught (a self-inflicted loss) instead, which is backwards."""
    consecutive = 0
    while not stop_event.is_set():
        reflection = cs.reflection()
        with state.lock:
            state.reflection = reflection
            game_over = state.game_over
            role = state.role

        consecutive = consecutive + 1 if reflection >= CATCH_REFLECTION else 0
        if consecutive >= CATCH_HOLD_FRAMES and not game_over and role == "ball":
            handle_caught(state, dm, mqtt_client)

        time.sleep(0.1)


# --------------------------------------------------------------------------
# Live display -- shows the signal (waveform + spectrum) and the decision
# it produced, updated in real time.
# --------------------------------------------------------------------------

def run_display(state):
    fig, (ax_wave, ax_spec) = plt.subplots(2, 1, figsize=(9, 7))
    fig.canvas.manager.set_window_title("Whistling World Cup -- live signal & decision")

    wave_line, = ax_wave.plot(np.arange(CHUNK), np.zeros(CHUNK))
    ax_wave.set_ylim(-1, 1)
    ax_wave.set_xlim(0, CHUNK)
    ax_wave.set_title("Microphone waveform")
    ax_wave.set_xlabel("sample")
    ax_wave.set_ylabel("amplitude")

    spec_line, = ax_spec.plot(BAND_FREQS, np.zeros_like(BAND_FREQS))
    peak_marker, = ax_spec.plot([], [], "ro", markersize=8)
    ax_spec.set_xlim(NOTE_LOW_HZ, NOTE_HIGH_HZ)
    ax_spec.set_title("Note spectrum (dashed lines appear once notes are calibrated)")
    ax_spec.set_xlabel("Hz")
    ax_spec.set_ylabel("magnitude")

    status_text = ax_spec.text(0.98, 0.95, "", transform=ax_spec.transAxes,
                                va="top", ha="right", fontsize=10, family="monospace",
                                bbox=dict(boxstyle="round", fc="white", alpha=0.85))
    note_lines_drawn = {"done": False}

    def update(_frame):
        with state.lock:
            samples = state.samples
            band_mag = state.spectrum[BAND_MASK]
            rms = state.rms
            noise_floor = state.noise_floor
            peak_freq = state.peak_freq
            refined_freq = state.refined_freq
            peakiness = state.peakiness
            tonal = state.tonal_detected
            note_freqs = list(state.note_freqs)
            committed_note = state.committed_note
            clap_in_progress = state.clap_in_progress
            forward_speed = state.forward_speed
            command = state.command
            reflection = state.reflection
            role = state.role
            start_received = state.start_received
            game_over = state.game_over
            outcome = state.outcome

        if not note_lines_drawn["done"] and note_freqs:
            for i, f in enumerate(note_freqs):
                ax_spec.axvline(f, color="tab:green", linestyle="--", linewidth=1, alpha=0.6)
                ax_spec.text(f, 0.02, f"note {i + 1}", rotation=90, fontsize=7, va="bottom", ha="right",
                             transform=ax_spec.get_xaxis_transform())
            note_lines_drawn["done"] = True

        wave_line.set_ydata(samples)

        spec_line.set_data(BAND_FREQS, band_mag)
        ax_spec.set_ylim(0, max(float(band_mag.max()) if band_mag.size else 1.0, 1.0) * 1.2)
        if tonal:
            peak_marker.set_data([refined_freq], [float(band_mag.max()) if band_mag.size else 0])
        else:
            peak_marker.set_data([], [])

        if committed_note is not None:
            meaning = command_for_note(committed_note)
            meaning = f"FORWARD @ {forward_speed:.0f}%" if meaning == "FORWARD" else meaning
            note_str = f"{committed_note + 1}/{NOTE_COUNT} -> {meaning}"
        else:
            note_str = "-"
        status = (
            f"role: {role:<7} match: {'LIVE' if start_received else 'waiting for start'}\n"
            f"rms:  {rms:.4f}  (floor {noise_floor:.4f})\n"
            f"peak: {refined_freq:7.1f} Hz  (coarse {peak_freq:.0f} Hz)   peakiness: {peakiness:6.1f}x\n"
            f"tonal detected: {'YES' if tonal else 'no'}   note: {note_str}\n"
            f"clap: {'in progress' if clap_in_progress else '-'}  (1 clap = STOP)\n"
            f"command: {command}\n"
            f"light sensor: {reflection}"
        )
        if game_over:
            status += f"\n\n*** GAME OVER: {outcome.upper()} ***"
        status_text.set_text(status)

        return wave_line, spec_line, peak_marker, status_text

    ani = animation.FuncAnimation(fig, update, interval=70, blit=False, cache_frame_data=False)
    plt.tight_layout()
    plt.show(block=True)  # must block -- interactive mode would make this return instantly and tear everything down
    return ani


# --------------------------------------------------------------------------
# Mic selection (pyaudio)
# --------------------------------------------------------------------------

def pick_input_device(pa):
    try:
        system_default_index = pa.get_default_input_device_info().get("index")
    except IOError:
        system_default_index = None

    print("Available microphone inputs:")
    inputs = []
    preferred_index = None
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info.get("maxInputChannels", 0) > 0:
            inputs.append(i)
            name = info["name"]
            is_preferred = preferred_index is None and PREFERRED_MIC_NAME_CONTAINS in name.lower()
            if is_preferred:
                preferred_index = i
            tag = " <- preferred" if is_preferred else (" <- system default" if i == system_default_index else "")
            print(f"  [{i}] {name}{tag}")

    if not inputs:
        print("No input devices found; using system default.")
        return None

    # A named preference (e.g. AirPods) beats the OS's own default input
    # device, since macOS often still points that at the built-in mic even
    # once a Bluetooth headset is connected.
    default_index = preferred_index if preferred_index is not None else system_default_index

    choice = input("Enter device number (or press Enter for default): ").strip()
    if not choice:
        return default_index
    try:
        idx = int(choice)
        if idx in inputs:
            return idx
    except ValueError:
        pass
    print("Invalid selection; using default.")
    return default_index


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Sound-controlled LEGO car for the Whistling World Cup.")
    parser.add_argument("--role", choices=["ball", "goalie"], required=True)
    parser.add_argument("--device", type=int, default=None,
                         help="pyaudio input device index (skips the interactive picker)")
    parser.add_argument("--practice", action="store_true",
                         help="skip MQTT and the 'start' wait entirely -- drive as soon as you make a sound, "
                              "for practicing before match day")
    parser.add_argument("--recalibrate", action="store_true",
                         help="redo the noise-floor and note calibration instead of reusing the saved ones")
    args = parser.parse_args()

    state = SharedState(role=args.role)
    print(f"=== Whistling World Cup -- role: {args.role}{' (PRACTICE MODE)' if args.practice else ''} ===")

    print("Connecting to LEGO Double Motor...")
    dm = doubleMotor()
    dm.connect(card_serial=MOTOR_CARD_SERIAL, card_color=MOTOR_CARD_COLOR)
    print("Connected to motor hub.")

    print("Connecting to LEGO Color Sensor (front light sensor)...")
    cs = colorSensor()
    cs.connect(card_serial=SENSOR_CARD_SERIAL, card_color=SENSOR_CARD_COLOR)
    print("Connected to color sensor.")

    if args.practice:
        # No broker, no waiting for the instructor's "start" -- drive as soon
        # as calibration finishes. Catch/goal detection still run against the
        # real sensor/mic, but with nowhere to publish to, they just stop the
        # car and play the song locally instead of ending a real match.
        mqtt_client = None
        state.start_received = True
        print("Practice mode: skipping MQTT -- driving is live as soon as calibration finishes.")
    else:
        mqtt_client = MQTTClient()
        mqtt_client.connect()
        mqtt_client.subscribe(START_TOPIC, lambda topic, payload: on_start_message(state, topic, payload))
        mqtt_client.subscribe(RESULT_TOPIC, lambda topic, payload: on_result_message(state, dm, topic, payload))
        print(f"MQTT connected. Waiting for 'start' on {START_TOPIC} ...")

    pa = pyaudio.PyAudio()
    device_index = args.device if args.device is not None else pick_input_device(pa)

    stop_event = threading.Event()
    audio_thread = threading.Thread(target=audio_worker,
                                     args=(state, dm, mqtt_client, pa, device_index, stop_event, args.recalibrate),
                                     daemon=True)
    sensor_thread = threading.Thread(target=sensor_worker,
                                      args=(state, cs, dm, mqtt_client, stop_event),
                                      daemon=True)
    audio_thread.start()
    sensor_thread.start()

    try:
        run_display(state)
    finally:
        stop_event.set()
        stop_robot(dm)
        if mqtt_client is not None:
            mqtt_client.disconnect()
        pa.terminate()
        audio_thread.join(timeout=2)
        sensor_thread.join(timeout=2)


if __name__ == "__main__":
    main()
