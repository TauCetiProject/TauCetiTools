"""Public CI reporting names. These aliases do not affect fleet admission or routing."""
import json
import re

# Permanent enrolled machine identities also identify older jobs whose requested
# labels predate the broker's human-readable fleet-host label.
FLEET_NAMES = {
    "4fab2fed9a5145f894d066c7bb28ace4": "eastanglia",
    "11e2756fc11042d798f759410573a935": "leanfro",
}
HOST_NAMES = {"chris-server": "eastanglia", "chris": "eastanglia", "tauchonki": "leanfro"}
RUNNERS = ("github", "namespace", "eastanglia", "leanfro", "self-hosted")
LABELS = {"github": "GitHub", "namespace": "Namespace", "eastanglia": "eastanglia",
          "leanfro": "leanfro", "self-hosted": "Other self-hosted"}


def reporting_kind(kind, labels, runner_name=""):
    """Keep unidentified self-hosted jobs separate; never guess their owner."""
    if kind != "self-hosted":
        return kind
    if isinstance(labels, str):
        labels = json.loads(labels or "[]")
    names = {FLEET_NAMES[label[6:]] for label in (labels or [])
             if label.startswith("fleet-") and label[6:] in FLEET_NAMES}
    names |= {HOST_NAMES[label[11:]] for label in (labels or [])
              if label.startswith("fleet-host-") and label[11:] in HOST_NAMES}
    names |= {alias for identity, alias in FLEET_NAMES.items()
              if re.fullmatch(r"fleet-" + identity[:12] + r"-[a-f0-9]{16}", runner_name or "")}
    return next(iter(names)) if len(names) == 1 else kind
