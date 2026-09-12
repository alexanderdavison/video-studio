#!/usr/bin/env python3
"""Multicam chroma sync: find where A window appears inside B (GoPro vs DJI).
Chroma (musical notes) is robust to mic EQ differences where raw waveform fails.
Usage: python multicam-sync.py <A_wav_or_mp4> <B_wav_or_mp4> [A_win_start] [A_win_dur]
Output: B offset + waveform PNGs."""
import numpy as np
import subprocess
import wave
import sys
import os

SR = 8000
HOP = 512

def extract_wav(src, out, ss=None, t=None, vol=None):
    cmd = ["ffmpeg", "-y", "-v", "error", "-vn"]
    if ss is not None: cmd += ["-ss", str(ss)]
    if t is not None: cmd += ["-t", str(t)]
    cmd += ["-i", src, "-ac", "1", "-ar", str(SR)]
    if vol is not None: cmd += ["-af", f"volume={vol}"]
    cmd += ["-c:a", "pcm_s16le", out]
    subprocess.run(cmd, check=True)

def load(p):
    w = wave.open(p); n = w.getnframes()
    return np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0

def main():
    a_src, b_src = sys.argv[1], sys.argv[2]
    a_ss = float(sys.argv[3]) if len(sys.argv) > 3 else 180
    a_dur = float(sys.argv[4]) if len(sys.argv) > 4 else 200

    print("extract (B boosted x10, audio only)...", flush=True)
    extract_wav(a_src, "/tmp/mc_a.wav", ss=a_ss, t=a_dur)
    extract_wav(b_src, "/tmp/mc_b.wav", vol=10.0)
    a = load("/tmp/mc_a.wav"); b = load("/tmp/mc_b.wav")
    print(f"A window: {len(a)/SR:.0f}s | B: {len(b)/SR:.0f}s", flush=True)

    import librosa
    ca = librosa.feature.chroma_stft(y=a, sr=SR, hop_length=HOP)
    cb = librosa.feature.chroma_stft(y=b, sr=SR, hop_length=HOP)
    ca = ca / (np.linalg.norm(ca, axis=0, keepdims=True) + 1e-9)
    cb = cb / (np.linalg.norm(cb, axis=0, keepdims=True) + 1e-9)

    nA, nB = ca.shape[1], cb.shape[1]
    total = np.zeros(nA + nB - 1)
    for band in range(12):
        total += np.abs(np.fft.ifft(
            np.fft.fft(cb[band], nA+nB-1) * np.conj(np.fft.fft(ca[band], nA+nB-1))).real)
    valid = total[:nB-nA+1]
    idx = int(np.argmax(valid))
    b_start = idx * HOP / SR

    def sim_at(off_s, win=30):
        off = int(off_s*SR/HOP)
        if off+win > nB or off+win > nA: return 0.0
        sims = []
        for k in range(0, win, 5):
            x = cb[:, off+k:off+k+5]; y = ca[:, k:k+5]
            sims.append(float(np.sum(x*y)))
        return float(np.mean(sims))
    cand = sim_at(b_start)
    rng = np.random.default_rng()
    rand = np.mean([sim_at(rng.integers(0, max(1, nB-nA))) for _ in range(5)])

    print(f"CHROMA: A window (A@{a_ss}s) appears in B at {b_start:.2f}s", flush=True)
    print(f"  => B@{b_start + a_dur/2:.1f}s == A@{a_ss + a_dur/2:.1f}s", flush=True)
    print(f"  verification: candidate {cand:.3f} vs random {rand:.3f}", flush=True)

    # waveforms
    outdir = os.path.dirname(os.path.abspath(sys.argv[0])) or "."
    for name, src in [("a", "/tmp/mc_a.wav"), ("b", "/tmp/mc_b.wav")]:
        subprocess.run(["ffmpeg","-y","-v","error","-i",src,
                        "-filter_complex","showwavespic=s=1600x400:colors=#FFB03A",
                        "-frames:v","1",f"{outdir}/wave_{name}.png"], check=True)
        print(f"  waveform: {outdir}/wave_{name}.png", flush=True)

if __name__ == "__main__":
    main()
