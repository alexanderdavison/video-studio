#!/usr/bin/env python3
"""Final coverage analysis for the 1020-1380 s candidate-opportunity audit.

Produces, from the detector outputs and the two candidate artifacts:
  1. consensus opportunities (>=2 of 3 detector metric variants)
  2. classification against the existing B candidate grid, with a declared rule
  3. a core-window sensitivity classification (peak +/- 2.5 s)
  4. grid-hole analysis: where the candidate grid offers NO legal B at all,
     and what the B camera is doing there
  5. a dense scan for strong activity runs with < 25 % legal coverage
"""
import json

import numpy as np

W, H = 480, 270
FPS = 2.0
CLUSTER_S = 6.0
ENTRY_EARLY = 2.0
ENTRY_LATE = 2.0
TAIL_TOL = 1.0
MISS_OVF = 0.25
CORE_HALF = 2.5
PREFIX = '/root/opp_audit/eps_u16'
METRICS = ['loc', 'activ', 'artic']
PRIMARY = 'artic'
RANGE = (1020.0, 1380.0)


def overload(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def load_events():
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
                               face1=max(x['tl1'] for x in cands), cands=cands))
    events.sort(key=lambda e: e['anchor'])
    return events


def classify(U0, U1, events):
    """Declared rule. Returns (class, detail)."""
    cands = [(ev, c) for ev in events for c in ev['cands']]
    L = max(U1 - U0, 1e-6)
    best = max(cands, key=lambda ec: overload(U0, U1, ec[1]['tl0'], ec[1]['tl1']), default=None)
    ov = overload(U0, U1, best[1]['tl0'], best[1]['tl1']) if best else 0.0
    ovf = ov / L
    on_time = [ec for ec in cands if U0 - ENTRY_EARLY <= ec[1]['tl0'] <= U0 + ENTRY_LATE]
    good = [ec for ec in on_time if ec[1]['tl1'] >= U1 - TAIL_TOL]
    exits = [c['tl1'] for _, c in cands if overload(U0, U1, c['tl0'], c['tl1']) > 0]
    if good:
        ec = min(good, key=lambda ec: abs(ec[1]['tl0'] - U0))
        cls = 'COVERED'
    elif ovf >= MISS_OVF:
        cls = 'PARTIALLY COVERED'
        ec = best
    else:
        cls = 'MISSED'
        ec = best
    flags = []
    if cls == 'PARTIALLY COVERED':
        if not on_time:
            pre = [ec2 for ec2 in cands if ec2[1]['tl0'] < U0]
            post = [ec2 for ec2 in cands if ec2[1]['tl0'] > U0]
            if pre and post:
                flags.append('nearest entries straddle the onset '
                             '(%.2f s before / %.2f s after)' % (pre[-1][1]['tl0'] - U0,
                                                                post[0][1]['tl0'] - U0))
            elif pre:
                flags.append('only early entries (latest %.2f s before onset)'
                             % (U0 - pre[-1][1]['tl0']))
            elif post:
                flags.append('only late entries (earliest %.2f s after onset)'
                             % (post[0][1]['tl0'] - U0))
        if exits and max(exits) < U1 - TAIL_TOL:
            short = (U1 - TAIL_TOL) - max(exits)
            flags.append('longest offered exit falls %.2f s short of the action end' % short)
            if short < 0.5:
                flags.append('BORDERLINE')
    return cls, dict(overlap=ov, ovf=ovf, entry=ec[1]['tl0'] if ec else None,
                     exit=ec[1]['tl1'] if ec else None,
                     best_exit=(max(exits) if exits else None),
                     on_time_entries=[(ev['event'], c['id'], c['tl0'] - U0, c['tl1'] - U1)
                                      for ev, c in on_time],
                     flags=flags)


def main():
    sets = {m: json.load(open(f"{PREFIX}_{m}.json"))['episodes'] for m in METRICS}
    events = load_events()

    # ---- consensus clustering --------------------------------------------
    all_eps = sorted([(m, e) for m in METRICS for e in sets[m]],
                     key=lambda x: x[1]['peak_perf'])
    clusters, cur = [], []
    for m, e in all_eps:
        if cur and e['peak_perf'] - max(x[1]['peak_perf'] for x in cur) > CLUSTER_S:
            clusters.append(cur)
            cur = []
        cur.append((m, e))
    if cur:
        clusters.append(cur)

    rows = []
    for c in clusters:
        ms = sorted({m for m, _ in c})
        if len(ms) < 2:
            continue
        t0 = float(np.median([e['t0_perf'] for _, e in c]))
        t1 = float(np.median([e['t1_perf'] for _, e in c]))
        pk = float(np.median([e['peak_perf'] for _, e in c]))
        cls, det = classify(t0, t1, events)
        ccls, cdet = classify(pk - CORE_HALF, pk + CORE_HALF, events)
        near = min(events, key=lambda e: abs(e['anchor'] - pk))
        rows.append(dict(opp=f"opp_{len(rows)+1:02d}", metrics=ms, votes=len(ms),
                         t0=t0, t1=t1, peak=pk, dur=t1 - t0,
                         peak_loc=float(np.median([e['peak_loc'] for _, e in c])),
                         peak_glob=float(np.median([e['peak_glob'] for _, e in c])),
                         peak_conc=float(np.median([e['conc'] for _, e in c])),
                         peak_cell=[int(np.median([e['cell'][0] for _, e in c])),
                                    int(np.median([e['cell'][1] for _, e in c]))],
                         nearest_event=near['event'], nearest_section=near['section'],
                         nearest_anchor=near['anchor'], delta_peak_vs_anchor=pk - near['anchor'],
                         classification=cls, core_classification=ccls,
                         members=[dict(metric=m, t0=e['t0_perf'], t1=e['t1_perf'],
                                       peak=e['peak_perf'], dur=e['dur_s'], mass=e['mass'],
                                       loc=e['peak_loc'], glob=e['peak_glob'],
                                       cell=e['cell']) for m, e in c],
                         **det, **{('core_' + k): v for k, v in cdet.items()}))

    # ---- grid holes -------------------------------------------------------
    faces = sorted([(ev['face0'], ev['face1']) for ev in events])
    merged = []
    for a, b in faces:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    holes = []
    prev = RANGE[0]
    for a, b in merged:
        if a > prev + 1e-9:
            holes.append([max(prev, RANGE[0]), min(a, RANGE[1])])
        prev = max(prev, b)
    if prev < RANGE[1]:
        holes.append([prev, RANGE[1]])
    holes = [h for h in holes if h[1] > h[0] and h[0] < RANGE[1] and h[1] > RANGE[0]]

    ser = json.load(open(f"{PREFIX}_series_{PRIMARY}.json"))
    t = np.array(ser['t_perf'])
    inr = (t >= RANGE[0]) & (t <= RANGE[1])
    sm = np.array(ser['sm'])
    base = sm[inr]
    hole_rows = []
    for a, b in holes:
        m = (t >= a) & (t <= b)
        if not m.any():
            continue
        hole_rows.append(dict(start=a, end=b, dur=b - a, n=int(m.sum()),
                              mean=float(sm[m].mean()),
                              peak=float(sm[m].max()),
                              mean_pct=float((base < sm[m].mean()).mean() * 100),
                              peak_pct=float((base < sm[m].max()).mean() * 100)))

    # ---- dense scan for strong runs with poor coverage --------------------
    thr = float(np.median(sm[inr]))
    dense = []
    for i in range(2, len(sm) - 2):
        if not (RANGE[0] <= t[i] <= RANGE[1]):
            continue
        if sm[i] != max(sm[i - 2:i + 3]) or sm[i] < thr:
            continue
        pk = sm[i]
        lo = i
        while lo > 0 and sm[lo - 1] > 0.6 * pk:
            lo -= 1
        hi = i
        while hi + 1 < len(sm) and sm[hi + 1] > 0.6 * pk:
            hi += 1
        dense.append((t[i], t[lo], t[hi + 1], (hi + 1 - lo) / FPS, pk))
    dense.sort(key=lambda x: -x[4])
    poor = []
    for pk_t, t0, t1, dur, val in dense:
        if dur < 2.0:
            continue
        cands = [(ev, c) for ev in events for c in ev['cands']]
        best = max(cands, key=lambda ec: overload(t0, t1, ec[1]['tl0'], ec[1]['tl1']))
        ovf = overload(t0, t1, best[1]['tl0'], best[1]['tl1']) / (t1 - t0)
        if ovf < MISS_OVF:
            poor.append(dict(peak=float(pk_t), t0=float(t0), t1=float(t1), dur=float(dur),
                             ovf=float(ovf),
                             strength_pct=float((base < val).mean() * 100)))

    ct = {k: sum(1 for r in rows if r['classification'] == k)
          for k in ('COVERED', 'PARTIALLY COVERED', 'MISSED')}
    cct = {k: sum(1 for r in rows if r['core_classification'] == k)
           for k in ('COVERED', 'PARTIALLY COVERED', 'MISSED')}
    n = len(rows)
    summary = dict(
        opportunities=n, **ct,
        coverage_rate_strict=ct['COVERED'] / n if n else 0.0,
        coverage_rate_weighted=(ct['COVERED'] + 0.5 * ct['PARTIALLY COVERED']) / n if n else 0.0,
        core=dict(**cct, coverage_rate_strict=cct['COVERED'] / n if n else 0.0),
        hole_seconds=float(sum(h['dur'] for h in hole_rows)),
        range_seconds=RANGE[1] - RANGE[0],
        hole_fraction=float(sum(h['dur'] for h in hole_rows) / (RANGE[1] - RANGE[0])),
    )
    json.dump(dict(summary=summary, rows=rows, holes=hole_rows, poor_coverage=dense and poor,
                   rule=dict(entry_early_s=ENTRY_EARLY, entry_late_s=ENTRY_LATE,
                             tail_tol_s=TAIL_TOL, miss_ovf=MISS_OVF, core_half_s=CORE_HALF,
                             cluster_s=CLUSTER_S, primary=PRIMARY, metrics=METRICS)),
              open('/root/opp_audit/final.json', 'w'), indent=1)

    print(f"consensus opportunities: {n}   (>=2 of {len(METRICS)} metric variants agree)")
    print(f"{'opp':6s} {'t0':>7s} {'peak':>7s} {'t1':>7s} {'dur':>5s} {'v':>2s} "
          f"{'near':>7s} {'dPK':>6s} {'cls':18s} {'core':18s} {'ovf%':>5s} {'entry-U0':>8s}")
    for r in rows:
        print(f"{r['opp']:6s} {r['t0']:7.2f} {r['peak']:7.2f} {r['t1']:7.2f} {r['dur']:5.1f} "
              f"{r['votes']:2d} {r['nearest_event']:7s} {r['delta_peak_vs_anchor']:6.2f} "
              f"{r['classification']:18s} {r['core_classification']:18s} "
              f"{r['ovf']*100:5.1f} {r['entry'] - r['t0']:8.2f}")
    for r in rows:
        if r['flags']:
            print(f"   {r['opp']}: " + '; '.join(r['flags']))
    print()
    print("candidate-grid holes over 1020-1380 s:")
    for h in hole_rows:
        print(f"   {h['start']:8.3f} -> {h['end']:8.3f}  {h['dur']:5.2f} s  "
              f"B activity mean {h['mean']:6.2f} (pct {h['mean_pct']:5.1f}) "
              f"peak {h['peak']:6.2f} (pct {h['peak_pct']:5.1f})")
    print(f"   total hole time {summary['hole_seconds']:.2f} s of {summary['range_seconds']:.0f} s "
          f"({summary['hole_fraction']*100:.1f} %)")
    print()
    print(f"dense scan: strong runs (>=2 s, >= median) with <25 % legal coverage: {len(poor)}")
    for p in poor[:12]:
        print(f"   peak {p['peak']:8.2f}  run {p['t0']:8.2f}-{p['t1']:8.2f} ({p['dur']:4.1f} s) "
              f"ovf {p['ovf']*100:5.1f} %  strength pct {p['strength_pct']:5.1f}")
    print()
    print(json.dumps(summary, indent=1))


if __name__ == '__main__':
    main()
