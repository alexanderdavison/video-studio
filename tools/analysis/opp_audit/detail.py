#!/usr/bin/env python3
"""Timing detail: for one opportunity, every legal B candidate that overlaps it.

usage: detail.py <U0> <U1> [--prefix ...]"""
import json
import sys

CX = None


def overload(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def events():
    out = []
    for sec, path in (('s1', '/root/opp_audit/sync_1020_1200.json'),
                      ('s2', '/root/opp_audit/sync_1200_1380.json')):
        d = json.load(open(path))
        for e in d['events']:
            c = e['context']
            for x in e['candidates']:
                if x['angle'] != 'B':
                    continue
                out.append(dict(sec=sec, ev=e['event_id'], anchor=c['beat'],
                                id=x['id'], tl0=x['timeline_start'], tl1=x['timeline_end'],
                                dur=x['duration_s'], cov=x['scores']['coverage']))
    return out


def report(U0, U1):
    ev = events()
    rows = []
    for x in ev:
        ov = overload(U0, U1, x['tl0'], x['tl1'])
        if ov <= 0:
            continue
        rows.append(dict(ev=x['ev'], anchor=x['anchor'], id=x['id'], entry=x['tl0'],
                         exit=x['tl1'], dur=x['dur'],
                         entry_vs_onset=x['tl0'] - U0, tail_vs_end=x['tl1'] - U1,
                         ov=ov, ovf=ov / (U1 - U0), cov=x['cov']))
    rows.sort(key=lambda r: r['entry'])
    print(f"opportunity {U0:.2f} -> {U1:.2f}  ({U1-U0:.2f} s)")
    print(f"{'event':7s} {'anchor':>8s} {'cand':9s} {'entry':>8s} {'exit':>8s} {'dur':>5s} "
          f"{'entry-U0':>8s} {'exit-U1':>8s} {'ovf%':>6s} {'Bcov':>5s}")
    for r in rows:
        print(f"{r['ev']:7s} {r['anchor']:8.2f} {r['id']:9s} {r['entry']:8.2f} {r['exit']:8.2f} "
              f"{r['dur']:5.2f} {r['entry_vs_onset']:8.2f} {r['tail_vs_end']:8.2f} "
              f"{r['ovf']*100:6.1f} {r['cov']:5.2f}")
    # candidate entries that could plausibly serve: within +/-2 s of onset
    near = [r for r in rows if -2.0 <= r['entry_vs_onset'] <= 2.0]
    print(f"  entries within +/-2.0 s of onset: {[ (r['ev'], r['id']) for r in near] or 'NONE'}")
    full = [r for r in rows if r['entry_vs_onset'] <= 2.0 and r['tail_vs_end'] >= -1.0]
    print(f"  candidates entering <=+2.0 s and reaching the action end: {[ (r['ev'], r['id']) for r in full] or 'NONE'}")


if __name__ == '__main__':
    report(float(sys.argv[1]), float(sys.argv[2]))
