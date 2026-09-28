"""Small deterministic JSON Pointer assertions shared by evidence scorers."""


def resolve_pointer(value, pointer):
    if pointer == "":
        return value
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not token.isdecimal() or (len(token) > 1 and token.startswith("0")):
                raise KeyError(pointer)
            value = value[int(token)]
        elif isinstance(value, dict):
            value = value[token]
        else:
            raise KeyError(pointer)
    return value


def assert_value(assertions, value, check, *, prefix="", available=True):
    for assertion in assertions:
        name = prefix + assertion.name
        if not available:
            check(name, None, "Required evidence is unavailable")
            continue
        try:
            actual = resolve_pointer(value, assertion.path)
        except (KeyError, IndexError):
            check(name, False, "Required observed path is missing")
            continue
        try:
            if assertion.operator == "exists":
                passed = True
            elif assertion.operator == "equals":
                passed = type(actual) is type(assertion.value) and actual == assertion.value
            elif assertion.operator == "contains":
                passed = isinstance(actual, (str, list, dict)) and assertion.value in actual
            else:
                numeric = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)
                passed = numeric(actual) and numeric(assertion.value) and (
                    actual <= assertion.value if assertion.operator == "max" else actual >= assertion.value)
            check(name, passed, "Observed value assertion " + ("satisfied" if passed else "not satisfied"))
        except TypeError:
            check(name, False, "Observed value has an incompatible type")
