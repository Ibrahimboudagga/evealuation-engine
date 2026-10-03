"""Deterministic agreement and repeatability analysis for human-labelled judge cases."""

from collections import Counter, defaultdict
from typing import Any

from pydantic import Field, model_validator

from app.scenarios.schemas import StrictModel


class CalibrationCase(StrictModel):
    id: str = Field(min_length=1)
    human_label: str = Field(min_length=1)
    judge_labels: list[str] = Field(min_length=1)
    group: str = "all"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def labels_are_nonempty(self):
        if any(not label.strip() for label in self.judge_labels):
            raise ValueError("Judge labels cannot be blank")
        return self


def _agreement(cases):
    return sum(case.judge_labels[0] == case.human_label for case in cases) / len(cases) if cases else None


def _kappa(cases):
    if not cases:
        return None
    observed = _agreement(cases)
    human = Counter(case.human_label for case in cases)
    judge = Counter(case.judge_labels[0] for case in cases)
    labels = set(human) | set(judge)
    expected = sum(human[label] / len(cases) * judge[label] / len(cases) for label in labels)
    return None if expected == 1 else (observed - expected) / (1 - expected)


def calibrate(cases: list[CalibrationCase]):
    if not cases or len({case.id for case in cases}) != len(cases):
        raise ValueError("Calibration data must contain unique case IDs")
    disagreements = [{"case_id": case.id, "human_label": case.human_label,
                      "judge_label": case.judge_labels[0], "group": case.group}
                     for case in cases if case.judge_labels[0] != case.human_label]
    repeated = [case for case in cases if len(case.judge_labels) > 1]
    stable = [case for case in repeated if len(set(case.judge_labels)) == 1]
    groups = defaultdict(list)
    for case in cases:
        groups[case.group].append(case)
    return {
        "schema_version": 1,
        "case_count": len(cases),
        "exact_agreement": _agreement(cases),
        "cohen_kappa": _kappa(cases),
        "repeatability": len(stable) / len(repeated) if repeated else None,
        "repeated_case_count": len(repeated),
        "disagreement_count": len(disagreements),
        "disagreements": disagreements,
        "groups": {name: {"case_count": len(items), "exact_agreement": _agreement(items),
                           "cohen_kappa": _kappa(items)} for name, items in sorted(groups.items())},
        "uncertainty": {
            "agreement_denominator": len(cases),
            "repeatability_denominator": len(repeated),
            "message": "Calibration describes this labelled set and judge version; it is not ground truth for other data."
        },
    }
