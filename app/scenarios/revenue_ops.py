"""Deterministic Revenue Ops contract checks, not a business-quality oracle."""

import math


def _cells(rows):
    return {(row.day, row.lt): row.value for row in rows}


def evaluate_revenue_ops(policy, actual, check):
    if actual is None:
        check("revenue_ops_evidence", None, "Revenue Ops instrumentation is unavailable")
        return
    for field in ("hotel_id", "date_from", "date_to", "chosen_cluster"):
        observed = getattr(actual, field)
        check("revenue_ops:" + field, observed == getattr(policy, field) if observed is not None else None,
              "Observed request and cluster must match the pinned scenario contract")
    expected = _cells(policy.day_lt)
    for field in ("day_lt", "clustering_inputs"):
        observed = getattr(actual, field)
        check("revenue_ops:" + field, _cells(observed) == expected if observed is not None else None,
              "DAY x LT values and clustering inputs must match the pinned input matrix")
    check("revenue_ops:truth_reference", True if policy.reference_status != "unreviewed" else None,
          "Anomaly truth status: " + policy.reference_status + "; synthetic truth demonstrates contract behavior only")
    observed_labels = actual.anomaly_labels
    expected_labels = {(label.day, label.lt): label.anomalous for label in policy.anomaly_labels}
    check("revenue_ops:anomaly_labels",
          {(label.day, label.lt): label.anomalous for label in observed_labels} == expected_labels
          if observed_labels is not None else None,
          "Labels must match the declared reference cells exactly; a reference ID does not establish reviewer quality")
    observed_cells = _cells(actual.day_lt) if actual.day_lt is not None else None
    for calculation in policy.calculations:
        values = ([observed_cells.get((cell.day, cell.lt)) for cell in calculation.cells]
                  if observed_cells is not None else None)
        if values is None or any(value is None for value in values):
            expected_value = None
        elif calculation.operation == "sum":
            expected_value = sum(values)
        elif calculation.operation == "mean":
            expected_value = sum(values) / len(values)
        else:
            expected_value = len(values)
        for label, mapping in (("calculation", actual.report_calculations), ("narrative", actual.narrative_claims)):
            if label == "narrative" and calculation.name not in policy.narrative_calculations:
                continue
            value = mapping.get(calculation.name) if mapping is not None else None
            passed = None if value is None or expected_value is None else math.isclose(
                value, expected_value, abs_tol=calculation.absolute_tolerance, rel_tol=0)
            check(f"revenue_ops:{label}:{calculation.name}", passed,
                  "Reported numeric claim must reconcile to the declared calculation over observed cells; prose interpretation is not scored")
