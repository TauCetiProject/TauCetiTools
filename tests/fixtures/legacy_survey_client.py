"""Frozen read_survey from TauCetiWorker 4972ab3722606a8ca9e442a110393a7662513068.

Source: tauceti_worker/github_state.py, function read_survey (including final LF).
SHA256: dd99a748f7a8cee19b55e565e54f16af379db06046233abe39b358941ab54d5c
Exercise the unchanged v1 parser against the service response without network I/O.
Only its read/cache/import scaffolding is stubbed; the function below is unchanged.
"""
import json
import sys
import types
import ast
import hashlib
from pathlib import Path

payload = json.load(sys.stdin)
package = types.ModuleType("legacy_survey_fixture")
contract = types.ModuleType("legacy_survey_fixture.survey_facts")
contract.CONTRACT = "tauceti-survey-facts/v1"
sys.modules[package.__name__] = package
sys.modules[contract.__name__] = contract
__package__ = package.__name__

def read(*args, **kwargs):
    return True, payload

def SurveyRows(rows, counts):
    return rows

def _unavailable(*args):
    return False, None

_sanitize = str

def read_survey(repo, roadmap="any", skip=(), *, numbers=(), stages=None):
    from .survey_facts import CONTRACT

    available, result = read(
        repo,
        "survey",
        roadmap=roadmap,
        skip=",".join(skip),
        numbers=",".join(map(str, numbers)),
        stages=",".join(stages if stages is not None else ("review", "fix", "rebase")),
        _optional=True,
        _with_metadata=True,
    )
    if not available:
        return False, None
    try:
        if result.get("contract") != CONTRACT:
            raise ValueError("incompatible survey contract")
        rows = result["data"]
        if not isinstance(rows, list):
            raise ValueError("invalid survey")
        for row in rows:
            if row["contract"] != CONTRACT or not isinstance(row["pr"], dict) or not isinstance(row["review"], dict):
                raise ValueError("incompatible survey facts")
            facts = row["review"]
            if (
                facts.get("meta") is not None
                and not isinstance(facts["meta"], dict)
                or not isinstance(facts.get("markers"), list)
                or not isinstance(facts.get("rebase_heads"), list)
            ):
                raise ValueError("invalid survey facts")
            if (
                type(row["pr"]["number"]) is not int
                or type(facts["issue_complete"]) is not bool
                or type(facts["review_complete"]) is not bool
            ):
                raise ValueError("invalid survey coverage")
            if facts.get("contest"):
                facts["contest"]["id"] = int(facts["contest"]["id"])
            pr = row["pr"]
            for field in ("author", "headRepository", "headRepositoryOwner"):
                if pr.get(field) is not None and not isinstance(pr[field], dict):
                    raise ValueError("invalid survey PR")
            for field in ("labels", "statusCheckRollup"):
                if not isinstance(pr.get(field, []), list) or any(
                    not isinstance(value, dict) for value in pr.get(field, [])
                ):
                    raise ValueError("invalid survey PR collection")
            if not isinstance(pr.get("target_focuses", []), list) or any(
                not isinstance(value, str) for value in pr.get("target_focuses", [])
            ):
                raise ValueError("invalid survey targets")
            for marker in facts["markers"]:
                if (
                    not isinstance(marker, dict)
                    or type(marker.get("expires_at")) is not int
                    or not isinstance(marker.get("providers"), list)
                    or any(not isinstance(provider, str) for provider in marker["providers"])
                ):
                    raise ValueError("invalid survey claim")
        counts = result.get("author_counts")
        if counts is not None and (
            not isinstance(counts, dict) or any(type(count) is not int or count < 0 for count in counts.values())
        ):
            raise ValueError("invalid survey author counts")
        return True, SurveyRows(rows, counts)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        return _unavailable(repo, "survey", "incompatible_protocol", _sanitize(error))

# Keep the frozen parser separate from the mutable fixture scaffolding.
source = Path(__file__).read_text()
function = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "read_survey")
body = "\n".join(source.splitlines()[function.lineno - 1:function.end_lineno]) + "\n"
if hashlib.sha256(body.encode()).hexdigest() != "dd99a748f7a8cee19b55e565e54f16af379db06046233abe39b358941ab54d5c":
    raise SystemExit("frozen read_survey changed")

available, rows = read_survey("org/repo")
if not available:
    raise SystemExit("unchanged survey client rejected response")
print(json.dumps({"accepted": True, "rows": len(rows)}))
