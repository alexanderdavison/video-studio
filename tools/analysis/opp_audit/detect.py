#!/usr/bin/env python3
"""Independent B-camera opportunity audit for the ISH-D 1020-1380 s range.

Method (deterministic, no model, no event-grid input):

  1. B reel is sampled at 2 fps into 480x270 8-bit grey frames over source
     1016.0 -> 1384.0 s (performance = source + 1.2783).
  2. Consecutive-sample absolute difference maps are reduced to an 8x6 cell grid.
     Per sample we measure how much change is *localised* (max cell above the
     frame's own background) versus *global* (whole-frame mean). Global change
     (exposure drift, rig shift) is subtracted so what remains is articulated
     movement - hands, arms, gear.
  3. The localised series is smoothed (1.5 s) and thresholded robustly
     (median + K*MAD). Runs above threshold, merged across <= 1.5 s dips and at
     least MIN_RUN_S long, are "episodes" - sustained localised activity.
  4. Episodes are ranked by excess mass above threshold and non-max-suppressed
     so no two reported opportunities share the same action.

The event grid (candidate artifacts) is loaded ONLY at the comparison stage.
"""
import json
import sys

import numpy as np

W, H = 480, 270
FPS = 2.0
B_SS, B_FRAMES = 1016.0, 736          # source seconds, frames
LAG = 1.2783                          # performance = B source + LAG
CX, CY = 8, 6                         # cell grid
SMOOTH = 3                            # samples (1.5 s)
K_MAD = 1.5                           # robust threshold
MERGE_GAP_S = 1.5
MIN_RUN_S = 2.5
NMS_MIN_SEP_S = 12.0
TOP_N = 14
RANGE = (1020.0, 1380.0)


def load(path, nframes):
    n = W * H
    buf = np.fromfile(path, dtype=np.uint8)
    got = buf.size // n
    if got < nframes:
        print(f"WARN: {path}: {got}/{nframes} frames", file=sys.stderr)
    buf = buf[:got * n].reshape(got, H, W)
    return buf.astype(np.float32)


def cells(diffmap):
    """(N-1, CY, CX) mean |diff| per cell."""
    n, h, w = diffmap.shape
    ch, cw = h // CY, w // CX
    d = diffmap[:, :ch * CY, :cw * CX]
    return d.reshape(n, CY, ch, CX, cw).mean(axis=(2, 4))


def main():
    b = load('/root/opp_audit/b_gray.raw', B_FRAMES)
    # sample i -> source time B_SS + i/FPS ; diff i is between i and i+1
    t_src = B_SS + (np.arange(b.shape[0]) + 0.5) / FPS
    t_perf = t_src + LAG
    diff = np.abs(np.diff(b, axis=0))

    cm = cells(diff)                                   # (N-1, CY, CX)
    glob = cm.mean(axis=(1, 2))
    loc = cm.max(axis=(1, 2))
    flat = cm.reshape(cm.shape[0], -1)
    idx = flat.argmax(axis=1)
    cam_row, cam_col = idx // CX, idx % CX
    # localised change = total cell change ABOVE the frame's own background level
    activ = np.clip(cm - glob[:, None, None], 0, None).sum(axis=(1, 2))
    conc = loc / np.maximum(glob, 1e-6)
    luma = b[:-1].mean(axis=(1, 2))

    def smooth(x, k=SMOOTH):
        ker = np.ones(k) / k
        return np.convolve(x, ker, mode='same')

    asm = smooth(activ)
    med = float(np.median(asm))
    mad = float(np.median(np.abs(asm - med))) * 1.4826
    thr = med + K_MAD * mad

    above = asm > thr
    runs = []
    i = 0
    n = len(above)
    while i < n:
        if not above[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and above[j + 1]:
            j += 1
        runs.append([i, j])
        i = j + 1
    # merge runs separated by <= MERGE_GAP_S
    gap_n = int(round(MERGE_GAP_S * FPS))
    merged = []
    for r in runs:
        if merged and r[0] - merged[-1][1] <= gap_n:
            merged[-1][1] = r[1]
        else:
            merged.append(list(r))

    eps = []
    for a, z in merged:
        if (z - a + 1) / FPS < MIN_RUN_S:
            continue
        seg = asm[a:z + 1] - thr
        mass = float(seg.sum()) / FPS                       # excess activity-seconds
        peak = int(a + np.argmax(asm[a:z + 1]))
        eps.append(dict(
            i0=a, i1=z, peak=peak,
            t0_src=float(t_src[a]), t1_src=float(t_src[z + 1]),
            peak_src=float(t_src[peak]),
            dur_s=float((z + 1 - a) / FPS),
            mass=mass,
            peak_activ=float(asm[peak]),
            peak_loc=float(loc[peak]), peak_glob=float(glob[peak]),
            peak_conc=float(conc[peak]),
            peak_cell=[int(cam_row[peak]), int(cam_col[peak])],
            peak_luma=float(luma[peak]),
            min_luma=float(luma[a:z + 1].min()),
            frac_low_glob=float((glob[a:z + 1] < 0.35).mean()),
        ))
    eps.sort(key=lambda e: -e['mass'])
    kept = []
    for e in eps:
        if all(abs(e['peak_src'] - k['peak_src']) >= NMS_MIN_SEP_S for k in kept):
            kept.append(e)
        if len(kept) >= TOP_N:
            break
    kept.sort(key=lambda e: e['peak_src'])

    # clip to the audited performance range
    out = []
    for k, e in enumerate(kept, 1):
        e = dict(e)
        e['opp_id'] = f"opp_{k:02d}"
        e['t0_perf'] = e['t0_src'] + LAG
        e['t1_perf'] = e['t1_src'] + LAG
        e['peak_perf'] = e['peak_src'] + LAG
        p0 = max(e['t0_perf'], RANGE[0])
        p1 = min(e['t1_perf'], RANGE[1])
        e['clipped'] = (p0 != e['t0_perf']) or (p1 != e['t1_perf']) or p1 < p0
        out.append(e)
    out = [e for e in out if not e['clipped']]

    series = dict(t_perf=[round(float(x), 3) for x in t_perf[:-1]],
                  activ=[round(float(x), 6) for x in activ],
                  activ_sm=[round(float(x), 6) for x in asm],
                  glob=[round(float(x), 6) for x in glob],
                  loc=[round(float(x), 6) for x in loc],
                  conc=[round(float(x), 4) for x in conc],
                  luma=[round(float(x), 3) for x in luma])
    json.dump(series, open('/root/opp_audit/b_series.json', 'w'))
    json.dump(dict(params=dict(fps=FPS, cells=[CY, CX], smooth=SMOOTH, k_mad=K_MAD,
                               merge_gap_s=MERGE_GAP_S, min_run_s=MIN_RUN_S,
                               nms_min_sep_s=NMS_MIN_SEP_S, top_n=TOP_N,
                               b_ss=B_SS, lag=LAG),
                   stats=dict(median=med, mad=mad, thr=thr,
                              activ_min=float(activ.min()), activ_max=float(activ.max())),
                   episodes=out), open('/root/opp_audit/b_episodes.json', 'w'), indent=1)

    print(f"frames={b.shape[0]} samples={len(asm)} thr={thr:.5f} med={med:.5f} mad={mad:.5f}")
    print(f"{'id':8s} {'perf t0':>9s} {'peak':>9s} {'perf t1':>9s} {'dur':>6s} {'mass':>8s} "
          f"{'loc':>7s} {'glob':>7s} {'conc':>6s} {'cell':>6s} {'luma':>6s}")
    for e in out:
        print(f"{e['opp_id']:8s} {e['t0_perf']:9.2f} {e['peak_perf']:9.2f} {e['t1_perf']:9.2f} "
              f"{e['dur_s']:6.2f} {e['mass']:8.1f} {e['peak_loc']:7.2f} {e['peak_glob']:7.2f} "
              f"{e['peak_conc']:6.2f} {str(e['peak_cell']):>7s} {e['peak_luma']:6.1f}")


if __name__ == '__main__':
    main()
