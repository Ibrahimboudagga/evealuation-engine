"""Version-bound agreement analysis for human-labelled judge cases."""

from collections import Counter, defaultdict
import hashlib
import json
import math
from typing import Any

from pydantic import Field, model_validator

from app.scenarios.schemas import StrictModel


class CalibrationCase(StrictModel):
    id: str = Field(min_length=1)
    human_label: str = Field(min_length=1)
    human_labels: list[str] = Field(default_factory=list)
    judge_labels: list[str] = Field(min_length=1)
    group: str = "all"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def labels_are_nonempty(self):
        if any(not label.strip() for label in self.judge_labels + self.human_labels):
            raise ValueError("Calibration labels cannot be blank")
        return self


class CalibrationIdentity(StrictModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    rubric_version: str = Field(min_length=1)
    rubric_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    label_set_version: str = Field(min_length=1)
    adjudication_version: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)

    def artifact_sha256(self):
        body = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(body.encode()).hexdigest()


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


def _wilson(successes: int, total: int, z: float = 1.96):
    if not total:
        return {"lower": None, "upper": None, "confidence": .95}
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return {"lower": max(0, center - margin), "upper": min(1, center + margin), "confidence": .95}


def _human_agreement(cases):
    pairs = agreed = 0
    for case in cases:
        labels = case.human_labels
        for left in range(len(labels)):
            for right in range(left + 1, len(labels)):
                pairs += 1
                agreed += labels[left] == labels[right]
    return {"agreement": agreed / pairs if pairs else None, "pair_count": pairs,
            "case_count": sum(len(case.human_labels) > 1 for case in cases)}


def calibrate(cases: list[CalibrationCase], identity: CalibrationIdentity | None = None,
              minimum_cases: int = 30, minimum_group_cases: int = 10):
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
    warnings = []
    if identity is None:
        warnings.append("Calibration is not bound to a judge configuration and cannot authorize release decisions.")
    if len(cases) < minimum_cases:
        warnings.append(f"Calibration has {len(cases)} cases; the configured minimum is {minimum_cases}.")
    sparse = sorted(name for name, items in groups.items() if len(items) < minimum_group_cases)
    if sparse:
        warnings.append("Sparse calibration groups: " + ", ".join(sparse))
    agreements = sum(case.judge_labels[0] == case.human_label for case in cases)
    return {
        "schema_version": 2,
        "judge_identity": identity.model_dump(mode="json") if identity else None,
        "judge_identity_sha256": identity.artifact_sha256() if identity else None,
        "release_eligible": identity is not None and len(cases) >= minimum_cases and not sparse,
        "case_count": len(cases),
        "exact_agreement": _agreement(cases),
        "exact_agreement_interval": _wilson(agreements, len(cases)),
        "cohen_kappa": _kappa(cases),
        "human_inter_rater": _human_agreement(cases),
        "repeatability": len(stable) / len(repeated) if repeated else None,
        "repeated_case_count": len(repeated),
        "disagreement_count": len(disagreements),
        "disagreements": disagreements,
        "groups": {name: {"case_count": len(items), "exact_agreement": _agreement(items),
                           "exact_agreement_interval": _wilson(sum(c.judge_labels[0] == c.human_label for c in items), len(items)),
                           "cohen_kappa": _kappa(items), "below_minimum": len(items) < minimum_group_cases}
                   for name, items in sorted(groups.items())},
        "warnings": warnings,
        "uncertainty": {
            "agreement_denominator": len(cases),
            "repeatability_denominator": len(repeated),
            "minimum_cases": minimum_cases, "minimum_group_cases": minimum_group_cases,
            "message": "Intervals describe this labelled set and exact judge identity; they do not establish truth for other populations."
        },
    }
