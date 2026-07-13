#!/usr/bin/env python3
"""F-K-2 CI gate: per-package Python coverage ratchet.

Usage: check_coverage_ratchet.py [coverage.json] [coverage-ratchet.json]
       (defaults: ./coverage.json and ./coverage-ratchet.json)

Reads pytest-cov's JSON report (--cov-report=json:coverage.json) and the
checked-in coverage-ratchet.json (repo root), aggregates covered/total
statements per top-level package directory, and:

  * FAILS (exit 1) if any package — or the overall total — is below its floor;
  * prints a non-fatal suggestion to raise a floor whenever measured coverage
    exceeds it by more than SLACK points.

The floors are a monotonic ratchet: raise them as coverage grows; never lower
them to make a PR pass. Floor values and the platform-skew rationale are
documented in coverage-ratchet.json itself.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import PurePosixPath

# Floors are set to floor(measured) - 2 (platform-skew margin: CI runs on
# Linux and skips macOS/Windows-only branches), so a package naturally sits
# ~2-3 points above its floor. Only nag to raise once it clears that margin.
SLACK = 3.0


def main() -> int:
    cov_path = sys.argv[1] if len(sys.argv) > 1 else "coverage.json"
    ratchet_path = sys.argv[2] if len(sys.argv) > 2 else "coverage-ratchet.json"

    with open(cov_path, encoding="utf-8") as f:
        cov = json.load(f)
    with open(ratchet_path, encoding="utf-8") as f:
        ratchet = json.load(f)

    floors: dict[str, float] = ratchet["packages"]
    total_floor: float = ratchet["total"]

    # Aggregate statement counts per top-level package directory.
    stmts: dict[str, int] = defaultdict(int)
    covered: dict[str, int] = defaultdict(int)
    for filename, data in cov["files"].items():
        pkg = PurePosixPath(filename.replace("\\", "/")).parts[0]
        stmts[pkg] += data["summary"]["num_statements"]
        covered[pkg] += data["summary"]["covered_lines"]

    failures: list[str] = []
    suggestions: list[str] = []
    for pkg, floor in sorted(floors.items()):
        if stmts.get(pkg, 0) == 0:
            failures.append(
                f"  {pkg}: no coverage data — was it dropped from --cov targets?"
            )
            continue
        pct = 100.0 * covered[pkg] / stmts[pkg]
        line = f"  {pkg}: {pct:.2f}% (floor {floor}%)"
        if pct < floor:
            failures.append(line + "  <-- BELOW FLOOR")
        else:
            print(line)
            if pct > floor + SLACK:
                suggestions.append(
                    f"  {pkg}: measured {pct:.2f}% — consider raising its floor "
                    f"to {int(pct) - 1} in coverage-ratchet.json"
                )

    total_pct = cov["totals"]["percent_covered"]
    if total_pct < total_floor:
        failures.append(
            f"  TOTAL: {total_pct:.2f}% (floor {total_floor}%)  <-- BELOW FLOOR"
        )
    else:
        print(f"  TOTAL: {total_pct:.2f}% (floor {total_floor}%)")
        if total_pct > total_floor + SLACK:
            suggestions.append(
                f"  TOTAL: measured {total_pct:.2f}% — consider raising the total "
                f"floor to {int(total_pct) - 1} in coverage-ratchet.json"
            )

    if failures:
        print("\nCoverage ratchet FAILED — coverage dropped below a checked-in floor:")
        print("\n".join(failures))
        print("\nAdd tests for the code you touched (do NOT lower the floor;")
        print("it is a monotonic ratchet — see coverage-ratchet.json).")
        return 1

    if suggestions:
        print("\nCoverage grew past its floor (non-fatal). Lock in the gains:")
        print("\n".join(suggestions))

    print("\nCoverage ratchet OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
