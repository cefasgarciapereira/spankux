<p align="center">
  <img src="doc/logo.png" alt="spankux logo" width="200">
</p>

# spankux

**English** | [简体中文][readme-zh-link]

Slap your laptop, it yells back.

> "this is the most amazing thing i've ever seen" — [@kenwheeler](https://x.com/kenwheeler)

> "I just ran sexy mode with my wife sitting next to me...We died laughing" — [@duncanthedev](https://x.com/duncanthedev)

> "peak engineering" — [@tylertaewook](https://x.com/tylertaewook)

A Linux-focused Python fork of [taigrr/spank](https://github.com/taigrr/spank/), which plays audio responses when you slap your laptop. The original relies on the Apple Silicon accelerometer (an IMU built into the hardware); this fork takes a simpler approach that works on any machine — it listens through the microphone and detects slaps by their acoustic signature.

## Requirements

- Python 3.10+
- One of: `mpg123`, `ffplay` (ffmpeg), or `mpv`

```bash
sudo apt install mpg123   # or: ffmpeg / mpv
```

## Install

**From GitHub Releases (recommended):**

```bash
pip install https://github.com/cefas/spankux/releases/latest/download/spankux-0.1.0-py3-none-any.whl
```

This installs the `spankux` and `calibrate-spankux` commands with all Python dependencies bundled.

**From source:**

```bash
git clone https://github.com/cefas/spankux
cd spankux
pip install -e .
```

## Usage

```bash
# Normal mode — says "ow!" when slapped
spankux

# Sexy mode — escalating responses based on slap frequency
spankux --sexy

# Halo mode — plays Halo death sounds when slapped
spankux --halo

# Fast mode — smaller audio chunks and shorter cooldown
spankux --fast
spankux --sexy --fast

# Custom mode — plays MP3s from a directory
spankux --custom /path/to/mp3s

# Custom mode — plays specific MP3 files
spankux --custom-files file1.mp3,file2.mp3

# Adjust amplitude threshold (lower = more sensitive)
spankux --min-amplitude 0.10   # more sensitive
spankux --min-amplitude 0.25   # less sensitive

# Set cooldown between responses in milliseconds (default: 750)
spankux --cooldown 500

# Set playback speed multiplier (default: 1.0)
spankux --speed 0.7   # slower and deeper
spankux --speed 1.5   # faster

# Scale playback volume by how hard you slap
spankux --volume-scaling

# Enable JSON stdio interface for GUI integration
spankux --stdio
```

### Modes

**Pain mode** (default): Randomly plays from pain/protest audio clips when a slap is detected.

**Sexy mode** (`--sexy`): Tracks slaps within a rolling window. The more you slap, the more intense the audio response. Escalation across many levels.

**Halo mode** (`--halo`): Randomly plays death sound effects from the Halo video game series.

**Custom mode** (`--custom` / `--custom-files`): Randomly plays MP3 files from a directory or an explicit file list.

### Detection tuning

`--fast` uses smaller audio chunks for lower latency and sets a shorter cooldown (350 ms) and a higher default amplitude threshold (0.18). You can still override individual values with `--min-amplitude` and `--cooldown`.

#### Sensitivity

`--min-amplitude` (default `0.05`) controls the minimum microphone RMS level required to trigger a response:

- `0.05–0.10` — very sensitive, detects light taps
- `0.15–0.30` — balanced
- `0.30–0.50` — only strong impacts trigger sounds

## Spectral calibration (reduce false positives)

By default, any loud sound that exceeds the amplitude threshold can trigger a response. The optional calibration workflow records a few actual slap samples and builds a frequency-domain fingerprint of your specific laptop's slap sound. Detection then requires both sufficient loudness *and* spectral similarity to that fingerprint, which greatly reduces false triggers from voices, music, or desk bumps.

### Step 1 — record a profile

```bash
calibrate-spankux
```

Follow the prompts: the script measures your ambient noise floor, then asks you to slap your laptop a few times. It saves `profile.json` in the current directory and prints a suggested `--min-amplitude` value.

```bash
# Customise number of samples and output path
calibrate-spankux --samples 7 --output my_profile.json
```

### Step 2 — use the profile

```bash
spankux --profile profile.json

# Adjust cosine-similarity threshold (0.0–1.0, default 0.80)
spankux --profile profile.json --similarity 0.70   # more permissive
spankux --profile profile.json --similarity 0.90   # stricter
```

The `--similarity` value is the minimum [cosine similarity](https://en.wikipedia.org/wiki/Cosine_similarity) between an incoming audio chunk's FFT spectrum and the stored profile. Lower values accept a broader range of sounds; higher values require a closer match.

## JSON stdio interface

Pass `--stdio` to enable machine-readable JSON output and accept commands on stdin. Useful for GUI wrappers.

**Events emitted on stdout:**

```json
{"status": "ready"}
{"timestamp": "2026-03-22T14:00:00Z", "slapNumber": 1, "amplitude": 0.31415, "file": "pain_01.mp3"}
```

**Commands accepted on stdin:**

```json
{"cmd": "pause"}
{"cmd": "resume"}
{"cmd": "status"}
{"cmd": "volume-scaling"}
{"cmd": "set", "amplitude": 0.15}
{"cmd": "set", "cooldown": 500}
{"cmd": "set", "speed": 1.2}
{"cmd": "set", "similarity": 0.75}
```

## How it works

1. Opens the default microphone via `sounddevice` at 44 100 Hz.
2. For each audio chunk (~23 ms), computes the RMS amplitude.
3. If RMS exceeds `--min-amplitude` and the cooldown has elapsed, a slap is registered.
4. **Optional spectral gate** (`--profile`): the chunk's FFT magnitude is compared to the stored profile via cosine similarity; chunks that don't match are ignored.
5. **Optional volume scaling** (`--volume-scaling`): light taps play quietly, hard slaps play at full volume.
6. **Optional speed control** (`--speed`): adjusts playback speed and pitch.
7. The selected MP3 is played in a background thread using `mpg123`, `ffplay`, or `mpv`.

## Credits

Original concept and audio assets by [taigrr/spank](https://github.com/taigrr/spank/).

## License

MIT

<!-- Links -->
[readme-zh-link]: ./README-zh.md
