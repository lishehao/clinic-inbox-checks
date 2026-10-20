"""A small, declarative check language.

Hand-written and model-written checks use the same language and the same
interpreter, so the two can be compared run by run. Checks are data, never
code: a model-written check cannot execute anything.

Events come from the simulator's server-side log, for example
  {"seq": 7, "event": "view", "page": "chart", "patient": "P01", "tab": "meds"}
  {"seq": 9, "event": "route", "message": "MSG01", "dest": ["pharmacy"], "persisted": true}

Check kinds
  exists       {"event": PATTERN}                    some event matches
  never        {"event": PATTERN}                    no event matches
  before       {"first": PATTERN, "then": PATTERN}   every "then" event comes after some "first" event
  followed_by  {"anchor": PATTERN, "then": PATTERN}  every "anchor" event is followed later by a "then" event
  final        {"path": "messages.MSG01.routes", "op": OP, "value": ...}   stored state at the end of the run
  each         {"over": [{"$m": "MSG06", ...}, ...], "check": CHECK}       CHECK holds for every binding

PATTERN keys: event, page, tab, patient, message, med, persisted (exact match),
  dest_includes, dest_equals, text_contains_any, text_contains_all,
  text_excludes_any, text_ends_with
Final OPs: equals, includes, not_includes, is_not_empty, contains_any,
  contains_all, excludes_any, ends_with
"""

import copy

EXACT_KEYS = {"event", "page", "tab", "patient", "message", "med", "persisted"}
SPECIAL_KEYS = {
    "dest_includes", "dest_equals", "text_contains_any", "text_contains_all",
    "text_excludes_any", "text_ends_with",
}
PATTERN_KEYS = EXACT_KEYS | SPECIAL_KEYS
FINAL_OPS = {
    "equals", "includes", "not_includes", "is_not_empty",
    "contains_any", "contains_all", "excludes_any", "ends_with",
}
KINDS = {"exists", "never", "before", "followed_by", "final", "each"}
EVENT_NAMES = {"view", "route", "save_draft", "mark_done", "edit_med"}
_MISSING = object()


def norm(text):
    """Lowercase, straighten quotes, collapse whitespace, drop trailing punctuation."""
    text = str(text or "").lower()
    for curly, plain in (("‘", "'"), ("’", "'"), ("“", '"'), ("”", '"')):
        text = text.replace(curly, plain)
    text = " ".join(text.split())
    return text.rstrip(" .!\"'")


def substitute(obj, bindings):
    if not bindings:
        return obj
    if isinstance(obj, str):
        if obj in bindings:
            return copy.deepcopy(bindings[obj])
        for key, value in bindings.items():
            if isinstance(value, str):
                obj = obj.replace(key, value)
        return obj
    if isinstance(obj, list):
        return [substitute(x, bindings) for x in obj]
    if isinstance(obj, dict):
        return {k: substitute(v, bindings) for k, v in obj.items()}
    return obj


def _as_list(value):
    return value if isinstance(value, list) else [value]


def match(pattern, event):
    for key, want in pattern.items():
        if key == "dest_includes":
            if want not in (event.get("dest") or []):
                return False
        elif key == "dest_equals":
            if sorted(event.get("dest") or []) != sorted(_as_list(want)):
                return False
        elif key == "text_contains_any":
            text = norm(event.get("text"))
            if not any(norm(w) in text for w in _as_list(want)):
                return False
        elif key == "text_contains_all":
            text = norm(event.get("text"))
            if not all(norm(w) in text for w in _as_list(want)):
                return False
        elif key == "text_excludes_any":
            text = norm(event.get("text"))
            if any(norm(w) in text for w in _as_list(want)):
                return False
        elif key == "text_ends_with":
            if not norm(event.get("text")).endswith(norm(want)):
                return False
        elif event.get(key) != want:
            return False
    return True


def resolve(state, path):
    current = state
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return _MISSING
    return current


def _final(value, op, arg):
    if value is _MISSING:
        return False
    if op == "equals":
        if isinstance(value, list) and isinstance(arg, list):
            return sorted(value) == sorted(arg)
        return value == arg
    if op == "includes":
        return isinstance(value, list) and arg in value
    if op == "not_includes":
        return isinstance(value, list) and arg not in value
    if op == "is_not_empty":
        return bool(value)
    text = norm(value)
    if op == "contains_any":
        return any(norm(w) in text for w in _as_list(arg))
    if op == "contains_all":
        return all(norm(w) in text for w in _as_list(arg))
    if op == "excludes_any":
        return not any(norm(w) in text for w in _as_list(arg))
    if op == "ends_with":
        return text.endswith(norm(arg))
    raise ValueError(f"unknown final op {op}")


def _result(check, passed, violation_seq=None, detail="", children=None):
    out = {
        "id": check.get("id", ""),
        "desc": check.get("desc", ""),
        "kind": check["kind"],
        "passed": bool(passed),
        "violation_seq": violation_seq,
        "detail": detail,
    }
    if children is not None:
        out["children"] = children
    return out


def evaluate(check, events, state, bindings=None):
    c = substitute(check, bindings)
    kind = c["kind"]
    if kind == "exists":
        hits = [e for e in events if match(c["event"], e)]
        return _result(c, bool(hits), None, f"first match at event {hits[0]['seq']}" if hits else "no matching event")
    if kind == "never":
        hits = [e for e in events if match(c["event"], e)]
        if hits:
            return _result(c, False, hits[0]["seq"], f"matching event {hits[0]['seq']}")
        return _result(c, True, None, "no matching event")
    if kind == "before":
        firsts = [e["seq"] for e in events if match(c["first"], e)]
        thens = [e["seq"] for e in events if match(c["then"], e)]
        earliest = min(firsts) if firsts else None
        for seq in thens:
            if earliest is None or earliest > seq:
                when = "never happened" if earliest is None else f"first happened at event {earliest}"
                return _result(c, False, seq, f"event {seq} came first; the required earlier step {when}")
        return _result(c, True, None, "vacuous: the later event never happened" if not thens else "order held")
    if kind == "followed_by":
        anchors = [e["seq"] for e in events if match(c["anchor"], e)]
        thens = [e["seq"] for e in events if match(c["then"], e)]
        for seq in anchors:
            if not any(t > seq for t in thens):
                return _result(c, False, seq, f"event {seq} was never followed by the required event")
        return _result(c, True, None, "vacuous: no anchor event" if not anchors else "every anchor was followed")
    if kind == "final":
        value = resolve(state, c["path"])
        passed = _final(value, c["op"], c.get("value"))
        shown = "missing" if value is _MISSING else repr(value)[:160]
        return _result(c, passed, None, f"{c['path']} = {shown}")
    if kind == "each":
        children = [evaluate(c["check"], events, state, b) for b in c["over"]]
        failed = [ch for ch in children if not ch["passed"]]
        seqs = [ch["violation_seq"] for ch in failed if ch["violation_seq"] is not None]
        return _result(
            c,
            not failed,
            min(seqs) if seqs else None,
            f"{len(failed)} of {len(children)} failed" if failed else f"all {len(children)} passed",
            children,
        )
    raise ValueError(f"unknown check kind {kind}")


def run_checks(checks, events, state):
    results = [evaluate(c, events, state) for c in checks]
    return {"passed": all(r["passed"] for r in results), "results": results}


def _pattern_errors(pattern, where):
    errors = []
    if not isinstance(pattern, dict) or not pattern:
        return [f"{where}: pattern must be a non-empty object"]
    for key in pattern:
        if key not in PATTERN_KEYS:
            errors.append(f"{where}: unknown pattern key {key!r}")
    if "event" in pattern and pattern["event"] not in EVENT_NAMES:
        errors.append(f"{where}: unknown event {pattern['event']!r}")
    return errors


def validate(check, where="check"):
    """Return a list of problems; an empty list means the check can be run."""
    if not isinstance(check, dict):
        return [f"{where}: not an object"]
    kind = check.get("kind")
    if kind not in KINDS:
        return [f"{where}: unknown kind {kind!r}"]
    if kind in ("exists", "never"):
        return _pattern_errors(check.get("event"), f"{where}.event")
    if kind == "before":
        return _pattern_errors(check.get("first"), f"{where}.first") + _pattern_errors(check.get("then"), f"{where}.then")
    if kind == "followed_by":
        return _pattern_errors(check.get("anchor"), f"{where}.anchor") + _pattern_errors(check.get("then"), f"{where}.then")
    if kind == "final":
        errors = []
        path = check.get("path")
        if not isinstance(path, str) or not path.startswith(("messages.", "patients.")):
            errors.append(f"{where}.path must start with messages. or patients.")
        if check.get("op") not in FINAL_OPS:
            errors.append(f"{where}.op unknown: {check.get('op')!r}")
        return errors
    if kind == "each":
        over = check.get("over")
        if not isinstance(over, list) or not over or not all(isinstance(b, dict) for b in over):
            return [f"{where}.over must be a non-empty list of objects"]
        return validate(check.get("check"), f"{where}.check")
    return []
