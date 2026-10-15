"""Test-only extension of mongomock (an independent, third-party implementation of MongoDB's aggregation).

mongomock cannot run a few things the analytics endpoints need, so this module adds exactly those. EVERYTHING ELSE -
every $match, $group, $cond, $in, $sort ... - is still executed by mongomock, including all the inner stages of a
$lookup sub-pipeline.

  * $lookup with localField / foreignField AND a sub-pipeline (MongoDB 5.0+): for each input document, run
    [ {$match: {foreignField: <local value>}}, *pipeline ] on the foreign collection and store the result in `as`.
  * $setWindowFields (5.0+), no partitionBy:
      - {$rank: {}}: standard competition ranking, as documented (1, 2, 2, 4; "sortBy can only take one field");
      - {$sum: <expr>} and {$avg: <expr>} over a `documents` window [lower, upper] (integers relative to the current
        row, or "unbounded" / "current"). $sum and $avg ignore non-numeric values (null, missing); $sum of nothing is 0,
        $avg of nothing is null. The output is in sortBy order. Window outputs read INPUT fields, never each other.
  * $densify (5.1+) with explicit bounds [lower, upper]: for every value lower, lower + step, ... below `upper`
    (lower INCLUSIVE, upper EXCLUSIVE) that no document has yet, add a document holding just that value. Documents
    outside the bounds are not removed. Numeric fields, or date fields with unit "day".
  * the expression operators $dateFromString (dateString + format) and $isoDayOfWeek.

Only used when the tests run without a real MongoDB; against a real server nothing here is installed.
tests/test_mongomock_shim.py pins the behaviour of every handler."""
import datetime as _dt

import mongomock.aggregate as _aggregate

_ORIGINAL_LOOKUP = _aggregate._PIPELINE_HANDLERS["$lookup"]
_ORIGINAL_DATE_OPERATOR = _aggregate._Parser._handle_date_operator


def _path(doc, dotted):
    """Value at a dotted path, None when missing (the same document a `{field: None}` filter matches)."""
    value = doc
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _evaluate(doc, expression):
    """An aggregation expression against one document; a missing field is simply 'no value' (None)."""
    try:
        return _aggregate._Parser(doc, ignore_missing_keys=True).parse(expression)
    except KeyError:
        return None


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ----------------------------------------------------------------------------------------------- $lookup
def lookup(in_collection, database, options):
    if "pipeline" not in options:
        return _ORIGINAL_LOOKUP(in_collection, database, options)
    if "let" in options:
        raise NotImplementedError("shim: only the localField/foreignField + pipeline form is implemented")
    for key in ("from", "localField", "foreignField", "as"):
        if key not in options:
            raise ValueError(f"$lookup needs '{key}'")
    foreign = database.get_collection(options["from"])
    pipeline = list(options["pipeline"])
    joined = []
    for doc in in_collection:
        inner = [{"$match": {options["foreignField"]: _path(doc, options["localField"])}}] + pipeline
        out = dict(doc)
        if pipeline and "$group" in pipeline[-1] and not list(foreign.aggregate(inner[:-1])):
            # Real MongoDB: a $group over an empty input emits NO document (mongomock emits one zero-filled row).
            # Emulated here so that "an employee without records gets an empty array" is what the tests see.
            out[options["as"]] = []
        else:
            out[options["as"]] = list(foreign.aggregate(inner))  # inner stages: mongomock's own engine
        joined.append(out)
    return joined


# ------------------------------------------------------------------------------------- $setWindowFields
def _sort_docs(docs, sort_by):
    for field, direction in reversed(list(sort_by.items())):  # stable sorts, least significant key first
        docs.sort(key=lambda d: (_path(d, field) is not None, _path(d, field) or 0), reverse=direction < 0)


def _window_bounds(window, position, count):
    if set(window) != {"documents"}:
        raise NotImplementedError(f"shim: only `documents` windows are implemented, got {sorted(window)}")
    lower, upper = window["documents"]

    def resolve(spec, unbounded):
        if spec == "unbounded":
            return unbounded
        if spec == "current":
            return position
        return position + spec

    low, high = max(resolve(lower, 0), 0), min(resolve(upper, count - 1), count - 1)
    return low, high


def set_window_fields(in_collection, database, options):
    unsupported = set(options) - {"sortBy", "output"}
    if unsupported:
        raise NotImplementedError(f"shim: unsupported $setWindowFields options {sorted(unsupported)}")
    sort_by = options.get("sortBy")
    docs = [dict(d) for d in in_collection]
    if sort_by:
        _sort_docs(docs, sort_by)

    computed = {}  # field -> list of values per document, computed from the INPUT documents only
    for name, spec in options["output"].items():
        if spec == {"$rank": {}}:
            if not isinstance(sort_by, dict) or len(sort_by) != 1:
                raise ValueError("$rank requires sortBy with exactly one field")
            ((field, _),) = sort_by.items()
            ranks, rank, previous = [], 0, object()
            for position, doc in enumerate(docs, start=1):
                value = _path(doc, field)
                if value != previous:  # a new value starts at its own position; equal values keep the rank
                    rank, previous = position, value
                ranks.append(rank)
            computed[name] = ranks
            continue
        operators = set(spec) - {"window"}
        if len(operators) != 1 or next(iter(operators)) not in ("$sum", "$avg"):
            raise NotImplementedError(f"shim: unsupported window output {spec!r} for {name}")
        operator = next(iter(operators))
        window = spec.get("window", {"documents": ["unbounded", "unbounded"]})
        if "documents" in window and not sort_by:
            raise ValueError("a documents window requires sortBy")
        operand = spec[operator]
        values = [_evaluate(d, operand) for d in docs]
        column = []
        for position in range(len(docs)):
            low, high = _window_bounds(window, position, len(docs))
            numbers = [v for v in values[low:high + 1] if _is_number(v)]
            if operator == "$sum":
                column.append(sum(numbers))
            else:
                column.append(sum(numbers) / len(numbers) if numbers else None)
        computed[name] = column
    for name, column in computed.items():
        for doc, value in zip(docs, column):
            doc[name] = value
    return docs


# ---------------------------------------------------------------------------------------------- $densify
def densify(in_collection, database, options):
    unsupported = set(options) - {"field", "range"}
    if unsupported:
        raise NotImplementedError(f"shim: unsupported $densify options {sorted(unsupported)}")
    field, spec = options["field"], options["range"]
    bounds, step, unit = spec.get("bounds"), spec["step"], spec.get("unit")
    if not isinstance(bounds, list) or len(bounds) != 2:
        raise NotImplementedError("shim: only explicit [lower, upper] bounds are implemented")
    docs = [dict(d) for d in in_collection]
    if any(field not in d for d in docs):
        raise ValueError("$densify: every document needs the field")
    if unit is None:
        increment = step
    elif unit == "day":
        if not isinstance(step, int) or isinstance(step, bool):
            raise ValueError("$densify: step must be an integer when unit is given")
        increment = _dt.timedelta(days=step)
    else:
        raise NotImplementedError(f"shim: unit {unit!r}")
    if not step > 0:
        raise ValueError("$densify: step must be positive")
    lower, upper = bounds
    existing = {d[field] for d in docs}
    added, value = [], lower
    while value < upper:  # lower inclusive, upper exclusive
        if value not in existing:
            added.append({field: value})
        try:
            value = value + increment
        except OverflowError:  # Python's datetime ends at 9999-12-31; whatever lies beyond is past any upper bound
            break
    result = docs + added
    result.sort(key=lambda d: d[field])  # stable: existing documents keep their relative order
    return result


# ------------------------------------------------------------------------------------ expression operators
def _date_operator(self, operator, values):
    if operator == "$dateFromString":
        if not isinstance(values, dict) or "dateString" not in values or "format" not in values:
            raise NotImplementedError("shim: $dateFromString needs dateString and format")
        extra = set(values) - {"dateString", "format"}
        if extra:
            raise NotImplementedError(f"shim: $dateFromString options {sorted(extra)}")
        text = self.parse(values["dateString"])
        if text is None:
            return None
        return _dt.datetime.strptime(text, values["format"]).replace(tzinfo=_dt.timezone.utc)
    if operator == "$isoDayOfWeek":
        value = self.parse(values)
        return None if value is None else value.isoweekday()
    return _ORIGINAL_DATE_OPERATOR(self, operator, values)


def install():
    """Idempotent."""
    _aggregate._PIPELINE_HANDLERS["$lookup"] = lookup
    _aggregate._PIPELINE_HANDLERS["$setWindowFields"] = set_window_fields
    _aggregate._PIPELINE_HANDLERS["$densify"] = densify
    _aggregate._Parser._handle_date_operator = _date_operator
