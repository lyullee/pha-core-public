"""Synthetic thermodynamically consistent packet for interface regression only.

This generator is separate from the adapter: normal integration always consumes
provider-owned histories. The 0.37 s duration is an explicit diagnostic pulse,
not a predicted accident duration or a field-calibrated equipment simulation.
"""
import argparse
import json
import math
from pathlib import Path
import sys

from pha_core.domain import digest

parser = argparse.ArgumentParser()
parser.add_argument("--prism-root", required=True, type=Path)
args = parser.parse_args()
root = args.prism_root
if not (root / "src/lh2_tno").is_dir() and (root / "lh2-tno/src/lh2_tno").is_dir():
    root /= "lh2-tno"
from CoolProp.CoolProp import PropsSI
temperature = 150.
pressure = 101325.
density = PropsSI("Dmass", "T", temperature, "P", pressure, "Hydrogen")
enthalpy = PropsSI("Hmass", "T", temperature, "P", pressure, "Hydrogen")
opening = .1
area = math.pi * opening**2 / 4
steps = [{"time_s": t, "mass_flow_kg_s": density * area * velocity,
          "pressure_pa_abs": pressure, "specific_enthalpy_j_kg": enthalpy, "temperature_k": temperature,
          "density_kg_m3": density, "effective_area_m2": area, "velocity_m_s": velocity,
          "area_provenance": "synthetic diagnostic circular exit .1m; large pulse for nonzero harm regression", "velocity_origin": "provider_resolved"}
         for t, velocity in [(0., 30.), (.2, 40.)]]
mass = sum(step["mass_flow_kg_s"] * (end - step["time_s"]) for step, end in zip(steps, [.2, .37]))
packet = {"component_id": "storage", "port_id": "storage.out", "event_id": "SYNTHETIC-INTERFACE-PULSE",
          "event_kind": "accidental_leak", "upstream_service": "COLD_GH2",
          "upstream_service_provenance": "Synthetic cold hydrogen pulse; no LH2 literature rate used",
          "enthalpy_convention": "static_exit_enthalpy",
          "property_basis": {"fluid": "Hydrogen", "backend": "CoolProp-HEOS", "energy_reference": "CoolProp-HEOS:Hydrogen:native"},
          "provider_source_digest": digest({"T": temperature, "P": pressure, "area": area, "input": "synthetic diagnostic"}),
          "provider_result_digest": digest({"steps": steps, "mass": mass}),
          "provider_boundary_reference": "synthetic interface regression only; not calibrated equipment provider",
          "input_origin": "synthetic", "position_enu_m": [0., 0., 1.], "direction_enu": [1., 0., 0.],
          "ambient_pressure_pa_abs": pressure, "ambient_temperature_k": 288.15,
          "duration_s": .37, "available_mass_kg": 1., "cumulative_mass_out_kg": mass,
          "cumulative_static_enthalpy_out_j": mass * enthalpy, "steps": steps,
          "failure_opening_diameter_m": opening, "termination_provenance": "explicit synthetic .37s pulse; not predicted physical isolation"}
request = json.loads((root / "examples/consequence_request.json").read_text(encoding="utf-8"))
request.update(source_input=None, resolved_release=packet, component_id="storage", available_mass_kg=1.,
               source_position_enu_m=[0., 0., 1.], annual_frequency_per_year=None,
               annual_frequency_provenance=None, initiation_intensity_per_s=None,
               operating_mode="receiving", hazop_scenario_id="HAZOP-DEMO-01",
               release_termination={"basis": "synthetic_diagnostic_horizon", "time_s": .37, "provenance": "Synthetic interface pulse; field release duration unresolved"},
               evaluation_times_s=[0., .1, .2, .37, .5, 1., 2., 3., 4., 5., 10., 20.])
out = Path(__file__).parent
for name, data in (("prism_diagnostic_request.json", request),
                   ("prism_diagnostic_facility.json", json.loads((root / "examples/consequence_facility.json").read_text(encoding="utf-8")))):
    (out / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
handoff = {"scenario_id": "SC-TK01-OVERPRESSURE-LOC", "port_ids": ["storage.out"],
           "release": {"mode": "hole", "hole_diameter_m": opening}, "request": request,
           "facility": json.loads((out / "prism_diagnostic_facility.json").read_text(encoding="utf-8"))}
(out / "prism_diagnostic_handoff.json").write_text(json.dumps(handoff, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("Synthetic PRISM diagnostic files created; no field validation claim.")
