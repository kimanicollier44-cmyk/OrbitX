"""OrbitX tests — every assertion mirrors a CHECK in main.py."""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as ox


def test_leo_velocity():
    assert abs(ox.circular_velocity_mps(400_000.0) - 7673.0) < 5.0


def test_leo_period_about_92_min():
    assert 5400.0 < ox.orbital_period_s(400_000.0) < 5700.0


def test_geo_period_is_sidereal_day():
    assert abs(ox.orbital_period_s(35_786_000.0) - 86164.0) < 120.0


def test_vis_viva_circular():
    r = ox.orbit_radius_m(400_000.0)
    assert abs(ox.vis_viva_mps(r, r) - ox.circular_velocity_mps(400_000.0)) < 1e-6


def test_hohmann_leo_geo():
    h = ox.hohmann(400_000.0, 35_786_000.0)
    assert 3800.0 < h["total_dv_mps"] < 4100.0
    assert abs(h["dv1_mps"] + h["dv2_mps"] - h["total_dv_mps"]) < 1e-6
    assert 5.0 < h["transfer_time_s"] / 3600.0 < 5.6


def test_hohmann_same_orbit_zero():
    assert ox.hohmann(400_000.0, 400_000.0)["total_dv_mps"] < 1e-6


def test_budget_has_computed_row():
    rows = ox.delta_v_budget()
    comp = [r for r in rows if r["kind"] == "computed"]
    assert len(comp) == 1
    assert abs(comp[0]["dv_mps"] - ox.hohmann(400_000.0, 35_786_000.0)
               ["total_dv_mps"]) < 1e-6


def test_corridor_caps():
    assert ox.corridor_zone(5000.0)["speed_cap_mps"] == 5.0
    assert ox.corridor_zone(500.0)["speed_cap_mps"] == 1.0
    assert ox.corridor_zone(50.0)["speed_cap_mps"] == 0.30
    assert ox.corridor_zone(5.0)["speed_cap_mps"] == 0.10
    assert ox.corridor_zone(1.0)["speed_cap_mps"] == 0.05


def test_governor_clamps():
    g = ox.governor_clamp(50.0, 2.0)
    assert g["speed_mps"] == 0.30 and g["clamped"]


def test_approach_speeds_monotonic():
    speeds = [s["speed_mps"] for s in ox.approach_profile()]
    assert all(a >= b for a, b in zip(speeds, speeds[1:]))


def test_phasing_small():
    assert 0.0 < ox.phasing_dv_mps(400_000.0, 10.0) < 200.0


def test_interlocks():
    assert ox.launch_interlocks(ox.NOMINAL_STATE)["all_pass"]
    assert not ox.launch_interlocks(
        dict(ox.NOMINAL_STATE, propellant_frac=0.5))["all_pass"]
    assert not ox.launch_interlocks(
        dict(ox.NOMINAL_STATE, abort_armed=False))["all_pass"]
    assert not ox.launch_interlocks(
        dict(ox.NOMINAL_STATE, weather_ok=False))["all_pass"]
    assert not ox.launch_interlocks(
        dict(ox.NOMINAL_STATE, comms=False))["all_pass"]


def test_reentry_g_range():
    assert 2.0 < ox.peak_g_load(7800.0, -1.5) < 6.0


def test_reentry_steeper_harsher():
    assert ox.peak_g_load(7800.0, -6.0) > ox.peak_g_load(7800.0, -1.5)


def test_heating_v_cubed():
    ratio = (ox.peak_heating_w_m2(15600.0, 2.0)
             / ox.peak_heating_w_m2(7800.0, 2.0))
    assert abs(ratio - 8.0) < 1e-9


def test_ground_track_max_lat():
    lat, _ = ox.sat_lat_lon(400_000.0, 51.6, math.pi / 2, 0.0, 0.0)
    assert abs(lat - 51.6) < 0.5


def test_passes_over_porter_ranch():
    passes = ox.predict_passes()
    assert len(passes) >= 1
    assert all(10.0 <= p["max_el_deg"] <= 90.0 for p in passes)


def test_knn():
    assert ox.knn_classify(8.0, 1.0, 12.0) == "go"
    assert ox.knn_classify(55.0, 0.85, 2.0) == "no-go"
    assert ox.knn_training_accuracy() >= 0.90


def test_carbon4():
    ok = ox.carbon4_decide({"live_action": False, "interlocks_pass": True})
    hold = ox.carbon4_decide({"live_action": True, "interlocks_pass": True})
    assert ok["verdict"] == "PROCEED" and hold["verdict"] == "HOLD"
    assert len(ok["trace_sha256"]) == 64
    assert ox.carbon4_decide({"live_action": False, "interlocks_pass": True}
                             )["trace_sha256"] == ok["trace_sha256"]


def test_seats():
    assert ox.MUSE_ADVISOR["can_override_hold"] is False
    fp = ox.flow_plan()
    assert fp["rendered"] is False and fp["live"] == 0


def test_hold_gates():
    assert len(ox.HOLD_GATES) == 14
    for key in ("live_launch", "disable_abort_system", "docking_live",
                "reentry_live", "upload_flight_plan_live"):
        assert key in ox.HOLD_GATES


def test_hold_demo_exit_code(capsys):
    assert ox.hold_demo("live_launch") == 2
    assert "HOLD" in capsys.readouterr().out


def test_checks_all_pass():
    checks, passed = ox.run_checks()
    assert passed == len(checks)
