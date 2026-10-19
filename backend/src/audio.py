"""Audio cleanup for the clinician-facing copy of a recording.

The original upload is always kept untouched. The cleaned copy is only meant
for listening (e.g. on a phone speaker): it must not be used as input to the
screening model, because denoising changes jitter/shimmer/HNR/MFCCs.
"""
import noisereduce as nr
import numpy as np
import pyloudnorm as pyln
import soundfile as sf
from scipy.signal import butter, resample_poly, sosfilt

TARGET_SR = 16000
TARGET_LUFS = -16.0
PEAK_CEILING = 0.89  # about -1 dBFS
SILENCE_RMS = 1e-4   # below this the recording is treated as empty


class AudioError(ValueError):
    """The upload cannot be used (unreadable, too short/long, or silent)."""


def read_mono(path, max_seconds):
    try:
        data, sr = sf.read(path, dtype="float32", always_2d=True)
    except Exception as exc:
        raise AudioError("Could not read the audio file. Please record again.") from exc
    y = data.mean(axis=1)
    if len(y) / sr > max_seconds + 1:
        raise AudioError(f"Recording is too long (limit {max_seconds} seconds).")
    if sr != TARGET_SR:
        g = np.gcd(int(sr), TARGET_SR)
        y = resample_poly(y, TARGET_SR // g, int(sr) // g).astype("float32")
    return y


def _trim_silence(y, sr, pad=0.25, frame=0.02):
    n = int(sr * frame)
    if len(y) < n * 4:
        return y
    frames = len(y) // n
    rms = np.sqrt((y[: frames * n].reshape(frames, n) ** 2).mean(axis=1))
    loud = np.where(rms > max(rms.max() * 0.05, SILENCE_RMS))[0]
    if len(loud) == 0:
        return y
    start = max(0, int((loud[0] * frame - pad) * sr))
    end = min(len(y), int(((loud[-1] + 1) * frame + pad) * sr))
    return y[start:end]


def clean(y, sr=TARGET_SR):
    """High-pass, denoise, trim silence, loudness-normalise, limit peaks."""
    if np.sqrt(np.mean(y**2)) < SILENCE_RMS:
        raise AudioError("The recording is silent. Check your microphone and try again.")

    sos = butter(4, 80, btype="highpass", fs=sr, output="sos")
    y = sosfilt(sos, y).astype("float32")

    y = nr.reduce_noise(y=y, sr=sr, stationary=True, prop_decrease=0.8).astype("float32")
    y = _trim_silence(y, sr)

    try:
        loudness = pyln.Meter(sr).integrated_loudness(y)
        if np.isfinite(loudness):
            y = pyln.normalize.loudness(y, loudness, TARGET_LUFS).astype("float32")
    except ValueError:
        pass  # clip shorter than the 0.4 s loudness block; fall through to peak scaling

    peak = float(np.max(np.abs(y)))
    if peak > PEAK_CEILING:
        y = y * (PEAK_CEILING / peak)
    elif 0 < peak < 0.1:
        y = y * (0.1 / peak)  # very quiet clip the LUFS step could not handle
    return y


def process_upload(original_path, clean_path, min_seconds, max_seconds):
    """Validate the upload and write the cleaned copy. Returns duration in seconds."""
    y = read_mono(original_path, max_seconds)
    duration = len(y) / TARGET_SR
    if duration < min_seconds:
        raise AudioError(f"Recording is too short (at least {min_seconds} seconds needed).")
    cleaned = clean(y)
    sf.write(clean_path, cleaned, TARGET_SR, subtype="PCM_16")
    return duration
