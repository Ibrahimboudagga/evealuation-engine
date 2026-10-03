"""Generate a judge-calibration report from JSONL without calling a provider."""

import argparse
import json
from pathlib import Path

from app.scenarios.calibration import CalibrationCase, calibrate


def main():
    parser = argparse.ArgumentParser(description="Measure judge agreement and repeatability")
    parser.add_argument("dataset", help="JSONL with id, human_label, judge_labels, and optional group")
    parser.add_argument("--output", default="calibration-report.json")
    args = parser.parse_args()
    cases = [CalibrationCase.model_validate_json(line) for line in
             Path(args.dataset).read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    report = calibrate(cases)
    Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
