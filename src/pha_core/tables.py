"""Explicit unit conversions and reviewable table replacement, without guessed IDs."""
import csv
import json
import math
from pathlib import Path

from .domain import StudySnapshot


def convert(value: float, unit: str, *, ambient_pa_abs: float | None = None):
    if isinstance(value, bool) or not math.isfinite(value):
        raise ValueError("finite numeric input required")
    factors = {"Pa_abs": (1, "Pa_abs"), "bar_abs": (100000, "Pa_abs"), "kPa_abs": (1000, "Pa_abs"),
               "s": (1, "s"), "h": (3600, "s"), "K": (1, "K"), "m": (1, "m"),
               "mm": (.001, "m"), "m3": (1, "m3"), "kg": (1, "kg"), "fraction": (1, "1"), "percent": (.01, "1")}
    if unit in {"bar_g", "Pa_g"}:
        if ambient_pa_abs is None or ambient_pa_abs <= 0:
            raise ValueError("gauge pressure requires an explicit absolute ambient reference")
        converted, target = value * (100000 if unit == "bar_g" else 1) + ambient_pa_abs, "Pa_abs"
    elif unit == "degC":
        converted, target = value + 273.15, "K"
    elif unit in factors:
        factor, target = factors[unit]
        converted = value * factor
    else:
        raise ValueError("unsupported/ambiguous unit; explicit basis required")
    if target in {"K", "Pa_abs"} and converted <= 0:
        raise ValueError("absolute pressure/temperature must be positive")
    if target == "1" and not 0 <= converted <= 1:
        raise ValueError("fraction/percent out of range")
    return {"value": converted, "unit": target, "original_value": value, "original_unit": unit}


def _cell(value):
    if value == "":
        return None
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def export_table(snapshot, table, path):
    rows = snapshot.model_dump(mode="json").get(table)
    if not isinstance(rows, list):
        raise ValueError("unknown entity table")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.suffix.lower() == ".json":
        target.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    headers = list(rows[0]) if rows else ["id"]
    serialized = [[json.dumps(row.get(k), ensure_ascii=False) for k in headers] for row in rows]
    if target.suffix.lower() == ".xlsx":
        from openpyxl import Workbook
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = table
        sheet.append(headers)
        for row in serialized:
            sheet.append(row)
        workbook.save(target)
    elif target.suffix.lower() == ".csv":
        with target.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            writer.writerows(serialized)
    else:
        raise ValueError("table format must be JSON, CSV or XLSX")


def preview_table(snapshot: StudySnapshot, table: str, path, *, new_revision: int, reason: str):
    current = snapshot.model_dump(mode="json")
    if not isinstance(current.get(table), list):
        raise ValueError("unknown entity table")
    source = Path(path)
    if source.suffix.lower() == ".json":
        rows = json.loads(source.read_text(encoding="utf-8-sig"))
    elif source.suffix.lower() == ".csv":
        with source.open(encoding="utf-8-sig", newline="") as f:
            rows = [{key: _cell(value) for key, value in row.items()} for row in csv.DictReader(f)]
    elif source.suffix.lower() == ".xlsx":
        from openpyxl import load_workbook
        workbook = load_workbook(source, read_only=True, data_only=False)
        try:
            sheet = workbook[table] if table in workbook.sheetnames else workbook.active
            values = iter(sheet.values)
            headers = next(values)
            rows = []
            for cells in values:
                if any(isinstance(c, str) and c.startswith("=") for c in cells):
                    raise ValueError("table formulas must be replaced with reviewed values")
                if any(c is not None for c in cells):
                    rows.append({k: _cell(v) for k, v in zip(headers, cells) if k is not None})
        finally:
            workbook.close()
    else:
        raise ValueError("table format must be JSON, CSV or XLSX")
    if new_revision is None or new_revision <= snapshot.revision or not reason:
        raise ValueError("new baseline revision and change reason required")
    old = {r["id"]: r for r in current[table]}
    new = {r["id"]: r for r in rows}
    changed = [key for key in old.keys() & new.keys() if old[key] != new[key]]
    for key in changed:
        if "revision" in old[key] and new[key].get("revision", 1) <= old[key]["revision"]:
            raise ValueError(f"{key}: increment entity revision for changed input")
    current.update({table: rows, "revision": new_revision, "change_reason": reason})
    candidate = StudySnapshot.model_validate_json(json.dumps(current, ensure_ascii=False, allow_nan=False))
    return candidate, {"added": sorted(new.keys() - old.keys()), "removed": sorted(old.keys() - new.keys()),
                       "changed": sorted(changed), "candidate_digest": candidate.digest(), "committed": False}
