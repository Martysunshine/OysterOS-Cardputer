"""Deterministic assertion evaluator. The model never sets a classification.

Inputs are ble_check_access_policy events (details: profile_id, test_id, characteristic_id, precondition,
precondition_established, access, app_status, raw_att_status, security_state, security_before,
security_after, security_transition, value_length, value_sha256, hash_verified) and, for the public read,
ble_read_profile_value events. Per observation:
  informational assertion            -> observation (never a finding)
  precondition not established, or access error/timeout/not_attempted/unknown -> inconclusive
  expected denied: a denial counts only from the layer that enforces the documented policy
      (app_authorization: denied_app, or denied_att with ATT 0x08; link_encryption: denied_att with ATT
      0x05/0x0F/0x0C; link_authentication: denied_att with ATT 0x05/0x0F) -> passed; any other denial
      (wrong layer, absent or unknown ATT code, ATT 0x0A) -> inconclusive;
      returned -> policy state field: false -> violation, true -> passed (transition recorded),
      null -> candidate
  expected allowed: returned (+ hash_verified true for transfer) -> passed;
      denied or hash mismatch -> candidate
Only events whose envelope status is success/denied, whose details.characteristic_id and details.precondition
equal the assertion's, and whose access agrees with the envelope status may drive passed/candidate/confirmed;
every other event is inconclusive (an unsupported event stays unsupported).
Aggregate: violations with no contradicting pass -> confirmed (medium for one, high for >= 2);
violations contradicted by a pass -> candidate; else candidate > passed > inconclusive.
"""

from __future__ import annotations

import hashlib
import json

from oyster.contracts import Event, Finding

_STATE_FIELD = {
    "link_encryption": "encrypted",
    "link_authentication": "authenticated_pairing",
    "app_authorization": "application_authorized",
}
_INCONCLUSIVE_ACCESS = ("error", "timeout", "not_attempted")
_DENIED = ("denied_app", "denied_att")
# ATT error codes that identify the enforcing layer (Bluetooth Core, Vol 3 Part F, 3.4.1.1)
ATT_INSUFFICIENT_AUTHENTICATION = 0x05
ATT_INSUFFICIENT_AUTHORIZATION = 0x08
ATT_INSUFFICIENT_ENC_KEY_SIZE = 0x0C
ATT_INSUFFICIENT_ENCRYPTION = 0x0F
_LINK_DENIAL_CODES = {
    "link_encryption": frozenset(
        {ATT_INSUFFICIENT_AUTHENTICATION, ATT_INSUFFICIENT_ENCRYPTION, ATT_INSUFFICIENT_ENC_KEY_SIZE}
    ),
    "link_authentication": frozenset({ATT_INSUFFICIENT_AUTHENTICATION, ATT_INSUFFICIENT_ENCRYPTION}),
}
_DRIVING_STATUS = ("success", "denied")
_ENVELOPE_FOR_ACCESS = {"returned": "success", "denied_app": "denied", "denied_att": "denied"}
_SIMULATED_NOTE = (
    "Simulated result from the software simulator: validates orchestration and policy "
    "evaluation only, not radio drivers, pairing interoperability or real BLE permissions."
)


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 3] + "..."


def _details(ev: Event) -> dict:
    return ev.details.model_dump(mode="json")


def _assertion(profile: dict, test_id: str) -> dict:
    for a in profile.get("assertions", []):
        if a.get("test_id") == test_id:
            return a
    raise ValueError(f"test_id {test_id!r} is not an assertion of profile {profile.get('profile_id')!r}")


def _policy(profile: dict, assertion: dict) -> str:
    op = "read" if assertion["operation"] == "transfer" else assertion["operation"]
    char = profile.get("characteristics", {}).get(assertion["characteristic"], {})
    return char.get("policy", {}).get(op, "none")


def _relevant(
    profile: dict, assertion: dict, events: list[Event], session_id: str, target_id: str
) -> list[Event]:
    out = []
    for ev in events:
        if ev.session_id != session_id or (ev.target_id is not None and ev.target_id != target_id):
            continue
        d = _details(ev)
        if d.get("profile_id") not in (None, profile.get("profile_id")):
            continue
        if ev.operation == "ble_check_access_policy" and d.get("test_id") == assertion["test_id"]:
            # a public (informational) read is only evidence about its own characteristic
            if assertion["informational"] and d.get("characteristic_id") != assertion["characteristic"]:
                continue
            out.append(ev)
        elif (
            ev.operation == "ble_read_profile_value"
            and assertion["informational"]
            and assertion["operation"] == "read"
            and d.get("characteristic_id") == assertion["characteristic"]
        ):
            out.append(ev)
    return out


def _access(ev: Event, d: dict) -> str:
    """Access outcome; read events map their status (success -> returned)."""
    if ev.operation == "ble_check_access_policy":
        return str(d.get("access") or "unknown")
    if ev.status.value == "success":
        return "returned"
    if ev.status.value == "denied":
        return "denied_att" if d.get("raw_att_status") not in (None, 0) else "denied_app"
    return {"timeout": "timeout", "error": "error"}.get(ev.status.value, "unknown")


def _envelope_problem(ev: Event, assertion: dict, d: dict, access: str) -> str | None:
    """Why this event may not drive passed/candidate/confirmed (None when it may)."""
    status = ev.status.value
    if status not in _DRIVING_STATUS:
        return f"event status is '{status}', not success or denied"
    if d.get("characteristic_id") != assertion["characteristic"]:
        return (
            f"event addresses characteristic {d.get('characteristic_id')!r}, "
            f"not {assertion['characteristic']!r}"
        )
    if d.get("precondition") != assertion["precondition"]:
        return (
            f"event was recorded under precondition {d.get('precondition')!r}, "
            f"not {assertion['precondition']!r}"
        )
    want = _ENVELOPE_FOR_ACCESS.get(access)
    if want is not None and status != want:
        return f"event status '{status}' contradicts access '{access}'"
    return None


def _denial_verdict(ev: Event, d: dict, policy: str, access: str) -> tuple[str, str, str | None]:
    """A denial proves enforcement only from the layer that owns the documented policy."""
    att = d.get("raw_att_status")
    att = att if isinstance(att, int) and not isinstance(att, bool) else None
    shown = f"denied by {access}" + (f" (ATT 0x{att:02X})" if att is not None else "")
    if policy == "none":
        return "passed", f"access {access}", None
    if policy == "app_authorization":
        if access == "denied_app" or (access == "denied_att" and att == ATT_INSUFFICIENT_AUTHORIZATION):
            return "passed", f"access {access}", None
        why = (
            "no ATT error code was recorded"
            if att is None
            else f"ATT 0x{att:02X} is not insufficient authorization (0x08)"
        )
        return (
            "inconclusive",
            f"{shown}; layer does not prove application authorization",
            f"Policy {policy} is enforced by the application, but {ev.event_id} was denied at the ATT layer "
            f"and {why}; the denial is not evidence of application-level enforcement.",
        )
    codes = _LINK_DENIAL_CODES.get(policy)
    if codes is not None:
        if access == "denied_att" and att in codes:
            return "passed", f"access {access} (ATT 0x{att:02X})", None
        if access == "denied_app":
            why = "an application-level denial never proves link-layer enforcement"
        elif att is None:
            why = "no ATT error code was recorded"
        else:
            why = f"ATT 0x{att:02X} is not one of " + "/".join(f"0x{c:02X}" for c in sorted(codes))
        return (
            "inconclusive",
            f"{shown}; layer does not prove {policy}",
            f"Policy {policy} is enforced by the link layer, but {ev.event_id} is not a matching "
            f"denial: {why}.",
        )
    return (
        "inconclusive",
        f"{shown}; policy {policy} cannot be matched to a denial layer",
        f"The documented policy {policy!r} has no known denial layer; {ev.event_id} cannot support a result.",
    )


def _verdict(ev: Event, assertion: dict, policy: str) -> tuple[str, str, str | None]:
    """(verdict, observed fragment, limitation) for one observation.
    verdict: passed | violation | candidate | inconclusive | unsupported."""
    d = _details(ev)
    if ev.status.value == "unsupported":
        return "unsupported", "check unsupported on this route", None
    access = _access(ev, d)
    state = d.get("security_state") or {}
    if d.get("precondition_established") is not True:
        return (
            "inconclusive",
            f"precondition {assertion['precondition']} not established",
            f"Precondition '{assertion['precondition']}' was not established ({ev.event_id}); "
            "this observation cannot support a finding.",
        )
    problem = _envelope_problem(ev, assertion, d, access)
    if problem is not None:
        return (
            "inconclusive",
            f"event not usable ({ev.status.value}/{access})",
            _clip(f"Event {ev.event_id} cannot support a result: {problem}.", 300),
        )
    if access in _INCONCLUSIVE_ACCESS or access not in ("returned", *_DENIED):
        return (
            "inconclusive",
            f"access {access}",
            f"Access ended '{access}' ({ev.event_id}); a timeout or error is never treated as a bypass.",
        )
    if assertion["expected"] == "denied":
        if access in _DENIED:
            return _denial_verdict(ev, d, policy, access)
        if policy == "public":
            return (
                "candidate",
                "value returned",
                "Profile expects denial but documents a public policy; review the profile.",
            )
        if policy == "none":
            return "violation", "value returned although the policy allows no access", None
        field = _STATE_FIELD.get(policy)
        value = state.get(field) if field else None
        if value is False:
            return "violation", f"value returned while {field}=false", None
        if value is True:
            lim = None
            if d.get("security_transition"):
                lim = _clip(
                    f"Link security changed during the access ({ev.event_id}): before "
                    f"{json.dumps(d.get('security_before'))}, after {json.dumps(d.get('security_after'))}; "
                    f"the value was returned with {field}=true.",
                    300,
                )
            return "passed", f"value returned with {field}=true", lim
        return (
            "candidate",
            f"value returned while {field} was unknown",
            f"The stack did not report {field} for {ev.event_id}; the policy state is unknown.",
        )
    # expected allowed
    if access in _DENIED:
        return "candidate", f"access {access} although allowed", None
    if assertion["operation"] == "transfer":
        hv = d.get("hash_verified")
        if hv is True:
            return "passed", "object returned and hash verified", None
        if hv is False:
            return "candidate", "object returned but the hash did not match", None
        return (
            "inconclusive",
            "object returned without hash verification",
            f"Transfer hash was not verified ({ev.event_id}).",
        )
    return "passed", "value returned", None


def _source_mode(events: list[Event]) -> str:
    modes = {ev.source_mode.value for ev in events}
    if "simulated" in modes:
        return "simulated"
    if "offline" in modes:
        return "offline"
    return "live"


def _finding_id(session_id: str, test_id: str, evidence: list[str], retest_of: str | None) -> str:
    h = hashlib.sha256("|".join([session_id, test_id, *evidence, retest_of or ""]).encode()).hexdigest()
    return f"finding-{h[:24]}"


def _summarize(fragments: list[str]) -> str:
    counts: dict[str, int] = {}
    for f in fragments:
        counts[f] = counts.get(f, 0) + 1
    parts = [f"{n}x {f}" if n > 1 else f for f, n in counts.items()]
    return _clip(f"{len(fragments)} observation(s): " + "; ".join(parts), 400)


_TITLES = {
    "observation": "{c} {o} is public and was observed",
    "passed": "{c} {o} enforced as documented",
    "confirmed": "{c} {o} allowed contrary to documented policy",
    "candidate": "{c} {o} possibly contrary to documented policy",
    "inconclusive": "{c} {o} check inconclusive",
    "unsupported": "{c} {o} check unsupported on this route",
}


def evaluate_assertion(
    profile: dict,
    test_id: str,
    events: list[Event],
    *,
    session_id: str,
    target_id: str,
    retest_of: str | None = None,
) -> Finding:
    """Classify one profile assertion from device evidence. Raises ValueError for an unknown test_id or when
    no event of this session/target addresses the assertion (a finding always cites evidence)."""
    a = _assertion(profile, test_id)
    rel = _relevant(profile, a, events, session_id, target_id)
    if not rel:
        raise ValueError(f"no evidence in {session_id} addresses {test_id}")
    policy = _policy(profile, a)
    limitations: list[str] = []
    fragments: list[str] = []

    if a.get("informational"):
        returned = sum(1 for ev in rel if _access(ev, _details(ev)) == "returned")
        for ev in rel:
            d = _details(ev)
            acc = _access(ev, d)
            n = d.get("value_length")
            fragments.append(
                f"value returned ({n} bytes)" if acc == "returned" and n is not None else f"access {acc}"
            )
        classification, severity = "observation", "informational"
        confidence = "high" if returned == len(rel) else ("medium" if returned else "low")
        limitations.append("A public read is informational and is never a vulnerability.")
    else:
        verdicts = []
        for ev in rel:
            v, frag, lim = _verdict(ev, a, policy)
            verdicts.append(v)
            fragments.append(frag)
            if lim and lim not in limitations:
                limitations.append(lim)
        violations = verdicts.count("violation")
        passes = verdicts.count("passed")
        candidates = verdicts.count("candidate")
        sev_violated = a.get("severity_if_violated", "medium")
        if violations and not passes:
            classification, severity = "confirmed", sev_violated
            confidence = "high" if violations >= 2 else "medium"
        elif violations:
            classification, severity, confidence = "candidate", sev_violated, "low"
            limitations.append(
                f"Inconsistent observations: {violations} contrary to policy, {passes} as documented."
            )
        elif candidates:
            classification, severity, confidence = "candidate", sev_violated, "low"
        elif passes:
            classification, severity = "passed", "informational"
            confidence = "high" if passes >= 2 else "medium"
        elif verdicts and all(v == "unsupported" for v in verdicts):
            classification, severity, confidence = "unsupported", "not_assessed", "low"
        else:
            classification, severity, confidence = "inconclusive", "not_assessed", "low"

    source_mode = _source_mode(rel)
    if source_mode == "simulated":
        limitations.append(_SIMULATED_NOTE)
    evidence = list(dict.fromkeys(ev.event_id for ev in rel))
    expected = _clip(
        f"{a['operation']} of {a['characteristic']} expected {a['expected']} under precondition "
        f"{a['precondition']} (policy {policy}). {a.get('description', '')}".strip(),
        400,
    )
    return Finding(
        finding_id=_finding_id(session_id, test_id, evidence, retest_of),
        session_id=session_id,
        target_id=target_id,
        profile_id=profile.get("profile_id", ""),
        test_id=test_id,
        classification=classification,
        title=_clip(_TITLES[classification].format(c=a["characteristic"], o=a["operation"]), 160),
        expected_policy=expected,
        observed_behavior=_summarize(fragments),
        evidence_ids=evidence,
        severity=severity,
        confidence=confidence,
        source_mode=source_mode,
        limitations=[_clip(x, 300) for x in limitations],
        retest_of=retest_of,
    )
