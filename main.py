#!/usr/bin/env python3
"""
OrbitX — Orbital Transport Autopilot Lab
=========================================
by Darion Kellar

SIM-ONLY educational lab for orbital transport: circular orbits,
Hohmann transfers, delta-v budgets, rendezvous + docking corridor,
launch interlocks, reentry, and ground-station pass prediction.
Pure Python stdlib, ONE main.py, zero network, zero dependencies.
Nothing here launches, flies, or controls any real spacecraft —
every live action is behind a HOLD gate, and the HOLD always wins.

OrbitX COMPLETES the X transportation family:
  Flightx = air, YachtX = water, RVx = road, HoverX = low altitude,
  TeleportX = teleport, OrbitX = space.

What is modelled, with real orbital mechanics:
  1. Circular orbit .......... period + velocity from Kepler, vis-viva
  2. Hohmann transfer ......... LEO->GEO delta-v budget + transfer time
  3. Delta-v budget table .... standard reference values, labelled
  4. Rendezvous .............. phasing + docking-corridor governor
  5. Launch interlocks ....... X-family style: all must pass first
  6. Reentry ................. Allen-Eggers peak g, Sutton-Graves heating
  7. Ground track ............ circular propagation (J2 ignored, stated),
                               pass prediction over Porter Ranch
  8. Regime classifier ....... stdlib kNN: go / marginal / no-go
  9. Advisory seats .......... Carbon 4 decider (tighten-only), Muse
                               advisor (never overrides a HOLD), Flow
                               plan-only (rendered=False, live=0)

Run:
  python3 main.py            full demo + CHECKS, prints VERDICT
  python3 main.py checks     CHECKS only
  python3 main.py hohmann    LEO->GEO transfer detail
  python3 main.py rendezvous docking corridor profile
  python3 main.py reentry    reentry profile detail
  python3 main.py passes     Porter Ranch pass prediction (24 h)
  python3 main.py holds      list the 14 HOLD gates
  python3 main.py hold-demo  ask for a live launch -> HOLD, exit 2
"""

import hashlib
import json
import math
import sys

# ---------------------------------------------------------------------------
# Constants (SI units throughout)
# ---------------------------------------------------------------------------
MU_EARTH = 3.986004418e14      # Earth gravitational parameter, m^3/s^2
R_EARTH = 6_371_000.0          # Earth mean radius, m
G0 = 9.80665                   # standard gravity, m/s^2
OMEGA_EARTH = 7.2921159e-5     # Earth rotation rate, rad/s
SCALE_HEIGHT = 7_200.0         # exponential atmosphere scale height, m
SG_K = 1.7415e-8               # Sutton-Graves constant (SI form)
PORTER_RANCH = (34.28, -118.55)  # lat, lon deg — ground station

# ---------------------------------------------------------------------------
# 1. Circular orbit + vis-viva
# ---------------------------------------------------------------------------
def orbit_radius_m(alt_m):
    return R_EARTH + alt_m


def circular_velocity_mps(alt_m):
    """v = sqrt(mu / r) for a circular orbit."""
    return math.sqrt(MU_EARTH / orbit_radius_m(alt_m))


def orbital_period_s(alt_m):
    """Kepler: T = 2*pi*sqrt(r^3 / mu)."""
    r = orbit_radius_m(alt_m)
    return 2.0 * math.pi * math.sqrt(r ** 3 / MU_EARTH)


def vis_viva_mps(r_m, a_m):
    """Vis-viva: v = sqrt(mu * (2/r - 1/a))."""
    return math.sqrt(MU_EARTH * (2.0 / r_m - 1.0 / a_m))


# ---------------------------------------------------------------------------
# 2. Hohmann transfer
# ---------------------------------------------------------------------------
def hohmann(alt1_m, alt2_m):
    """Hohmann transfer between two circular orbits (coplanar)."""
    r1, r2 = orbit_radius_m(alt1_m), orbit_radius_m(alt2_m)
    a_t = 0.5 * (r1 + r2)
    v1 = circular_velocity_mps(alt1_m)
    v2 = circular_velocity_mps(alt2_m)
    vp = vis_viva_mps(r1, a_t)
    va = vis_viva_mps(r2, a_t)
    return {
        "r1_m": r1, "r2_m": r2, "a_transfer_m": a_t,
        "dv1_mps": abs(vp - v1),
        "dv2_mps": abs(v2 - va),
        "total_dv_mps": abs(vp - v1) + abs(v2 - va),
        "transfer_time_s": math.pi * math.sqrt(a_t ** 3 / MU_EARTH),
    }


# ---------------------------------------------------------------------------
# 3. Delta-v budget table — STANDARD REFERENCE VALUES (labelled as such;
#    these are textbook/industry reference figures, not computed here)
# ---------------------------------------------------------------------------
DELTA_V_BUDGET = [
    # name, delta-v m/s, note
    ("Launch surface -> LEO (incl. gravity + drag losses)", 9400.0, "reference"),
    ("LEO station-keeping (per year)", 50.0, "reference"),
    ("LEO -> GEO (Hohmann, computed in this lab)", None, "computed"),
    ("GEO station-keeping (per year)", 50.0, "reference"),
    ("Deorbit burn from LEO (disposal)", 100.0, "reference"),
    ("Rendezvous + docking reserve", 30.0, "reference"),
]


def delta_v_budget():
    rows = []
    h = hohmann(400_000.0, 35_786_000.0)
    for name, dv, kind in DELTA_V_BUDGET:
        rows.append({"name": name,
                     "dv_mps": h["total_dv_mps"] if dv is None else dv,
                     "kind": kind})
    return rows


# ---------------------------------------------------------------------------
# 4. Rendezvous — phasing + docking-corridor governor
#    Approach speed caps shrink as range closes (X-family governor style).
# ---------------------------------------------------------------------------
CORRIDOR_ZONES = [
    # zone, range must exceed this floor (m), speed cap m/s
    ("FAR_FIELD", 1_000.0, 5.0),
    ("APPROACH", 100.0, 1.0),
    ("CLOSE", 10.0, 0.30),
    ("FINAL", 2.0, 0.10),
    ("CAPTURE", 0.0, 0.05),
]


def corridor_zone(range_m):
    """Zone for a given range: widest zone whose floor the range exceeds."""
    for name, floor, cap in CORRIDOR_ZONES:
        if range_m > floor:
            return {"zone": name, "speed_cap_mps": cap}
    return {"zone": "CAPTURE", "speed_cap_mps": 0.05}


def governor_clamp(range_m, requested_mps):
    z = corridor_zone(range_m)
    allowed = min(requested_mps, z["speed_cap_mps"])
    return {"zone": z["zone"], "range_m": range_m,
            "requested_mps": requested_mps, "speed_mps": allowed,
            "clamped": allowed < requested_mps}


def phasing_dv_mps(alt_m, phase_deg_per_orbit):
    """Rough phasing cost: change period slightly, drift, change back.
    Modelled as 2 burns of dv = v * (dT/T) / 3 (small-change approx)."""
    v = circular_velocity_mps(alt_m)
    frac = abs(phase_deg_per_orbit) / 360.0
    return 2.0 * v * frac / 3.0


def approach_profile():
    """Deterministic approach: range steps with governor-clamped speeds."""
    steps = []
    ranges = [5000.0, 2000.0, 800.0, 300.0, 80.0, 25.0, 6.0, 1.5]
    for r in ranges:
        g = governor_clamp(r, 5.0)
        steps.append({"range_m": r, "zone": g["zone"],
                      "speed_mps": g["speed_mps"]})
    return steps


# ---------------------------------------------------------------------------
# 5. Launch / departure interlocks (X-family style) — sim craft is an
#    UNCREWED cargo profile; crew-systems interlock = cabin safe/stowed.
# ---------------------------------------------------------------------------
NOMINAL_STATE = {
    "propellant_frac": 1.00,      # tanks full
    "comms": True,                # telemetry link up
    "tracking": True,             # ground tracking locked
    "weather_ok": True,           # launch weather within limits
    "range_clear": True,          # range green, no incursions
    "abort_armed": True,          # abort system armed (never disabled)
    "thermal_ok": True,           # TPS inspected, no damage flags
    "crew_systems_safe": True,    # uncrewed cargo: cabin stowed + safed
}


def launch_interlocks(state):
    checks = [
        ("propellant", state["propellant_frac"] >= 0.90,
         f"propellant {state['propellant_frac']:.0%} >= 90%"),
        ("comms", state["comms"], "telemetry link up"),
        ("tracking", state["tracking"], "ground tracking locked"),
        ("weather", state["weather_ok"], "launch weather within limits"),
        ("range_clear", state["range_clear"], "range green"),
        ("abort_armed", state["abort_armed"], "abort system armed"),
        ("thermal", state["thermal_ok"], "TPS inspection clean"),
        ("crew_systems", state["crew_systems_safe"],
         "uncrewed cargo profile: cabin stowed + safed"),
    ]
    out = [{"name": n, "ok": bool(ok), "detail": d} for n, ok, d in checks]
    return {"checks": out, "all_pass": all(c["ok"] for c in out)}


# ---------------------------------------------------------------------------
# 6. Reentry — Allen-Eggers peak deceleration + Sutton-Graves heating
# ---------------------------------------------------------------------------
def peak_g_load(entry_speed_mps, flight_path_deg):
    """Allen-Eggers (ballistic, exponential atmosphere):
    a_peak = v^2 * sin|gamma| / (2 * e * H). Returned in g."""
    gamma = math.radians(abs(flight_path_deg))
    a = entry_speed_mps ** 2 * math.sin(gamma) / (2.0 * math.e * SCALE_HEIGHT)
    return a / G0


def peak_heating_w_m2(entry_speed_mps, nose_radius_m, rho_peak=8.0e-5):
    """Sutton-Graves: q = k * sqrt(rho / Rn) * v^3 (stagnation point)."""
    return SG_K * math.sqrt(rho_peak / nose_radius_m) * entry_speed_mps ** 3


def reentry_profile():
    v, gamma = 7_800.0, -1.5     # shallow capsule entry from LEO
    return {"entry_speed_mps": v, "flight_path_deg": gamma,
            "peak_g": peak_g_load(v, gamma),
            "peak_heating_w_m2": peak_heating_w_m2(v, 2.0),
            "entry_interface_alt_km": 122.0,
            "landing_ellipse_km": (22.0, 6.0)}


# ---------------------------------------------------------------------------
# 7. Ground track + pass prediction — circular orbit, spherical Earth,
#    J2 perturbation IGNORED (stated simplification), Earth rotates.
# ---------------------------------------------------------------------------
def sat_lat_lon(alt_m, incl_deg, u_rad, lon_node_deg, t_s):
    """Sub-satellite point for a circular orbit at argument u."""
    i = math.radians(incl_deg)
    lat = math.asin(math.sin(i) * math.sin(u_rad))
    lon = math.radians(lon_node_deg) + math.atan2(
        math.cos(i) * math.sin(u_rad), math.cos(u_rad)) - OMEGA_EARTH * t_s
    return math.degrees(lat), (math.degrees(lon) + 540.0) % 360.0 - 180.0


def _ecef(lat_deg, lon_deg, alt_m):
    la, lo = math.radians(lat_deg), math.radians(lon_deg)
    r = R_EARTH + alt_m
    return (r * math.cos(la) * math.cos(lo),
            r * math.cos(la) * math.sin(lo), r * math.sin(la))


def elevation_deg(station, sat_lat, sat_lon, alt_m):
    sx, sy, sz = _ecef(sat_lat, sat_lon, alt_m)
    gx, gy, gz = _ecef(station[0], station[1], 0.0)
    rx, ry, rz = sx - gx, sy - gy, sz - gz
    rng = math.sqrt(rx * rx + ry * ry + rz * rz)
    la, lo = math.radians(station[0]), math.radians(station[1])
    up = (math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo),
          math.sin(la))
    sin_el = (rx * up[0] + ry * up[1] + rz * up[2]) / rng
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_el))))


def predict_passes(station=PORTER_RANCH, alt_m=400_000.0, incl_deg=51.6,
                   hours=24.0, step_s=30.0, min_el_deg=10.0):
    """Scan a day of propagation; return windows above min elevation."""
    period = orbital_period_s(alt_m)
    passes, cur = [], None
    steps = int(hours * 3600.0 / step_s)
    for k in range(steps + 1):
        t = k * step_s
        u = 2.0 * math.pi * t / period
        lat, lon = sat_lat_lon(alt_m, incl_deg, u, 0.0, t)
        el = elevation_deg(station, lat, lon, alt_m)
        if el >= min_el_deg:
            if cur is None:
                cur = {"start_s": t, "max_el_deg": el}
            cur["max_el_deg"] = max(cur["max_el_deg"], el)
            cur["end_s"] = t
        elif cur is not None:
            passes.append(cur)
            cur = None
    if cur is not None:
        passes.append(cur)
    return passes


# ---------------------------------------------------------------------------
# 8. kNN regime classifier — go / marginal / no-go
#    Features: (upper wind km/h, propellant frac, comms margin dB)
# ---------------------------------------------------------------------------
KNN_TRAINING = [
    ((5.0, 1.00, 12.0), "go"), ((10.0, 0.98, 10.0), "go"),
    ((15.0, 0.95, 9.0), "go"), ((8.0, 1.00, 14.0), "go"),
    ((25.0, 0.93, 7.0), "marginal"), ((30.0, 0.92, 6.0), "marginal"),
    ((22.0, 0.95, 5.0), "marginal"), ((28.0, 0.90, 8.0), "marginal"),
    ((45.0, 0.88, 3.0), "no-go"), ((55.0, 0.85, 2.0), "no-go"),
    ((40.0, 0.80, 4.0), "no-go"), ((60.0, 0.95, 1.0), "no-go"),
]
KNN_SCALE = (60.0, 0.25, 14.0)


def _dist(a, b):
    return math.sqrt(sum(((x - y) / s) ** 2
                         for x, y, s in zip(a, b, KNN_SCALE)))


def knn_classify(wind_kmh, propellant_frac, comms_db, k=3):
    feats = (wind_kmh, propellant_frac, comms_db)
    ranked = sorted(KNN_TRAINING, key=lambda p: _dist(feats, p[0]))
    votes = {}
    for _, label in ranked[:k]:
        votes[label] = votes.get(label, 0) + 1
    return max(sorted(votes), key=lambda lab: votes[lab])


def knn_training_accuracy():
    right = sum(1 for feats, label in KNN_TRAINING
                if knn_classify(*feats) == label)
    return right / len(KNN_TRAINING)


# ---------------------------------------------------------------------------
# 9. Advisory seats
# ---------------------------------------------------------------------------
CARBON4 = {"seat": "Gemini Carbon 4", "role": "DECIDER",
           "key_names_only": ["GEMINI_API_KEY", "CARBON4_API_KEY"],
           "tighten_only": True, "live": 0}
MUSE_ADVISOR = {"seat": "Muse Spark", "role": "advisor",
                "can_override_hold": False}
FLOW_PLAN = {"seat": "Google Flow", "rendered": False, "live": 0,
             "shots": ["Earth-limb dawn rise", "Hohmann arc diagram",
                       "Docking corridor approach", "Reentry plasma trail",
                       "Porter Ranch pass map"]}


def carbon4_decide(context):
    phases = ["MAP", "MODEL", "STRESS", "DECIDE"]
    verdict = "PROCEED"
    if context.get("live_action") or not context.get("interlocks_pass", False):
        verdict = "HOLD"
    trace_src = json.dumps({"phases": phases, "context": context,
                            "verdict": verdict}, sort_keys=True)
    return {"seat": CARBON4["seat"], "phases": phases, "verdict": verdict,
            "tighten_only": CARBON4["tighten_only"],
            "trace_sha256": hashlib.sha256(trace_src.encode()).hexdigest()}


def flow_plan():
    return dict(FLOW_PLAN)


# ---------------------------------------------------------------------------
# 10. HOLD gates — 14, permanent
# ---------------------------------------------------------------------------
HOLD_GATES = {
    "live_launch": "No real launch is ever commanded.",
    "real_spacecraft_control": "No real spacecraft is flown or commanded.",
    "crewed_flight_live": "No crewed flight is planned for real or flown.",
    "docking_live": "No real rendezvous or docking is attempted.",
    "bypass_interlock": "Launch interlocks cannot be bypassed.",
    "disable_abort_system": "The abort system is never disabled.",
    "reentry_live": "No real reentry is flown or guided.",
    "claim_certified": "Never claim this sim is certified flight hardware.",
    "commercial_service_claim": "No launch services are sold or offered.",
    "upload_flight_plan_live": "No flight plan is filed with any authority.",
    "real_range_operations": "No real range, pad, or tracking asset is used.",
    "orbital_debris_risk_live": "No real orbit is populated with objects.",
    "weapons_or_military_use": "No weapons or military mission planning.",
    "remove_human_oversight": "Human oversight of real ops is never removed.",
}


def hold_demo(action="live_launch"):
    gate = HOLD_GATES.get(action, "unknown live action")
    print(f"HOLD: {action} — {gate}")
    print("VERDICT: HOLD (exit 2)")
    return 2


# ---------------------------------------------------------------------------
# CHECKS — every number asserted against an analytic value
# ---------------------------------------------------------------------------
def run_checks():
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    # --- circular orbit / Kepler ---
    v_leo = circular_velocity_mps(400_000.0)
    add("leo_velocity", abs(v_leo - math.sqrt(MU_EARTH / 6_771_000.0)) < 1e-6,
        f"v(400 km) = {v_leo:.0f} m/s (~7.67 km/s)")
    t_leo = orbital_period_s(400_000.0)
    add("leo_period_90min", 5400.0 < t_leo < 5700.0,
        f"T(400 km) = {t_leo / 60:.1f} min (~92 min)")
    t_geo = orbital_period_s(35_786_000.0)
    add("geo_period_sidereal_day", abs(t_geo - 86164.0) < 120.0,
        f"T(GEO) = {t_geo:.0f} s vs sidereal day 86164 s")
    add("vis_viva_circular_matches",
        abs(vis_viva_mps(6_771_000.0, 6_771_000.0) - v_leo) < 1e-6,
        "vis-viva with a=r reproduces circular velocity")
    add("period_grows_with_altitude",
        orbital_period_s(800_000.0) > t_leo,
        "higher orbit, longer period (Kepler)")

    # --- Hohmann ---
    h = hohmann(400_000.0, 35_786_000.0)
    add("hohmann_total_dv", 3_800.0 < h["total_dv_mps"] < 4_100.0,
        f"LEO->GEO total dv = {h['total_dv_mps']:.0f} m/s (~3.9 km/s)")
    add("hohmann_dv_split",
        abs(h["dv1_mps"] + h["dv2_mps"] - h["total_dv_mps"]) < 1e-6,
        f"dv1 {h['dv1_mps']:.0f} + dv2 {h['dv2_mps']:.0f} = total")
    add("hohmann_transfer_time", 5.0 < h["transfer_time_s"] / 3600.0 < 5.6,
        f"transfer time {h['transfer_time_s'] / 3600:.2f} h (~5.3 h)")
    add("hohmann_same_orbit_zero",
        hohmann(400_000.0, 400_000.0)["total_dv_mps"] < 1e-6,
        "same orbit -> zero delta-v")
    rows = delta_v_budget()
    add("budget_uses_computed_hohmann",
        abs([r for r in rows if r["kind"] == "computed"][0]["dv_mps"]
            - h["total_dv_mps"]) < 1e-6,
        "budget table carries the computed Hohmann figure")

    # --- rendezvous / governor ---
    add("corridor_far_field", corridor_zone(5000.0)["speed_cap_mps"] == 5.0,
        "5,000 m -> FAR_FIELD cap 5.0 m/s")
    add("corridor_capture", corridor_zone(1.0)["speed_cap_mps"] == 0.05,
        "1 m -> CAPTURE cap 0.05 m/s")
    gc = governor_clamp(50.0, 2.0)
    add("governor_clamps_close_range",
        gc["speed_mps"] == 0.30 and gc["clamped"],
        "50 m request 2.0 m/s clamped to CLOSE cap 0.30 m/s")
    prof = approach_profile()
    speeds = [s["speed_mps"] for s in prof]
    add("approach_speeds_monotonic_down",
        all(a >= b for a, b in zip(speeds, speeds[1:])),
        "approach speeds never increase as range closes")
    add("phasing_dv_positive_and_small",
        0.0 < phasing_dv_mps(400_000.0, 10.0) < 200.0,
        f"10 deg/orbit phasing ~ {phasing_dv_mps(400_000.0, 10.0):.0f} m/s")

    # --- interlocks ---
    add("interlocks_nominal_pass", launch_interlocks(NOMINAL_STATE)["all_pass"],
        "nominal state: all 8 interlocks pass")
    add("interlocks_fail_low_propellant",
        not launch_interlocks(dict(NOMINAL_STATE, propellant_frac=0.5))["all_pass"],
        "propellant 50% trips the propellant interlock")
    add("interlocks_fail_abort_unarmed",
        not launch_interlocks(dict(NOMINAL_STATE, abort_armed=False))["all_pass"],
        "abort unarmed trips the abort interlock")
    add("interlocks_fail_weather",
        not launch_interlocks(dict(NOMINAL_STATE, weather_ok=False))["all_pass"],
        "bad weather trips the weather interlock")

    # --- reentry ---
    rp = reentry_profile()
    add("reentry_peak_g_capsule_range", 2.0 < rp["peak_g"] < 6.0,
        f"shallow LEO entry peak {rp['peak_g']:.1f} g (capsule range)")
    add("reentry_steeper_is_harsher",
        peak_g_load(7_800.0, -6.0) > peak_g_load(7_800.0, -1.5),
        "steeper flight path -> higher peak g (Allen-Eggers)")
    add("heating_scales_v_cubed",
        abs(peak_heating_w_m2(15_600.0, 2.0) / peak_heating_w_m2(7_800.0, 2.0)
            - 8.0) < 1e-9,
        "doubling entry speed multiplies heating by 8 (v^3)")

    # --- ground track / passes ---
    lat0, lon0 = sat_lat_lon(400_000.0, 51.6, 0.0, 0.0, 0.0)
    add("ground_track_starts_on_node", abs(lat0) < 1e-9 and abs(lon0) < 1e-9,
        "u=0 starts at lat 0, lon 0 (ascending node)")
    lat_max, _ = sat_lat_lon(400_000.0, 51.6, math.pi / 2, 0.0, 0.0)
    add("ground_track_max_lat_is_inclination", abs(lat_max - 51.6) < 0.5,
        f"max latitude {lat_max:.1f} deg ~ inclination 51.6 deg")
    passes = predict_passes()
    add("passes_found_over_porter_ranch", len(passes) >= 1,
        f"{len(passes)} passes >= 10 deg in 24 h over Porter Ranch")
    add("passes_have_real_elevation",
        all(10.0 <= p["max_el_deg"] <= 90.0 for p in passes),
        "every pass max elevation within [10, 90] deg")

    # --- classifier ---
    add("knn_go_case", knn_classify(8.0, 1.0, 12.0) == "go",
        "calm / full / strong link -> go")
    add("knn_nogo_case", knn_classify(55.0, 0.85, 2.0) == "no-go",
        "storm / low / weak link -> no-go")
    acc = knn_training_accuracy()
    add("knn_training_accuracy", acc >= 0.90,
        f"kNN self-accuracy {acc:.3f} on its training set")

    # --- seats ---
    c_ok = carbon4_decide({"live_action": False, "interlocks_pass": True})
    c_hold = carbon4_decide({"live_action": True, "interlocks_pass": True})
    add("carbon4_trace_deterministic",
        len(c_ok["trace_sha256"]) == 64
        and carbon4_decide({"live_action": False, "interlocks_pass": True})
        ["trace_sha256"] == c_ok["trace_sha256"],
        "sha256 trace is 64 hex chars and repeatable")
    add("carbon4_tightens_to_hold",
        c_ok["verdict"] == "PROCEED" and c_hold["verdict"] == "HOLD",
        "sim PROCEED, live action HOLD")
    add("muse_never_overrides", MUSE_ADVISOR["can_override_hold"] is False,
        "Muse advisor can_override_hold=False")
    fp = flow_plan()
    add("flow_plan_only", fp["rendered"] is False and fp["live"] == 0,
        "Flow rendered=False, live=0")

    # --- HOLD gates ---
    add("hold_gates_count", len(HOLD_GATES) == 14,
        f"{len(HOLD_GATES)} HOLD gates defined")
    add("hold_gates_cover_launch_and_abort",
        "live_launch" in HOLD_GATES and "disable_abort_system" in HOLD_GATES,
        "live launch + abort-disable both held")

    passed = sum(1 for c in checks if c["ok"])
    return checks, passed


def print_checks():
    checks, passed = run_checks()
    print("-" * 68)
    print("CHECKS")
    print("-" * 68)
    for c in checks:
        print(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}")
    print(f"\nCHECKS {passed}/{len(checks)}")
    return checks, passed


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------
def demo():
    print("=" * 68)
    print("OrbitX — Orbital Transport Autopilot Lab (SIM ONLY)")
    print("by Darion Kellar")
    print("X family complete: Flightx air / YachtX water / RVx road /")
    print("HoverX low altitude / TeleportX teleport / OrbitX space")
    print("=" * 68)

    print("\n[1] Circular orbit (Kepler):")
    for alt_km in (400.0, 800.0, 35_786.0):
        alt = alt_km * 1000.0
        print(f"    {alt_km:>9,.0f} km: v = {circular_velocity_mps(alt):,.0f} m/s, "
              f"T = {orbital_period_s(alt) / 60:.1f} min")

    h = hohmann(400_000.0, 35_786_000.0)
    print(f"\n[2] Hohmann LEO(400 km) -> GEO: dv1 {h['dv1_mps']:.0f} + "
          f"dv2 {h['dv2_mps']:.0f} = {h['total_dv_mps']:.0f} m/s, "
          f"transfer {h['transfer_time_s'] / 3600:.2f} h")

    print("\n[3] Delta-v budget (reference values labelled):")
    for r in delta_v_budget():
        print(f"    {r['name']:<52} {r['dv_mps']:>8,.0f} m/s  [{r['kind']}]")

    print("\n[4] Rendezvous approach (docking-corridor governor):")
    for s in approach_profile():
        print(f"    range {s['range_m']:>7,.0f} m  {s['zone']:<10} "
              f"speed {s['speed_mps']:.2f} m/s")

    il = launch_interlocks(NOMINAL_STATE)
    print(f"\n[5] Launch interlocks: all_pass={il['all_pass']}")
    for c in il["checks"]:
        print(f"    [{'PASS' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}")

    rp = reentry_profile()
    print(f"\n[6] Reentry from LEO: entry {rp['entry_speed_mps']:,} m/s at "
          f"{rp['flight_path_deg']} deg -> peak {rp['peak_g']:.1f} g, "
          f"peak heating {rp['peak_heating_w_m2'] / 1e4:.1f} W/cm^2, "
          f"ellipse {rp['landing_ellipse_km'][0]:.0f} x "
          f"{rp['landing_ellipse_km'][1]:.0f} km")

    passes = predict_passes()
    print(f"\n[7] Ground track (circular, J2 ignored): {len(passes)} passes "
          f">= 10 deg over Porter Ranch in 24 h")
    for p in passes[:3]:
        print(f"    pass at t+{p['start_s'] / 3600:.2f} h, "
              f"max elevation {p['max_el_deg']:.0f} deg")

    print(f"\n[8] Regime classifier: calm/full/strong = "
          f"{knn_classify(8.0, 1.0, 12.0)}, storm/low/weak = "
          f"{knn_classify(55.0, 0.85, 2.0)}")

    dec = carbon4_decide({"live_action": False, "interlocks_pass": True})
    print(f"\n[9] Carbon 4 decider: {dec['verdict']} "
          f"(trace {dec['trace_sha256'][:16]}...)")
    print(f"    Muse advisor: can_override_hold={MUSE_ADVISOR['can_override_hold']}")
    fp = flow_plan()
    print(f"    Flow: rendered={fp['rendered']}, live={fp['live']}, "
          f"{len(fp['shots'])} shots planned")

    print(f"\n[10] HOLD gates: {len(HOLD_GATES)} — live launch, real "
          "spacecraft control, docking, reentry, abort-disable... all HOLD.")

    checks, passed = print_checks()
    verdict = "PROCEED" if passed == len(checks) else "HOLD"
    print(f"VERDICT {verdict} (sandbox simulation only) | HOLD gates {len(HOLD_GATES)}")
    return 0 if verdict == "PROCEED" else 2


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "demo"
    if cmd == "hold-demo":
        return hold_demo(argv[2] if len(argv) > 2 else "live_launch")
    if cmd == "checks":
        _, passed = print_checks()
        checks, _ = run_checks()
        print(f"VERDICT {'PROCEED' if passed == len(checks) else 'HOLD'} "
              f"(sandbox simulation only) | HOLD gates {len(HOLD_GATES)}")
        return 0 if passed == len(checks) else 2
    if cmd == "hohmann":
        h = hohmann(400_000.0, 35_786_000.0)
        for k, v in h.items():
            print(f"{k}: {v:,.2f}")
        return 0
    if cmd == "rendezvous":
        for s in approach_profile():
            print(f"range {s['range_m']:>8,.0f} m  {s['zone']:<10} "
                  f"{s['speed_mps']:.2f} m/s")
        return 0
    if cmd == "reentry":
        rp = reentry_profile()
        for k, v in rp.items():
            print(f"{k}: {v}")
        return 0
    if cmd == "passes":
        for p in predict_passes():
            print(f"t+{p['start_s'] / 3600:5.2f} h -> "
                  f"t+{p['end_s'] / 3600:5.2f} h  max el {p['max_el_deg']:.0f} deg")
        return 0
    if cmd == "holds":
        for name, desc in HOLD_GATES.items():
            print(f"HOLD {name}: {desc}")
        return 0
    return demo()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
