"""Shared fixed-authority, bounded bors observation and exact batch validation."""
import json
import re
import urllib.parse
import urllib.request

REPO = "TauCetiProject/TauCeti"
OBSERVATION_ROOT = "https://bors.taucetiproject.org/repositories/1/active-batches"
MAX_BYTES = 2 * 1024 * 1024
ADMISSION_HEADER = "TauCeti-Admission"
FROZEN_ADMISSION = "frozen-accepted-head/v1"


def sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def valid_observation(data, branch):
    return (isinstance(data, dict) and data.get("schema") == "tauceti-bors.observation/v1"
            and data.get("repo") == REPO and data.get("base") == branch)


def _observation(batch_id=None, branch="main", *, admission=False):
    if not isinstance(branch, str) or not branch:
        raise ValueError("invalid bors branch")
    query = {"base": branch}
    if batch_id is not None:
        if type(batch_id) is not int or batch_id <= 0:
            raise ValueError("invalid bors batch ID")
        query["batch_id"] = batch_id
    request = urllib.request.Request(OBSERVATION_ROOT + "?" + urllib.parse.urlencode(query), headers={
        "Cache-Control": "no-cache", "User-Agent": "TauCetiBorsObservation/1.0"
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        capability = None
        if admission:
            if response.geturl() != request.full_url:
                raise ValueError("redirected scope authority")
            values = response.headers.get_all(ADMISSION_HEADER, [])
            if len(values) > 1:
                raise ValueError("ambiguous admission authority")
            if values == [FROZEN_ADMISSION]:
                capability = FROZEN_ADMISSION
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("bors observation exceeds bound")
    data = json.loads(raw)
    if not valid_observation(data, branch):
        raise ValueError("incorrectly scoped bors observation")
    return (data, capability) if admission else data


def observation(batch_id=None, branch="main"):
    return _observation(batch_id, branch)


def observation_with_admission(batch_id=None, branch="main"):
    """Capability comes only from the same fixed-authority HTTPS response.

    It is deliberately separate from JSON: no body field can enable it. Legacy
    Bors (or a proxy dropping the header) keeps the stricter mutable-PR policy.
    """
    return _observation(batch_id, branch, admission=True)


def requested_batch(data, head, base, *, batch_id, branch="main"):
    if (not sha(head) or not sha(base) or not valid_observation(data, branch)
            or type(batch_id) is not int or batch_id <= 0):
        raise ValueError("invalid bors dispatch identity")
    batch = data.get("requested_batch")
    if (not isinstance(batch, dict) or type(batch.get("id")) is not int
            or batch.get("id") != batch_id or batch.get("state") != "running"
            or batch.get("base") != branch or batch.get("head_sha") != head
            or batch.get("base_sha") != base):
        raise ValueError("stale or mismatched bors dispatch")
    return batch
