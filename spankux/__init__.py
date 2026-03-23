#!/usr/bin/env python3
"""
spankux: Detects slaps via microphone and plays audio responses on Linux.
"""

import argparse
import json
import math
import os
import queue
import random
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

try:
    import numpy as np
    import sounddevice as sd
except ImportError:
    print("Error: Missing dependencies. Install with: pip install sounddevice numpy", file=sys.stderr)
    sys.exit(1)

VERSION = "dev"
SCRIPT_DIR = Path(__file__).parent

DECAY_HALF_LIFE = 30.0      # seconds; controls how fast escalation score fades
DEFAULT_MIN_AMPLITUDE = 0.05
DEFAULT_COOLDOWN_MS = 750
DEFAULT_SPEED_RATIO = 1.0
DEFAULT_SIMILARITY = 0.80
SAMPLE_RATE = 44100
CHUNK_SIZE = 1024           # ~23ms per chunk at 44100 Hz

# Global mutable state, protected by _state_lock
_state_lock = threading.Lock()
_state = {
    "paused": False,
    "min_amplitude": DEFAULT_MIN_AMPLITUDE,
    "cooldown_ms": DEFAULT_COOLDOWN_MS,
    "speed_ratio": DEFAULT_SPEED_RATIO,
    "volume_scaling": False,
    "similarity_threshold": DEFAULT_SIMILARITY,
}


# ---------------------------------------------------------------------------
# Audio player helpers
# ---------------------------------------------------------------------------

def find_player():
    """Return the first available CLI audio player, or None."""
    for p in ("mpg123", "ffplay", "mpv"):
        if shutil.which(p):
            return p
    return None


def _build_atempo(speed):
    """Build an ffmpeg atempo filter chain for arbitrary speed ratios.

    atempo only accepts values in [0.5, 2.0], so chain multiple filters
    for speeds outside that range.
    """
    filters = []
    while speed > 2.0:
        filters.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5:
        filters.append("atempo=0.5")
        speed *= 2.0
    filters.append(f"atempo={speed:.4f}")
    return ",".join(filters)


def amplitude_to_volume_factor(amplitude):
    """Map RMS amplitude [0.05, 0.80+] to a linear volume factor [0.125, 1.0].

    Uses the same logarithmic curve as the original Go implementation so that
    light taps are noticeably quieter and hard hits play near full volume.
    """
    min_amp, max_amp = 0.05, 0.80
    min_vol, max_vol = 0.125, 1.0
    if amplitude <= min_amp:
        return min_vol
    if amplitude >= max_amp:
        return max_vol
    t = (amplitude - min_amp) / (max_amp - min_amp)
    t = math.log(1 + t * 99) / math.log(100)
    return min_vol + t * (max_vol - min_vol)


def play_audio(filepath, amplitude, player, speed, vol_scale):
    """Play an MP3 file using the system audio player (blocking)."""
    vol_factor = amplitude_to_volume_factor(amplitude) if vol_scale else 1.0
    devnull = subprocess.DEVNULL

    try:
        if player == "mpg123":
            cmd = ["mpg123", "-q"]
            if vol_scale:
                # -f scale factor: 32768 = 100% volume
                cmd += ["-f", str(int(32768 * vol_factor))]
            cmd.append(str(filepath))

        elif player == "ffplay":
            af = []
            if vol_scale:
                af.append(f"volume={vol_factor:.4f}")
            if speed != 1.0:
                af.append(_build_atempo(speed))
            cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]
            if af:
                cmd += ["-af", ",".join(af)]
            cmd.append(str(filepath))

        elif player == "mpv":
            cmd = ["mpv", "--no-video", "--really-quiet"]
            if vol_scale:
                cmd.append(f"--volume={int(vol_factor * 100)}")
            if speed != 1.0:
                cmd.append(f"--speed={speed}")
            cmd.append(str(filepath))

        else:
            return

        subprocess.run(cmd, stdout=devnull, stderr=devnull)

    except Exception as e:
        print(f"spankux: playback error: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Slap tracking and escalation
# ---------------------------------------------------------------------------

class SlapTracker:
    """Tracks slap history and selects audio files based on mode and score."""

    def __init__(self, files, mode, cooldown_ms):
        self.files = sorted(files)
        self.mode = mode        # "random" or "escalation"
        self.score = 0.0
        self.last_time = None
        self.total = 0
        self._lock = threading.Lock()
        self._bag = []  # shuffle bag for random mode

        # Compute the escalation curve scale so that sustained max-rate
        # slapping (one per cooldown) converges to the final file index.
        cooldown_s = cooldown_ms / 1000.0
        ss_max = 1.0 / (1.0 - math.pow(0.5, cooldown_s / DECAY_HALF_LIFE))
        self.scale = (ss_max - 1) / math.log(len(self.files) + 1)

    def record(self):
        """Decay the current score, add 1 for the new slap, return (total, score)."""
        now = time.time()
        with self._lock:
            if self.last_time is not None:
                elapsed = now - self.last_time
                self.score *= math.pow(0.5, elapsed / DECAY_HALF_LIFE)
            self.score += 1.0
            self.last_time = now
            self.total += 1
            return self.total, self.score

    def get_file(self, score):
        """Select an audio file. Random mode ignores score."""
        if self.mode == "random":
            if not self._bag:
                self._bag = self.files[:]
                random.shuffle(self._bag)
            return self._bag.pop()
        # Escalation: 1-exp(-x) curve maps score → file index
        max_idx = len(self.files) - 1
        idx = min(
            int(len(self.files) * (1.0 - math.exp(-(score - 1) / self.scale))),
            max_idx,
        )
        return self.files[idx]


def load_audio_files(directory):
    """Return a sorted list of MP3 file paths from a directory."""
    path = Path(directory)
    if not path.is_dir():
        raise ValueError(f"Directory not found: {directory}")
    files = sorted(str(f) for f in path.glob("*.mp3"))
    if not files:
        raise ValueError(f"No MP3 files found in {directory}")
    return files


# ---------------------------------------------------------------------------
# JSON stdin command interface
# ---------------------------------------------------------------------------

def read_stdin_commands(stdio_mode):
    """Process JSON commands from stdin (runs in a background daemon thread)."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            cmd = json.loads(line)
        except json.JSONDecodeError as e:
            if stdio_mode:
                print(json.dumps({"error": f"invalid command: {e}"}), flush=True)
            continue

        action = cmd.get("cmd", "")

        if action == "pause":
            with _state_lock:
                _state["paused"] = True
            if stdio_mode:
                print('{"status":"paused"}', flush=True)

        elif action == "resume":
            with _state_lock:
                _state["paused"] = False
            if stdio_mode:
                print('{"status":"resumed"}', flush=True)

        elif action == "set":
            with _state_lock:
                if "amplitude" in cmd and 0 < cmd["amplitude"] <= 1:
                    _state["min_amplitude"] = cmd["amplitude"]
                if "cooldown" in cmd and cmd["cooldown"] > 0:
                    _state["cooldown_ms"] = cmd["cooldown"]
                if "speed" in cmd and cmd["speed"] > 0:
                    _state["speed_ratio"] = cmd["speed"]
                if "similarity" in cmd and 0.0 <= cmd["similarity"] <= 1.0:
                    _state["similarity_threshold"] = cmd["similarity"]
                amp  = _state["min_amplitude"]
                cool = _state["cooldown_ms"]
                spd  = _state["speed_ratio"]
                sim  = _state["similarity_threshold"]
            if stdio_mode:
                print(json.dumps({
                    "status": "settings_updated",
                    "amplitude": amp,
                    "cooldown": cool,
                    "speed": spd,
                    "similarity": sim,
                }), flush=True)

        elif action == "volume-scaling":
            with _state_lock:
                _state["volume_scaling"] = not _state["volume_scaling"]
                vs = _state["volume_scaling"]
            if stdio_mode:
                print(json.dumps({
                    "status": "volume_scaling_toggled",
                    "volume_scaling": vs,
                }), flush=True)

        elif action == "status":
            with _state_lock:
                snap = dict(_state)
            if stdio_mode:
                print(json.dumps({
                    "status": "ok",
                    "paused": snap["paused"],
                    "amplitude": snap["min_amplitude"],
                    "cooldown": snap["cooldown_ms"],
                    "volume_scaling": snap["volume_scaling"],
                    "speed": snap["speed_ratio"],
                    "similarity": snap["similarity_threshold"],
                }), flush=True)

        else:
            if stdio_mode:
                print(json.dumps({"error": f"unknown command: {action}"}), flush=True)


# ---------------------------------------------------------------------------
# Microphone listener
# ---------------------------------------------------------------------------

def _fit_to_size(chunk: np.ndarray, target_size: int) -> np.ndarray:
    """Zero-pad or truncate a chunk to exactly target_size samples."""
    if len(chunk) == target_size:
        return chunk
    if len(chunk) > target_size:
        return chunk[:target_size]
    out = np.zeros(target_size, dtype=chunk.dtype)
    out[:len(chunk)] = chunk
    return out


def listen_for_slaps(tracker, player, stdio_mode, fast_mode, profile: Optional[dict] = None):
    """Capture microphone input, detect slaps, and play audio responses."""
    slap_queue = queue.Queue()
    last_yell = [0.0]
    last_yell_lock = threading.Lock()
    chunk = 512 if fast_mode else CHUNK_SIZE

    def audio_callback(indata, frames, time_info, status):
        # Read state snapshot — keep this callback minimal
        with _state_lock:
            if _state["paused"]:
                return
            min_amp    = _state["min_amplitude"]
            cooldown_s = _state["cooldown_ms"] / 1000.0
            sim_thresh = _state["similarity_threshold"]

        rms = float(np.sqrt(np.mean(indata ** 2)))
        if rms < min_amp:
            return

        if profile is not None:
            fitted = _fit_to_size(indata[:, 0], profile["chunk_size"])
            spec = np.abs(np.fft.rfft(fitted))
            spec /= np.linalg.norm(spec) + 1e-10
            similarity = float(np.dot(spec, profile["spectrum"]))
            if similarity < sim_thresh:
                return

        now = time.time()
        with last_yell_lock:
            if now - last_yell[0] < cooldown_s:
                return
            last_yell[0] = now

        slap_queue.put(rms)

    try:
        stream = sd.InputStream(
            callback=audio_callback,
            channels=1,
            samplerate=SAMPLE_RATE,
            blocksize=chunk,
        )
    except sd.PortAudioError as e:
        print(f"Error: Could not open microphone: {e}", file=sys.stderr)
        sys.exit(1)

    with stream:
        while True:
            try:
                rms = slap_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            with _state_lock:
                speed = _state["speed_ratio"]
                vs    = _state["volume_scaling"]

            num, score = tracker.record()
            filepath = tracker.get_file(score)

            if stdio_mode:
                event = {
                    "timestamp":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "slapNumber": num,
                    "amplitude":  round(rms, 5),
                    "file":       os.path.basename(filepath),
                }
                print(json.dumps(event), flush=True)
            else:
                print(f"slap #{num} [amp={rms:.5f}] -> {os.path.basename(filepath)}")

            threading.Thread(
                target=play_audio,
                args=(filepath, rms, player, speed, vs),
                daemon=True,
            ).start()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="spankux",
        description="Detects slaps via microphone and plays audio responses.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--sexy", "-s", action="store_true",
                        help="Enable sexy mode (escalating intensity)")
    parser.add_argument("--halo", "-H", action="store_true",
                        help="Enable halo mode")
    parser.add_argument("--custom", "-c", default="", metavar="DIR",
                        help="Path to custom MP3 audio directory")
    parser.add_argument("--custom-files", default="", metavar="FILES",
                        help="Comma-separated list of custom MP3 files")
    parser.add_argument("--fast", action="store_true",
                        help="Faster detection: smaller audio chunks and shorter cooldown")
    parser.add_argument("--min-amplitude", type=float, default=None, metavar="F",
                        help=f"Minimum RMS amplitude threshold 0.0–1.0 "
                             f"(default: {DEFAULT_MIN_AMPLITUDE}, fast default: 0.18)")
    parser.add_argument("--cooldown", type=int, default=None, metavar="MS",
                        help=f"Cooldown between responses in ms "
                             f"(default: {DEFAULT_COOLDOWN_MS}, fast default: 350)")
    parser.add_argument("--stdio", action="store_true",
                        help="Enable JSON stdio interface for GUI integration")
    parser.add_argument("--volume-scaling", action="store_true",
                        help="Scale playback volume by slap amplitude")
    parser.add_argument("--speed", type=float, default=DEFAULT_SPEED_RATIO, metavar="F",
                        help="Playback speed multiplier (default: %(default)s)")
    parser.add_argument("--profile", default=None, metavar="PATH",
                        help="Path to profile.json produced by calibrate-spankux. "
                             "When set, spectral similarity is checked in addition to --min-amplitude.")
    parser.add_argument("--similarity", type=float, default=DEFAULT_SIMILARITY, metavar="F",
                        help=f"Minimum cosine similarity to profile (0.0–1.0, default: {DEFAULT_SIMILARITY}). "
                             f"Only active when --profile is set.")

    args = parser.parse_args()

    # Validate mutually exclusive modes
    if sum([args.sexy, args.halo, bool(args.custom or args.custom_files)]) > 1:
        parser.error("--sexy, --halo, and --custom/--custom-files are mutually exclusive")

    if args.speed <= 0:
        parser.error("--speed must be greater than 0")

    if not 0.0 <= args.similarity <= 1.0:
        parser.error("--similarity must be between 0.0 and 1.0")

    # Fast mode sets looser defaults; explicit flags override them
    min_amplitude = 0.18 if args.fast else DEFAULT_MIN_AMPLITUDE
    cooldown_ms   = 350  if args.fast else DEFAULT_COOLDOWN_MS

    if args.min_amplitude is not None:
        if not 0.0 < args.min_amplitude <= 1.0:
            parser.error("--min-amplitude must be between 0.0 and 1.0")
        min_amplitude = args.min_amplitude

    if args.cooldown is not None:
        if args.cooldown <= 0:
            parser.error("--cooldown must be greater than 0")
        cooldown_ms = args.cooldown

    # Load spectral profile
    profile = None
    if args.profile:
        try:
            with open(args.profile) as f:
                profile = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            parser.error(f"Could not read profile: {e}")
        if profile.get("sample_rate") != SAMPLE_RATE:
            parser.error(
                f"Profile sample rate ({profile.get('sample_rate')}) does not match "
                f"expected {SAMPLE_RATE} Hz"
            )
        profile["spectrum"] = np.array(profile["spectrum"])

    # Initialise global state
    with _state_lock:
        _state["min_amplitude"] = min_amplitude
        _state["cooldown_ms"]   = cooldown_ms
        _state["speed_ratio"]   = args.speed
        _state["volume_scaling"] = args.volume_scaling
        _state["similarity_threshold"] = args.similarity

    # Load audio files
    try:
        if args.custom_files:
            files = [f.strip() for f in args.custom_files.split(",")]
            for f in files:
                if not f.lower().endswith(".mp3"):
                    parser.error(f"custom file must be MP3: {f}")
                if not os.path.exists(f):
                    parser.error(f"custom file not found: {f}")
            mode, pack_name = "random", "custom"

        elif args.custom:
            files = load_audio_files(args.custom)
            mode, pack_name = "random", "custom"

        elif args.sexy:
            files = load_audio_files(SCRIPT_DIR / "audio" / "sexy")
            mode, pack_name = "escalation", "sexy"

        elif args.halo:
            files = load_audio_files(SCRIPT_DIR / "audio" / "halo")
            mode, pack_name = "random", "halo"

        else:
            files = load_audio_files(SCRIPT_DIR / "audio" / "pain")
            mode, pack_name = "random", "pain"

    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Find audio player
    player = find_player()
    if player is None:
        print(
            "Error: No audio player found. Install mpg123, ffplay (ffmpeg), or mpv.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.speed != 1.0 and player == "mpg123":
        print(
            "Warning: mpg123 does not support speed control. "
            "Install ffplay (ffmpeg) or mpv for --speed support.",
            file=sys.stderr,
        )

    tracker = SlapTracker(files, mode, cooldown_ms)

    if args.stdio:
        threading.Thread(target=read_stdin_commands, args=(True,), daemon=True).start()

    preset = "fast" if args.fast else "default"
    print(
        f"spankux: listening for slaps in {pack_name} mode "
        f"with {preset} tuning... (ctrl+c to quit)"
    )
    if args.stdio:
        print('{"status":"ready"}', flush=True)

    def handle_exit(sig, frame):
        print("\nbye!")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    listen_for_slaps(tracker, player, args.stdio, args.fast, profile)
    print("\nbye!")


if __name__ == "__main__":
    main()
