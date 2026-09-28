"""Checks over exported tool, retrieval, state, side-effect and turn evidence.

These verify observations from an instrumented application. They do not perform
an independent security audit or infer semantic truth from citation identifiers.
"""

from app.scenarios.assertions import assert_value, resolve_pointer


def evaluate_trace(scenario, evidence, check):
    calls = evidence.tool_calls or []
    complete = evidence.tool_calls is not None and evidence.trace_complete
    for expected in scenario.tool_expectations:
        prefix = f"tool:{expected.tool_name}:{expected.occurrence}:"
        matching = [call for call in calls if call.name == expected.tool_name]
        if not complete:
            check(prefix + "call", None, "A complete tool trace is required")
            continue
        if len(matching) <= expected.occurrence:
            check(prefix + "call", False, "Expected tool attempt is absent from the complete trace")
            continue
        if len(matching) > 1 and any(c.sequence is None for c in matching):
            check(prefix + "call", None, "Repeated tool attempts require recorded sequence numbers")
            continue
        matching.sort(key=lambda c: c.sequence if c.sequence is not None else 0)
        observed = matching[expected.occurrence]
        check(prefix + "call", observed.status == "succeeded", "The selected tool attempt must succeed")
        assert_value(expected.arguments, observed.arguments, check, prefix=prefix + "argument:",
                     available=observed.arguments is not None)
        assert_value(expected.result, observed.result.value if observed.result else None, check,
                     prefix=prefix + "result:", available=observed.result is not None)
    if scenario.tool_order:
        if not complete or any(c.sequence is None for c in calls):
            check("tool_order", None, "Tool ordering requires a complete trace with explicit sequence numbers")
        else:
            ordered = iter(sorted(calls, key=lambda c: c.sequence))
            passed = all(any(call.name == name and call.status == "succeeded" for call in ordered)
                         for name in scenario.tool_order)
            check("tool_order", passed, "Required successful calls must appear in the declared order")

    documents = evidence.retrievals or []
    retrieval_complete = evidence.retrievals is not None and evidence.retrieval_complete
    if scenario.retrieval:
        policy = scenario.retrieval
        expected = set(policy.relevant_source_ids)
        observed = {d.source_id for d in documents}
        recall = len(expected & observed) / len(expected)
        check("retrieval_recall", recall >= policy.min_recall if retrieval_complete else None,
              "Recall uses the configured relevant source IDs; requires complete retrieval evidence")
        if policy.max_irrelevant is not None:
            check("retrieval_irrelevant", len(observed - expected) <= policy.max_irrelevant if retrieval_complete else None,
                  "Counts observed source IDs outside the declared relevant set")
    for claim in scenario.grounding:
        check("grounding_reference:" + claim.name, True if claim.reference_status != "unreviewed" else None,
              "Reference status: " + claim.reference_status + "; semantic truth requires independently reviewed references")
        try:
            actual = resolve_pointer(evidence.output, claim.claim_path)
            valid_claim = type(actual) is type(claim.expected_value) and actual == claim.expected_value
        except (KeyError, IndexError):
            valid_claim = False
        check("grounding_claim:" + claim.name, valid_claim, "Output claim must match the configured reference")
        matches = [d for d in documents if d.source_id == claim.source_id]
        if not matches:
            supported = False if retrieval_complete else None
        elif any(d.content is not None and claim.quote in d.content for d in matches):
            supported = True
        else:
            supported = None if any(d.content is None for d in matches) else False
        check("grounding_source:" + claim.name, supported,
              "The configured supporting quote must occur in observed source content; this is an exact evidence check")

    for expected in scenario.state_checks:
        actual = getattr(evidence, "state_" + expected.phase)
        assert_value(expected.assertions, actual, check, prefix=f"state:{expected.name}:{expected.phase}:",
                     available=actual is not None)
    if scenario.side_effect_policy:
        policy = scenario.side_effect_policy
        effects = evidence.side_effects or []
        complete = evidence.side_effects is not None and evidence.side_effects_complete
        check("side_effects_allowed", all(e.name in policy.allowed for e in effects) if complete else None,
              "Every attempted side effect must be explicitly allowed; a complete observation is required")
        for name in policy.required:
            check("side_effect_required:" + name,
                  any(e.name == name and e.status == "succeeded" for e in effects) if complete else None,
                  "Required side effect must have an observed successful execution")
        if policy.require_authorized:
            authorized = None
            if complete:
                authorized = False if any(e.authorized is False for e in effects) else (
                    None if any(e.authorized is None for e in effects) else True)
            check("side_effects_authorized", authorized,
                  "Uses authorization decisions exported by the application boundary; this is not an independent isolation audit")

    if scenario.turns:
        if evidence.turns is None or not evidence.turns_complete:
            check("turn_sequence", None, "A complete bounded turn trace is required")
        else:
            turns = sorted(evidence.turns, key=lambda t: t.sequence)
            check("turn_sequence", [t.id for t in turns] == [t.id for t in scenario.turns],
                  "All declared turns must execute exactly once in the declared order")
            by_id = {t.id: t for t in turns}
            for expected in scenario.turns:
                observed = by_id.get(expected.id)
                if observed is None:
                    continue  # The complete-trace absence is already a verified failure.
                assert_value(expected.assertions, observed.output, check, prefix=f"turn:{expected.id}:output:")
                assert_value(expected.state_assertions, observed.state, check, prefix=f"turn:{expected.id}:state:",
                             available=observed.state is not None)
