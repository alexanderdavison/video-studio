#!/usr/bin/env python3
"""Independent B-camera opportunity detector (v2) — event-grid blind.

Samples the B reel at 2 fps, 480x270, and looks for *sustained localised
articulated activity*: change concentrated in a small part of the frame
(hands, arms, gear) rather than a whole-frame shift (exposure drift, rig
movement).

  gain      one global linear gain over the whole stack (no per-frame
            normalisation - that would destroy the differences being measured)
  cells     8x6 mean |frame - previous frame| grid
  glob      mean cell change            (broad change)
  loc       max cell change             (peak local change)
  activ     sum(max(0, cell - glob))    (change above the frame's own background)
  artic     sum(max(0, cell - floor))   (change above a fixed noise floor,
                                         floor = 2.5 x quietest-frame background)
  episode   smoothed series (SMOOTH samples) -> local maxima -> NMS by peak
            separation -> run expanded around the peak while the smoothed
            series stays above RUN_FRAC x peak, at least MIN_RUN_S long

usage: detect2.py --raw FILE --dtype u8|u16 --metric loc|activ|artic [--out PREFIX]
"""
import argparse
import json

import numpy as np

W, H = 480, 270
FPS = 2.0
B_SS, B_FRAMES = 1016.0, 736
LAG = 1.2783
CX, CY = 8, 6
SMOOTH = 5                 # 2.5 s
RUN_FRAC = 0.60
MIN_RUN_S = 2.0
NMS_MIN_SEP_S = 15.0
TOP_N = 12
RANGE = (1020.0, 1380.0)
TARGET_P995 = 60.0         # delivered-like level for the gain


def load(path, dtype, nframes):
    dt = np.uint8 if dtype == 'u8' else np.uint16
    buf = np.fromfile(path, dtype=dt)
    n = W * H
    got = buf.size // n
    buf = buf[:got * n].reshape(got, H, W).astype(np.float32)
    if dtype == 'u16':
        buf /= 257.0
    return buf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--raw', default='/root/opp_audit/b_gray.raw')
    ap.add_argument('--dtype', default='u8', choices=['u8', 'u16'])
    ap.add_argument('--metric', default='artic', choices=['loc', 'activ', 'artic'])
    ap.add_argument('--out', default='/root/opp_audit/eps')
    a = ap.parse_args()

    b = load(a.raw, a.dtype, B_FRAMES)
    gain = TARGET_P995 / float(np.percentile(b, 99.5))
    b *= gain
    t_src = B_SS + (np.arange(b.shape[0]) + 0.5) / FPS
    t_perf = t_src + LAG
    diff = np.abs(np.diff(b, axis=0))

    ch, cw = H // CY, W // CX
    cm = diff[:, :ch * CY, :cw * CX].reshape(
        diff.shape[0], CY, ch, CX, cw).mean(axis=(2, 4))
    glob = cm.mean(axis=(1, 2))
    loc = cm.max(axis=(1, 2))
    idx = cm.reshape(cm.shape[0], -1).argmax(axis=1)
    cell = list(zip(idx // CX, idx % CX))
    activ = np.clip(cm - glob[:, None, None], 0, None).sum(axis=(1, 2))
    floor = 2.5 * float(np.percentile(glob, 10))
    artic = np.clip(cm - floor, 0, None).sum(axis=(1, 2))
    series = {'loc': loc, 'activ': activ, 'artic': artic}[a.metric]

    ker = np.ones(SMOOTH) / SMOOTH
    sm = np.convolve(series, ker, mode='same')
    luma = b[:-1].mean(axis=(1, 2))

    # local maxima
    peaks = [i for i in range(2, len(sm) - 2)
             if sm[i] == max(sm[i - 2:i + 3]) and RANGE[0] <= t_perf[i] <= RANGE[1]]
    peaks.sort(key=lambda i: -sm[i])
    kept = []
    for i in peaks:
        if all(abs(t_perf[i] - t_perf[k]) >= NMS_MIN_SEP_S for k in kept):
            kept.append(i)
        if len(kept) >= TOP_N:
            break

    eps = []
    for i in sorted(kept):
        pk = sm[i]
        lo = i
        while lo > 0 and sm[lo - 1] > RUN_FRAC * pk:
            lo -= 1
        hi = i
        while hi + 1 < len(sm) and sm[hi + 1] > RUN_FRAC * pk:
            hi += 1
        # clip the run to the audited range
        while lo < i and t_perf[lo] < RANGE[0]:
            lo += 1
        while hi > i and t_perf[hi] > RANGE[1]:
            hi -= 1
        seg = sm[lo:hi + 1] - RUN_FRAC * pk
        eps.append(dict(
            peak_idx=int(i), run=[int(lo), int(hi)],
            t0_perf=float(t_perf[lo]), t1_perf=float(t_perf[hi + 1]),
            peak_perf=float(t_perf[i]), dur_s=float((hi + 1 - lo) / FPS),
            mass=float(seg.sum()) / FPS, peak_sm=float(pk),
            peak_loc=float(loc[i]), peak_glob=float(glob[i]),
            peak_activ=float(activ[i]), peak_artic=float(artic[i]),
            conc=float(loc[i] / max(glob[i], 1e-6)),
            cell=[int(cell[i][0]), int(cell[i][1])],
            luma=float(luma[i]),
            rank_mass=0))
    eps.sort(key=lambda e: -e['mass'])
    for r, e in enumerate(eps, 1):
        e['rank_mass'] = r
    eps.sort(key=lambda e: e['peak_perf'])
    for k, e in enumerate(eps, 1):
        e['opp_id'] = f"{a.metric}_{k:02d}"

    json.dump(dict(metric=a.metric, raw=a.raw, dtype=a.dtype, gain=gain,
                   floor=float(floor),
                   params=dict(fps=FPS, cells=[CY, CX], smooth=SMOOTH,
                               run_frac=RUN_FRAC, min_run_s=MIN_RUN_S,
                               nms_min_sep_s=NMS_MIN_SEP_S, top_n=TOP_N, lag=LAG),
                   episodes=eps), open(f"{a.out}_{a.metric}.json", 'w'), indent=1)
    json.dump(dict(t_perf=[round(float(x), 3) for x in t_perf[:-1]],
                   sm=[round(float(x), 5) for x in sm],
                   loc=[round(float(x), 5) for x in loc],
                   glob=[round(float(x), 5) for x in glob],
                   activ=[round(float(x), 5) for x in activ],
                   artic=[round(float(x), 5) for x in artic],
                   luma=[round(float(x), 4) for x in luma]),
              open(f"{a.out}_series_{a.metric}.json", 'w'))

    print(f"metric={a.metric} raw={a.raw} gain={gain:.3f} floor={floor:.4f}")
    print(f"{'id':12s} {'t0':>8s} {'peak':>8s} {'t1':>8s} {'dur':>6s} {'mass':>7s} "
          f"{'loc':>6s} {'glob':>6s} {'conc':>6s} {'cell':>8s} {'luma':>6s}")
    for e in eps:
        print(f"{e['opp_id']:12s} {e['t0_perf']:8.2f} {e['peak_perf']:8.2f} "
              f"{e['t1_perf']:8.2f} {e['dur_s']:6.2f} {e['mass']:7.2f} "
              f"{e['peak_loc']:6.2f} {e['peak_glob']:6.2f} {e['conc']:6.2f} "
              f"{str(e['cell']):>8s} {e['luma']:6.2f}")


if __name__ == '__main__':
    main()
