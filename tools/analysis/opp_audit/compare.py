#!/usr/bin/env python3
"""Compare independently detected B opportunities against the candidate event grid.

Input : /root/opp_audit/b_episodes.json  (detector output, event-grid-blind)
        /root/opp_audit/sync_1020_1200.json, sync_1200_1380.json (candidate artifacts)
Output: /root/opp_audit/coverage.json

Coordinate note: in these artifacts the program timeline position of a B insert
equals the performance time (a_offset = 0 and the sync invariant makes
timeline_position - source_in = 1.2783), so a candidate's timeline_start /
timeline_end are directly comparable to a performance-time opportunity interval.

Classification rule (declared up front, applied mechanically):
  best  = the B candidate, over all 38 events, with the largest time overlap with
          the opportunity interval [U0, U1]; ovf = overlap / (U1 - U0)
  missing  : ovf < 0.25
  partial  : 0.25 <= ovf < 0.80
             or ovf >= 0.80 but the candidate's entry is mis-timed, i.e.
             entry < U0 - 2.0 s (offered camera would have to run before the
             action) or entry > U0 + 1.0 s (part of the action lost)
  covered  : ovf >= 0.80 and entry within [U0 - 2.0, U0 + 1.0]
"""
import json

ENTRY_EARLY_TOL = 2.0
ENTRY_LATE_TOL = 1.0
MISS_OVF = 0.25
COVER_OVF = 0.80


def overload(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def main():
    eps = json.load(open('/root/opp_audit/b_episodes.json'))['episodes']
    events = []
    for sec, path in (('s1', '/root/opp_audit/sync_1020_1200.json'),
                      ('s2', '/root/opp_audit/sync_1200_1380.json')):
        d = json.load(open(path))
        for e in d['events']:
            c = e['context']
            cands = [dict(id=x['id'], tl0=x['timeline_start'], tl1=x['timeline_end'],
                          src0=x['start'], dur=x['duration_s'], action=x['action'])
                     for x in e['candidates'] if x['angle'] == 'B']
            events.append(dict(section=sec, event=e['event_id'], anchor=c['beat'],
                               entry=c['sync_entry']['entry_source_s'],
                               entry_tl=c['sync_entry']['timeline_s'],
                               face0=min(x['tl0'] for x in cands),
                               face1=max(x['tl1'] for x in cands),
                               cands=cands))
    events.sort(key=lambda e: e['anchor'])

    rows = []
    for ep in eps:
        U0, U1 = ep['t0_perf'], ep['t1_perf']
        L = U1 - U0
        best = None
        for ev in events:
            for c in ev['cands']:
                ov = overload(U0, U1, c['tl0'], c['tl1'])
                if best is None or ov > best['ov']:
                    best = dict(ov=ov, ev=ev, c=c)
        ovf = best['ov'] / L if L > 0 else 0.0
        entry = best['c']['tl0']
        # strongest legal exit available for this action: latest candidate end
        # (any event) whose span overlaps the opportunity
        exits = [c['tl1'] for ev in events for c in ev['cands']
                 if overload(U0, U1, c['tl0'], c['tl1']) > 0]
        best_exit = max(exits) if exits else None
        # nearest event by anchor to the opportunity peak
        near = min(events, key=lambda e: abs(e['anchor'] - ep['peak_perf']))
        delta = ep['peak_perf'] - near['anchor']
        # events whose face span overlaps the opportunity at all
        overlapping = [ev for ev in events if overload(U0, U1, ev['face0'], ev['face1']) > 0]

        if ovf < MISS_OVF:
            cls = 'MISSED'
        elif ovf < COVER_OVF:
            cls = 'PARTIALLY COVERED'
        else:
            if entry < U0 - ENTRY_EARLY_TOL or entry > U0 + ENTRY_LATE_TOL:
                cls = 'PARTIALLY COVERED'
            else:
                cls = 'COVERED'
        if cls == 'COVERED' and not (overlapping and near is not None):
            cls = 'PARTIALLY COVERED'
        rows.append(dict(
            opp=ep['opp_id'], t0=U0, t1=U1, peak=ep['peak_perf'], dur=ep['dur_s'],
            mass=ep['mass'], loc=ep['peak_loc'], glob=ep['peak_glob'],
            conc=ep['peak_conc'], cell=ep['peak_cell'], luma=ep['peak_luma'],
            nearest_event=near['event'] if near else None,
            nearest_anchor=near['anchor'] if near else None,
            nearest_section=near['section'] if near else None,
            delta_peak_vs_anchor=delta,
            best_candidate_event=best['ev']['event'], best_candidate_id=best['c']['id'],
            best_candidate_tl0=best['c']['tl0'], best_candidate_tl1=best['c']['tl1'],
            best_candidate_dur=best['c']['dur'],
            overlap_s=best['ov'], overlap_frac=ovf,
            entry=entry, best_exit=best_exit,
            entry_vs_onset=entry - U0,
            overlapping_events=[ev['event'] for ev in overlapping],
            classification=cls,
        ))

    cls_ct = {k: sum(1 for r in rows if r['classification'] == k)
              for k in ('COVERED', 'PARTIALLY COVERED', 'MISSED')}
    n = len(rows)
    summary = dict(opportunities=n, **cls_ct,
                   coverage_rate_strict=cls_ct['COVERED'] / n if n else 0,
                   coverage_rate_weighted=(cls_ct['COVERED'] + 0.5 * cls_ct['PARTIALLY COVERED']) / n if n else 0)

    json.dump(dict(rows=rows, summary=summary), open('/root/opp_audit/coverage.json', 'w'), indent=1)

    print(f"{'opp':7s} {'perf t0':>8s} {'peak':>8s} {'t1':>8s} {'cls':17s} "
          f"{'nearEv':7s} {'dPK':>7s} {'bestEv':7s} {'bestId':10s} {'ovf%':>6s} {'entry-U0':>9s} {'exit':>8s}")
    for r in rows:
        print(f"{r['opp']:7s} {r['t0']:8.2f} {r['peak']:8.2f} {r['t1']:8.2f} {r['classification']:17s} "
              f"{r['nearest_event']:7s} {r['delta_peak_vs_anchor']:7.2f} {r['best_candidate_event']:7s} "
              f"{r['best_candidate_id']:10s} {r['overlap_frac']*100:6.1f} {r['entry_vs_onset']:9.2f} "
              f"{(r['best_exit'] or float('nan')):8.2f}")
    print()
    print("summary:", json.dumps(summary))


if __name__ == '__main__':
    main()
