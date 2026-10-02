#!/usr/bin/env python3
"""Opportunity x candidate-grid comparison and coverage summary.

Loads episode sets from one or more detector metric runs, clusters them into
consensus opportunities, then classifies each against the existing B candidate
grid.  Also measures the A camera's localised activity at the same moments as a
context column (does B carry change A does not?).

usage: classify.py --primary artic --eps 'loc,activ,artic' --label u16
"""
import argparse
import json

import numpy as np

W, H = 480, 270
FPS = 2.0
A_SS, A_FRAMES = 1018.0, 732
LAG = 1.2783
CX, CY = 8, 6
TARGET_P995 = 60.0

ENTRY_EARLY_TOL = 2.0
ENTRY_LATE_TOL = 1.0
MISS_OVF = 0.25
COVER_OVF = 0.80
CLUSTER_S = 6.0


def load_u8(path, nframes):
    n = W * H
    buf = np.fromfile(path, dtype=np.uint8)
    got = buf.size // n
    return buf[:got * n].reshape(got, H, W).astype(np.float32)


def load_u16(path, nframes):
    n = W * H
    buf = np.fromfile(path, dtype=np.uint16)
    got = buf.size // n
    arr = buf[:got * n].reshape(got, H, W).astype(np.float32)
    return arr / 257.0


def localised_series(path, load, nframes, ss, lag):
    b = load(path, nframes)
    gain = TARGET_P995 / float(np.percentile(b, 99.5))
    b *= gain
    t_perf = ss + (np.arange(b.shape[0]) + 0.5) / FPS + lag
    diff = np.abs(np.diff(b, axis=0))
    ch, cw = H // CY, W // CX
    cm = diff[:, :ch * CY, :cw * CX].reshape(
        diff.shape[0], CY, ch, CX, cw).mean(axis=(2, 4))
    glob = cm.mean(axis=(1, 2))
    loc = cm.max(axis=(1, 2))
    return t_perf[:-1], glob, loc


def overload(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--primary', default='artic')
    ap.add_argument('--eps', default='loc,activ,artic')
    ap.add_argument('--prefix', default='/root/opp_audit/eps_u16')
    ap.add_argument('--bpix', default='/root/opp_audit/b_gray16.raw')
    ap.add_argument('--bpixdtype', default='u16')
    ap.add_argument('--a', default='/root/opp_audit/a_gray.raw')
    ap.add_argument('--out', default='/root/opp_audit/coverage_u16.json')
    args = ap.parse_args()

    metrics = args.eps.split(',')
    sets = {}
    for m in metrics:
        d = json.load(open(f"{args.prefix}_{m}.json"))
        sets[m] = d['episodes']

    # ---- cluster across metrics -------------------------------------------
    all_eps = [(m, e) for m in metrics for e in sets[m]]
    all_eps.sort(key=lambda x: x[1]['peak_perf'])
    clusters, cur = [], []
    for m, e in all_eps:
        if cur and e['peak_perf'] - max(x[1]['peak_perf'] for x in cur) > CLUSTER_S:
            clusters.append(cur)
            cur = []
        cur.append((m, e))
    if cur:
        clusters.append(cur)
    cons = []
    for c in clusters:
        ms = sorted({m for m, _ in c})
        if len(ms) < 2:
            continue
        t0 = float(np.median([e['t0_perf'] for _, e in c]))
        t1 = float(np.median([e['t1_perf'] for _, e in c]))
        pk = float(np.median([e['peak_perf'] for _, e in c]))
        cons.append(dict(metrics=ms, votes=len(ms), t0=t0, t1=t1, peak=pk,
                         dur=t1 - t0,
                         members=[dict(metric=m, t0=e['t0_perf'], t1=e['t1_perf'],
                                       peak=e['peak_perf'], dur=e['dur_s'],
                                       mass=e['mass'], loc=e['peak_loc'],
                                       glob=e['peak_glob'], conc=e['conc'],
                                       cell=e['cell'])
                                  for m, e in c]))
    cons.sort(key=lambda x: x['peak'])

    # ---- A counterpart ----------------------------------------------------
    a_t, a_glob, a_loc = localised_series(args.a, load_u8, A_FRAMES, A_SS, 0.0)

    def a_at(t, half=2.0):
        m = (a_t >= t - half) & (a_t <= t + half)
        return float(a_loc[m].max()) if m.any() else float('nan')

    # ---- event grid -------------------------------------------------------
    events = []
    for sec, path in (('s1', '/root/opp_audit/sync_1020_1200.json'),
                      ('s2', '/root/opp_audit/sync_1200_1380.json')):
        d = json.load(open(path))
        for e in d['events']:
            c = e['context']
            cands = [dict(id=x['id'], tl0=x['timeline_start'], tl1=x['timeline_end'],
                          dur=x['duration_s'], action=x['action'],
                          coverage=x['scores']['coverage'])
                     for x in e['candidates'] if x['angle'] == 'B']
            events.append(dict(section=sec, event=e['event_id'], anchor=c['beat'],
                               face0=min(x['tl0'] for x in cands),
                               face1=max(x['tl1'] for x in cands),
                               cands=cands))
    events.sort(key=lambda e: e['anchor'])

    rows = []
    for i, o in enumerate(cons, 1):
        U0, U1 = o['t0'], o['t1']
        L = max(U1 - U0, 1e-6)
        best = None
        for ev in events:
            for c in ev['cands']:
                ov = overload(U0, U1, c['tl0'], c['tl1'])
                if best is None or ov > best['ov']:
                    best = dict(ov=ov, ev=ev, c=c)
        ovf = best['ov'] / L
        entry = best['c']['tl0']
        exits = [c['tl1'] for ev in events for c in ev['cands']
                 if overload(U0, U1, c['tl0'], c['tl1']) > 0]
        near = min(events, key=lambda e: abs(e['anchor'] - o['peak']))
        in_face = [ev['event'] for ev in events
                   if overload(U0, U1, ev['face0'], ev['face1']) > 0]
        # any legal face at all covering the onset?
        onset_cover = [ev['event'] for ev in events
                       if ev['face0'] <= U0 + 0.5 and ev['face1'] >= U1 - 0.5]
        if ovf < MISS_OVF:
            cls = 'MISSED'
        elif ovf < COVER_OVF:
            cls = 'PARTIALLY COVERED'
        elif entry < U0 - ENTRY_EARLY_TOL or entry > U0 + ENTRY_LATE_TOL:
            cls = 'PARTIALLY COVERED'
        else:
            cls = 'COVERED'
        rows.append(dict(
            opp=f"opp_{i:02d}", votes=o['votes'], metrics=o['metrics'],
            t0=U0, t1=U1, peak=o['peak'], dur=L,
            a_loc_at_peak=a_at(o['peak']),
            nearest_event=near['event'], nearest_section=near['section'],
            nearest_anchor=near['anchor'], delta_peak_vs_anchor=o['peak'] - near['anchor'],
            best_candidate_event=best['ev']['event'], best_candidate_id=best['c']['id'],
            best_candidate_tl0=best['c']['tl0'], best_candidate_tl1=best['c']['tl1'],
            best_candidate_dur=best['c']['dur'],
            overlap_s=best['ov'], overlap_frac=ovf,
            entry=entry, entry_vs_onset=entry - U0,
            best_exit=(max(exits) if exits else None),
            in_face_events=in_face, onset_covering_faces=onset_cover,
            classification=cls, members=o['members']))

    ct = {k: sum(1 for r in rows if r['classification'] == k)
          for k in ('COVERED', 'PARTIALLY COVERED', 'MISSED')}
    n = len(rows)
    summary = dict(opportunities=n, **ct,
                   coverage_rate_strict=ct['COVERED'] / n if n else 0.0,
                   coverage_rate_weighted=(ct['COVERED'] + 0.5 * ct['PARTIALLY COVERED']) / n if n else 0.0)

    json.dump(dict(primary=args.primary, metrics=metrics, rows=rows,
                   summary=summary), open(args.out, 'w'), indent=1)

    print(f"consensus opportunities (>=2 of {len(metrics)} metric variants): {n}")
    print(f"{'opp':6s} {'t0':>8s} {'peak':>8s} {'t1':>8s} {'dur':>5s} {'v':>2s} "
          f"{'A_loc':>6s} {'near':>7s} {'dPK':>7s} {'best':>7s} {'cand':>9s} "
          f"{'ovf%':>6s} {'ent-U0':>7s} {'exit':>8s}  class")
    for r in rows:
        print(f"{r['opp']:6s} {r['t0']:8.2f} {r['peak']:8.2f} {r['t1']:8.2f} {r['dur']:5.1f} "
              f"{r['votes']:2d} {r['a_loc_at_peak']:6.2f} {r['nearest_event']:7s} "
              f"{r['delta_peak_vs_anchor']:7.2f} {r['best_candidate_event']:7s} "
              f"{r['best_candidate_id']:9s} {r['overlap_frac']*100:6.1f} "
              f"{r['entry_vs_onset']:7.2f} "
              f"{(r['best_exit'] if r['best_exit'] is not None else float('nan')):8.2f}  "
              f"{r['classification']}")
    print(json.dumps(summary, indent=1))


if __name__ == '__main__':
    main()
