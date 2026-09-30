#!/usr/bin/env python3
"""
LaLaDash Audio Diagnostic — run on the Pi to isolate engine sound issues.

Usage:  python3 Installation/diag_audio.py
  (or from venv:  source Installation/env/bin/activate && python3 Installation/diag_audio.py)

Tests:
  1. PyAudio device enumeration + default output info
  2. Pure sine-wave playback (callback mode) — tests if PyAudio callback works at all
  3. Pure sine-wave playback (blocking mode) — compare smoothness
  4. Callback timing jitter measurement — measures how often/late callbacks fire
  5. Numpy render cost — how long _render_block-style math takes on this CPU
  6. PulseAudio config check (sample rate, resampling method)
"""

import os
import sys
import time
import struct
import threading
import subprocess
import contextlib
import numpy as np

# ── Silence ALSA spam ─────────────────────────────────────────
@contextlib.contextmanager
def _silence_alsa():
    devnull = os.open(os.devnull, os.O_WRONLY)
    saved   = os.dup(2)
    os.dup2(devnull, 2)
    os.close(devnull)
    try:
        yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)

try:
    import pyaudio
except ImportError:
    print("ERROR: pyaudio not installed")
    sys.exit(1)

RATE   = 44100
BUFFER = 1024
CHANNELS = 2
FREQ   = 440.0  # A4 sine wave
DURATION = 3.0  # seconds per test

SEP = "=" * 60

def banner(title):
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


# ── Test 1: Device enumeration ─────────────────────────────────
def test_devices():
    banner("TEST 1: PyAudio Device Enumeration")
    with _silence_alsa():
        pa = pyaudio.PyAudio()

    info = pa.get_host_api_info_by_index(0)
    print(f"Host API: {info['name']}")
    print(f"Device count: {info['deviceCount']}")
    print(f"Default output device index: {info['defaultOutputDevice']}")
    print()

    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        if d['maxOutputChannels'] > 0:
            marker = " <-- DEFAULT" if i == info['defaultOutputDevice'] else ""
            print(f"  [{i}] {d['name']}  ch={d['maxOutputChannels']}  "
                  f"rate={d['defaultSampleRate']:.0f}  "
                  f"latency={d['defaultLowOutputLatency']*1000:.1f}ms{marker}")

    default = pa.get_device_info_by_index(info['defaultOutputDevice'])
    print(f"\nDefault device native rate: {default['defaultSampleRate']:.0f} Hz")
    if default['defaultSampleRate'] != RATE:
        print(f"  !! WARNING: Native rate != {RATE} Hz — PulseAudio will resample!")
        print(f"     This adds CPU overhead. Consider matching rates.")

    pa.terminate()


# ── Test 2: Callback sine wave ─────────────────────────────────
def test_callback():
    banner("TEST 2: Callback Mode Sine Wave (3 sec)")
    print("You should hear a clean, steady tone...")

    phase = [0.0]
    underruns = [0]

    def callback(in_data, frame_count, time_info, status):
        if status:
            underruns[0] += 1
        t = np.arange(frame_count) / RATE + phase[0]
        phase[0] += frame_count / RATE
        samples = (np.sin(2 * np.pi * FREQ * t) * 0.3 * 32767).astype(np.int16)
        stereo = np.column_stack([samples, samples]).flatten()
        return (stereo.tobytes(), pyaudio.paContinue)

    with _silence_alsa():
        pa = pyaudio.PyAudio()

    stream = pa.open(format=pyaudio.paInt16, channels=CHANNELS, rate=RATE,
                     output=True, frames_per_buffer=BUFFER,
                     stream_callback=callback)
    stream.start_stream()
    time.sleep(DURATION)
    stream.stop_stream()
    stream.close()
    pa.terminate()

    print(f"  Underruns reported by PortAudio: {underruns[0]}")
    if underruns[0] > 0:
        print("  !! Underruns detected — callback can't keep up")
    else:
        print("  OK — no underruns")


# ── Test 3: Blocking sine wave ─────────────────────────────────
def test_blocking():
    banner("TEST 3: Blocking Mode Sine Wave (3 sec)")
    print("Compare smoothness with Test 2...")

    with _silence_alsa():
        pa = pyaudio.PyAudio()

    stream = pa.open(format=pyaudio.paInt16, channels=CHANNELS, rate=RATE,
                     output=True, frames_per_buffer=BUFFER)

    phase = 0.0
    total_frames = int(RATE * DURATION)
    written = 0

    while written < total_frames:
        n = min(BUFFER, total_frames - written)
        t = np.arange(n) / RATE + phase
        phase += n / RATE
        samples = (np.sin(2 * np.pi * FREQ * t) * 0.3 * 32767).astype(np.int16)
        stereo = np.column_stack([samples, samples]).flatten()
        stream.write(stereo.tobytes())
        written += n

    stream.stop_stream()
    stream.close()
    pa.terminate()
    print("  Done — compare with Test 2")


# ── Test 4: Callback timing jitter ────────────────────────────
def test_jitter():
    banner("TEST 4: Callback Timing Jitter (3 sec)")

    timestamps = []
    expected_interval = BUFFER / RATE  # ~23.2 ms for 1024 @ 44100

    def callback(in_data, frame_count, time_info, status):
        timestamps.append(time.monotonic())
        n = frame_count
        out = np.zeros(n * CHANNELS, dtype=np.int16)
        return (out.tobytes(), pyaudio.paContinue)

    with _silence_alsa():
        pa = pyaudio.PyAudio()

    stream = pa.open(format=pyaudio.paInt16, channels=CHANNELS, rate=RATE,
                     output=True, frames_per_buffer=BUFFER,
                     stream_callback=callback)
    stream.start_stream()
    time.sleep(DURATION)
    stream.stop_stream()
    stream.close()
    pa.terminate()

    if len(timestamps) < 2:
        print("  ERROR: No callbacks fired!")
        return

    intervals = np.diff(timestamps) * 1000  # ms
    print(f"  Expected interval: {expected_interval*1000:.1f} ms")
    print(f"  Callbacks received: {len(timestamps)}")
    print(f"  Interval — mean: {np.mean(intervals):.1f} ms  "
          f"std: {np.std(intervals):.1f} ms  "
          f"max: {np.max(intervals):.1f} ms  "
          f"min: {np.min(intervals):.1f} ms")

    late = np.sum(intervals > expected_interval * 1000 * 2)  # > 2× expected
    print(f"  Late callbacks (>2× expected): {late}/{len(intervals)}")

    if np.max(intervals) > 100:
        print("  !! PROBLEM: Some callbacks delayed >100ms — likely GIL contention")
    elif np.std(intervals) > 5:
        print("  !! WARNING: High jitter — may cause audible glitches")
    else:
        print("  OK — timing looks stable")


# ── Test 5: Render cost benchmark ─────────────────────────────
def test_render_cost():
    banner("TEST 5: Render Cost Benchmark")

    # Simulate what _render_block does: 6 voices with resampling + mixing
    n = BUFFER
    num_voices = 6
    pcm_len = 44100 * 2  # 2 second loops
    pcms = [np.random.randn(pcm_len).astype(np.float32) * 0.3 for _ in range(num_voices)]
    positions = [0.0] * num_voices
    rates = [0.8, 1.0, 1.2, 1.0, 0.9, 1.1]
    gains = [0.8, 0.3, 0.5, 0.4, 0.25, 0.2]

    iterations = 500

    start = time.monotonic()
    for _ in range(iterations):
        mix = np.zeros(n, dtype=np.float32)
        for v in range(num_voices):
            src = pcms[v]
            L = len(src)
            step = rates[v]
            pos = positions[v]
            idx = pos + step * np.arange(n, dtype=np.float64)
            idx %= L
            i0 = idx.astype(np.int32)
            frac = (idx - i0).astype(np.float32)
            i1 = (i0 + 1) % L
            out = ((1.0 - frac) * src[i0] + frac * src[i1]) * gains[v]
            mix += out.astype(np.float32)
            positions[v] = float((pos + step * n) % L)

        peak = np.max(np.abs(mix))
        if peak > 1.0:
            mix /= peak

        stereo = np.column_stack([mix, mix])
        _ = (np.clip(stereo, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()

    elapsed = time.monotonic() - start
    per_block_us = (elapsed / iterations) * 1e6
    budget_us = (n / RATE) * 1e6  # time available per callback

    print(f"  Block size: {n} samples ({n/RATE*1000:.1f} ms)")
    print(f"  Budget per callback: {budget_us:.0f} µs")
    print(f"  Render cost (6 voices): {per_block_us:.0f} µs  ({per_block_us/budget_us*100:.1f}% of budget)")

    if per_block_us > budget_us * 0.5:
        print("  !! WARNING: Render takes >50% of callback budget!")
        print("     Consider larger buffer or fewer voices")
    elif per_block_us > budget_us * 0.3:
        print("  !! Note: Render takes 30-50% of budget — tight on Pi")
    else:
        print("  OK — plenty of headroom")


# ── Test 6: PulseAudio config ─────────────────────────────────
def test_pulse_config():
    banner("TEST 6: PulseAudio / System Audio Config")

    # Check if PulseAudio is running
    try:
        result = subprocess.run(["pactl", "info"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            print("  PulseAudio info:")
            for line in result.stdout.strip().split('\n'):
                line = line.strip()
                if any(k in line.lower() for k in ['sample', 'rate', 'resample', 'default sink', 'server name']):
                    print(f"    {line}")
        else:
            print("  PulseAudio not running or pactl not available")
    except FileNotFoundError:
        print("  pactl not found — PulseAudio may not be installed")
    except Exception as e:
        print(f"  PulseAudio check error: {e}")

    # Check PulseAudio daemon.conf
    pulse_conf_paths = [
        os.path.expanduser("~/.config/pulse/daemon.conf"),
        "/etc/pulse/daemon.conf",
    ]
    for path in pulse_conf_paths:
        if os.path.exists(path):
            print(f"\n  {path}:")
            try:
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith(';') and not line.startswith('#'):
                            if any(k in line for k in ['sample-rate', 'resample', 'fragment', 'default-fragments']):
                                print(f"    {line}")
            except Exception as e:
                print(f"    Error reading: {e}")

    # Check ALSA config
    print("\n  ALSA info:")
    try:
        result = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if 'card' in line.lower():
                    print(f"    {line.strip()}")
        else:
            print("    aplay -l failed")
    except FileNotFoundError:
        print("    aplay not found")
    except Exception as e:
        print(f"    {e}")

    # CPU info
    print("\n  CPU info:")
    try:
        with open("/proc/cpuinfo") as f:
            model = [l for l in f if l.startswith("model name") or l.startswith("Model")]
            if model:
                print(f"    {model[0].strip()}")
    except Exception:
        pass

    try:
        nproc = os.cpu_count()
        print(f"    CPU cores: {nproc}")
    except Exception:
        pass


# ── Test 7: Callback + GIL contention simulation ──────────────
def test_gil_contention():
    banner("TEST 7: Callback Under GIL Contention (3 sec)")
    print("Simulating Qt + OBD threads competing for GIL...")

    timestamps = []
    underruns = [0]

    def callback(in_data, frame_count, time_info, status):
        if status:
            underruns[0] += 1
        timestamps.append(time.monotonic())
        n = frame_count
        # Simulate actual render work
        mix = np.zeros(n, dtype=np.float32)
        for _ in range(4):
            src = np.random.randn(n).astype(np.float32) * 0.1
            mix += src
        stereo = np.column_stack([mix, mix]).flatten()
        out = (np.clip(stereo, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
        return (out, pyaudio.paContinue)

    # GIL contenders — simulate what Qt main loop + OBD polling do
    stop_event = threading.Event()

    def gil_hog_qt():
        """Simulate Qt event processing — short frequent GIL holds"""
        while not stop_event.is_set():
            _ = [i * i for i in range(500)]
            time.sleep(0.005)

    def gil_hog_obd():
        """Simulate OBD polling — longer GIL holds every 50ms"""
        while not stop_event.is_set():
            _ = list(range(2000))
            _ = {str(i): i for i in range(200)}
            time.sleep(0.05)

    with _silence_alsa():
        pa = pyaudio.PyAudio()

    stream = pa.open(format=pyaudio.paInt16, channels=CHANNELS, rate=RATE,
                     output=True, frames_per_buffer=BUFFER,
                     stream_callback=callback)

    t1 = threading.Thread(target=gil_hog_qt, daemon=True)
    t2 = threading.Thread(target=gil_hog_obd, daemon=True)

    stream.start_stream()
    t1.start()
    t2.start()
    time.sleep(DURATION)
    stop_event.set()
    stream.stop_stream()
    stream.close()
    pa.terminate()

    if len(timestamps) < 2:
        print("  ERROR: No callbacks fired!")
        return

    intervals = np.diff(timestamps) * 1000
    expected = BUFFER / RATE * 1000

    print(f"  Callbacks: {len(timestamps)}")
    print(f"  Interval — mean: {np.mean(intervals):.1f} ms  "
          f"std: {np.std(intervals):.1f} ms  "
          f"max: {np.max(intervals):.1f} ms")
    print(f"  Underruns: {underruns[0]}")

    late = np.sum(intervals > expected * 2)
    print(f"  Late callbacks: {late}/{len(intervals)}")

    # Compare with test 4
    print("\n  Compare with Test 4 (no contention) to see GIL impact")


# ── Main ───────────────────────────────────────────────────────
if __name__ == "__main__":
    # Redirect ALL output to a file on the Desktop so it can be read without a terminal
    OUTPUT_PATH = os.path.expanduser("~/Desktop/LaLaDash_AudioDiag.txt")
    _real_stdout = sys.stdout
    _log_file = open(OUTPUT_PATH, "w")

    class _Tee:
        """Write to both file and stdout (if available)."""
        def __init__(self, *streams):
            self.streams = streams
        def write(self, msg):
            for s in self.streams:
                try:
                    s.write(msg)
                    s.flush()
                except Exception:
                    pass
        def flush(self):
            for s in self.streams:
                try:
                    s.flush()
                except Exception:
                    pass

    sys.stdout = _Tee(_log_file, _real_stdout)

    print("\n" + "=" * 60)
    print("  LaLaDash Audio Diagnostic Tool")
    print("=" * 60)

    tests = [
        ("1", "Device Enumeration",        test_devices),
        ("2", "Callback Sine Wave",        test_callback),
        ("3", "Blocking Sine Wave",        test_blocking),
        ("4", "Callback Timing Jitter",    test_jitter),
        ("5", "Render Cost Benchmark",     test_render_cost),
        ("6", "PulseAudio / System Config", test_pulse_config),
        ("7", "GIL Contention Simulation", test_gil_contention),
    ]

    if len(sys.argv) > 1:
        selected = sys.argv[1:]
    else:
        selected = [t[0] for t in tests]

    for num, name, func in tests:
        if num in selected:
            try:
                func()
            except Exception as e:
                print(f"\n  !! TEST {num} FAILED: {e}")

    banner("DONE")
    print(f"Results saved to: {OUTPUT_PATH}\n")

    _log_file.close()
    sys.stdout = _real_stdout
