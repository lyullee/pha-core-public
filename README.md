# PHA Core — public source edition

Local Python software for shared equipment data, static fault tree analysis
(FTA), layer of protection analysis (LOPA), low-demand safety integrity level
(SIL) calculations, and optional PRISM consequence integration. Includes a Korean
browser workbench, SQLite storage, CLI, synthetic examples, and calculation tests.

## Install and run

Python 3.12 or later:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[test,tables]"
.venv\Scripts\python -m pha_core serve --db artifacts/workbench.sqlite --port 8765
```

Open http://127.0.0.1:8765. Load the synthetic example, validate, save, and run.
Stop the server with Ctrl+C. CLI example:

```powershell
python -m pha_core run examples/tank_overpressure.snapshot.json --output artifacts/result.json
python -m pytest -q
```

## Scope

FTA uses ROBDD with shared events, minimal cut sets and Birnbaum importance.
LOPA prevents duplicate barrier credit. SIL calculations cover restricted
low-demand approximations; a numerical band does not establish lifecycle
compliance or certification. Examples are synthetic, not facility records.

PRISM is optional and installed separately. The original adapter patch is in
`integrations/prism-extension.patch`; its use requires the appropriate upstream
version and rights. Integration tests skip when that separate checkout is absent.
No reference PDFs, internal reports, invention forms, field data, original Git
history or archived execution records are distributed here.

## Rights

This is a source-visible release, not a grant of an open-source license. See
LICENSE and THIRD_PARTY_NOTICES.md. Existing methods and external libraries
retain their respective rights. Research and engineering-review software;
not a certified safety controller or a complete facility QRA.
