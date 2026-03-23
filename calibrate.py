#!/usr/bin/env python3
"""
calibrate.py: Record microphone slap samples and produce a spectral profile
for use with spankux.py --profile.
"""

import argparse
import json
import sys
import time
from pathlib import Path

try:
    import numpy as np
    import sounddevice as sd
except ImportError:
    print("Error: Missing dependencies. Install with: pip install sounddevice numpy", file=sys.stderr)
    sys.exit(1)

SAMPLE_RATE = 44100
CHUNK_SIZE = 1024


def record_audio(duration: float) -> np.ndarray:
    """Record audio for a fixed duration and return as a 1-D float32 array."""
    frames = int(duration * SAMPLE_RATE)
    audio = sd.rec(frames, samplerate=SAMPLE_RATE, channels=1, dtype="float32")
    sd.wait()
    return audio[:, 0]


def measure_noise_floor(duration: float = 1.0) -> float:
    """Record silence and return mean RMS across chunks."""
    print(f"  Measuring noise floor ({duration:.0f}s of silence)...", end=" ", flush=True)
    audio = record_audio(duration)
    chunks = len(audio) // CHUNK_SIZE
    if chunks == 0:
        return 0.01
    rmss = [
        float(np.sqrt(np.mean(audio[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE] ** 2)))
        for i in range(chunks)
    ]
    floor = float(np.mean(rmss))
    print(f"done  (RMS = {floor:.5f})")
    return floor


def wait_for_transient(timeout: float, threshold: float) -> np.ndarray | None:
    """
    Record audio in CHUNK_SIZE blocks for up to `timeout` seconds.
    Return the first chunk whose RMS exceeds `threshold`, or None on timeout.
    """
    chunks_max = int(timeout * SAMPLE_RATE / CHUNK_SIZE) + 1
    result = [None]

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, blocksize=CHUNK_SIZE, dtype="float32") as stream:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            block, _ = stream.read(CHUNK_SIZE)
            chunk = block[:, 0]
            rms = float(np.sqrt(np.mean(chunk ** 2)))
            if rms >= threshold:
                return chunk.copy()

    return None


def compute_spectrum(chunk: np.ndarray) -> np.ndarray:
    """Return the L2-normalised rfft magnitude of a chunk."""
    mag = np.abs(np.fft.rfft(chunk, n=CHUNK_SIZE))
    mag /= np.linalg.norm(mag) + 1e-10
    return mag


def dominant_freq_range(mean_spectrum: np.ndarray) -> tuple[float, float]:
    """Return (low_hz, high_hz) for the band containing 80% of spectral energy."""
    freqs = np.fft.rfftfreq(CHUNK_SIZE, d=1.0 / SAMPLE_RATE)
    power = mean_spectrum ** 2
    cumsum = np.cumsum(power)
    total = cumsum[-1]
    lo_idx = int(np.searchsorted(cumsum, 0.10 * total))
    hi_idx = int(np.searchsorted(cumsum, 0.90 * total))
    return float(freqs[lo_idx]), float(freqs[min(hi_idx, len(freqs) - 1)])


def main():
    parser = argparse.ArgumentParser(
        prog="calibrate",
        description="Record slap samples and produce a spectral profile for spankux.",
    )
    parser.add_argument("--samples", type=int, default=5, metavar="N",
                        help="Number of slap samples to record (default: 5)")
    parser.add_argument("--output", default="profile.json", metavar="PATH",
                        help="Output path for the profile (default: profile.json)")
    args = parser.parse_args()

    if args.samples < 1:
        parser.error("--samples must be at least 1")

    output_path = Path(args.output)

    print("=== spankux microphone calibration ===")
    print()
    print("This script will record a few slap samples from your laptop's")
    print("microphone and build a spectral fingerprint used to reduce false")
    print("positives in slap detection.")
    print()

    # Noise floor
    try:
        noise_floor = measure_noise_floor(duration=1.0)
    except sd.PortAudioError as e:
        print(f"Error: Could not open microphone: {e}", file=sys.stderr)
        sys.exit(1)

    threshold = 3.0 * noise_floor
    print(f"  Detection threshold set to {threshold:.5f} (3× noise floor)")
    print()

    spectra = []
    rms_values = []
    i = 0

    while i < args.samples:
        print(f"Sample {i + 1}/{args.samples} — press Enter, then slap your laptop.")
        try:
            input()
        except EOFError:
            pass

        print("  Listening (2 s)...", end=" ", flush=True)
        chunk = wait_for_transient(timeout=2.0, threshold=threshold)

        if chunk is None:
            print("no slap detected. Try again.")
            continue

        rms = float(np.sqrt(np.mean(chunk ** 2)))
        print(f"captured  (RMS = {rms:.5f})")

        spectra.append(compute_spectrum(chunk))
        rms_values.append(rms)
        i += 1

    print()
    print("Computing profile...")

    mean_spectrum = np.mean(np.stack(spectra), axis=0)
    mean_spectrum /= np.linalg.norm(mean_spectrum) + 1e-10

    mean_rms = float(np.mean(rms_values))
    lo_hz, hi_hz = dominant_freq_range(mean_spectrum)

    profile = {
        "version": 1,
        "sample_rate": SAMPLE_RATE,
        "chunk_size": CHUNK_SIZE,
        "spectrum": mean_spectrum.tolist(),
        "mean_rms": round(mean_rms, 6),
        "n_samples": len(spectra),
    }

    with open(output_path, "w") as f:
        json.dump(profile, f)

    print(f"Profile saved to: {output_path}")
    print()
    print("Summary:")
    print(f"  Samples recorded   : {len(spectra)}")
    print(f"  Mean slap RMS      : {mean_rms:.5f}  (suggested --min-amplitude starting value)")
    print(f"  Dominant freq range: {lo_hz:.0f} – {hi_hz:.0f} Hz")
    print()
    print("Usage:")
    print(f"  python spankux.py --profile {output_path}")
    print(f"  python spankux.py --profile {output_path} --min-amplitude {mean_rms * 0.6:.3f}")


if __name__ == "__main__":
    main()
