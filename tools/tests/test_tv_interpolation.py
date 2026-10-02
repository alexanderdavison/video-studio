#!/usr/bin/env python3
"""test_tv_interpolation.py — a time-varying match must survive interpolation intact.

The renderer interpolates a time-varying camera match between control points. Two things must hold,
and both have failed or nearly failed in this pipeline already:

  1. the LUT blends (a correction cannot jump or vanish between control points)
  2. any eq terms attached to the same pre-grade (gamma, saturation) are BLENDED, not dropped -
     interp_curve originally re-emitted only `curves=`, which would have applied the correction at
     control times and silently discarded it in between ("configured but not applied")

Also asserts the wrapper behaves at and beyond the control-point ends (clamping, never extrapolating).

Exit: 0 = PASS, 1 = FAIL, 2 = error.
"""
import importlib.util
import sys

RENDERER = "/opt/video-studio/tools/proxy/render_proxy.py"

spec = importlib.util.spec_from_file_location("rp", RENDERER)
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)

C1 = "curves=r='0.0000/0.0000 0.5000/0.4000 1.0000/0.9000',eq=gamma=0.9000,eq=saturation=0.8000"
C2 = "curves=r='0.0000/0.0000 0.5000/0.6000 1.0000/1.0000',eq=gamma=1.0000,eq=saturation=1.0000"
points = [{"t": 0.0, "curve": C1}, {"t": 100.0, "curve": C2}]

fails = []


def check(name, cond, detail=""):
    print("  %-52s %s%s" % (name, "PASS" if cond else "FAIL", ("  " + detail) if detail else ""))
    if not cond:
        fails.append(name)


mid = rp.curve_at(points, 50.0)
lut_mid, ex_mid = rp.split_pre(mid)
_, ex1 = rp.split_pre(C1)
_, ex2 = rp.split_pre(C2)

check("midpoint LUT blends (not one end copied)", "0.5000" in lut_mid and mid != C1 and mid != C2)
check("midpoint keeps eq terms", set(ex_mid) == {"gamma", "saturation"}, str(ex_mid))
check("midpoint blends gamma", abs(ex_mid.get("gamma", 0) - 0.95) < 1e-6, str(ex_mid.get("gamma")))
check("midpoint blends saturation",
      abs(ex_mid.get("saturation", 0) - 0.90) < 1e-6, str(ex_mid.get("saturation")))
check("no term silently dropped anywhere in 0..100",
      all(set(rp.split_pre(rp.curve_at(points, t))[1]) == {"gamma", "saturation"}
          for t in range(0, 101, 5)))
check("at a control point the exact curve is used", rp.curve_at(points, 0.0) == C1)
check("before the first point it clamps (no extrapolation)", rp.curve_at(points, -50.0) == C1)
check("after the last point it clamps", rp.curve_at(points, 999.0) == C2)

# a term present on only one side must be carried, not lost
half = rp.curve_at([{"t": 0.0, "curve": "curves=r='0/0 1/1',eq=gamma=0.9"},
                    {"t": 10.0, "curve": "curves=r='0/0 1/1'"}], 5.0)
_, ex_half = rp.split_pre(half)
check("one-sided eq term survives interpolation", "gamma" in ex_half, str(ex_half))

# sidecar fields (gamma/sat) must be folded into the effective curve when eq terms are absent
sidecar = [{"t": 0.0, "curve": "curves=r='0/0 1/1'", "gamma": 0.9, "sat": 0.8},
           {"t": 10.0, "curve": "curves=r='0/0 1/1'", "gamma": 1.0, "sat": 1.0}]
mid_side = rp.curve_at(sidecar, 5.0)
_, ex_side = rp.split_pre(mid_side)
check("sidecar gamma/sat are applied when curve has no eq terms",
      set(ex_side) == {"gamma", "saturation"}, str(ex_side))
check("sidecar values interpolate at midpoint",
      abs(ex_side.get("gamma", 0) - 0.95) < 1e-6 and
      abs(ex_side.get("saturation", 0) - 0.90) < 1e-6,
      str(ex_side))

# the blended curve must be monotone per channel (a non-monotone LUT bulges)
pts = rp.parse_curve(lut_mid)["r"]
check("blended LUT stays monotone", all(pts[i][1] <= pts[i + 1][1] + 1e-9
                                       for i in range(len(pts) - 1)))

if fails:
    print("\nFAIL: %s" % ", ".join(fails))
    sys.exit(1)
print("\nPASS: time-varying interpolation preserves the LUT and the eq terms, and clamps at the ends")
