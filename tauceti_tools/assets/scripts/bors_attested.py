"""Confirm that the queue attested exactly this staging candidate (an attested landing, decision 200).

A `tauceti-bors-attested` dispatch asks pr-build to land a batch without rebuilding it: a trusted repair
worker already built, audited and linted exactly this commit with main's TauCetiTools pin and policy
files, and the queue (TauCetiWorker `Controller._attestation_problem`) accepted that. The dispatch
action alone is not trusted for that: the queue's own observation of the requested batch must be
`running` with exactly this head and base and must name the attestation of exactly this commit and
tree. Payload fields never choose anything here; the authority is the fixed observation endpoint.

Usage: bors_attested.py --head SHA --base SHA --batch-id N --branch main --tree SHA
Exits 0 when the queue attests the candidate, 1 otherwise (with the reason on stderr).
"""
import argparse
import sys

from bors_observation import observation, requested_batch, sha


def attested(data, head, base, tree, *, batch_id, branch="main"):
    """The requested batch's `attestation` when it names exactly `head` and `tree`; raises otherwise."""
    batch = requested_batch(data, head, base, batch_id=batch_id, branch=branch)
    note = batch.get("attestation")
    if not isinstance(note, dict):
        raise ValueError("the queue does not attest this batch")
    if not sha(tree) or note.get("tip") != head or note.get("tree") != tree:
        raise ValueError("the queue's attestation is of another commit or tree")
    return note


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--head", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--batch-id", type=int, required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--tree", required=True)
    args = parser.parse_args()
    try:
        data = observation(args.batch_id, args.branch)
        note = attested(data, args.head, args.base, args.tree, batch_id=args.batch_id, branch=args.branch)
    except (KeyError, TypeError, ValueError, OSError) as error:
        print(f"attested landing refused: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(f"the queue attests {args.head} (tree {args.tree}): task {note.get('task_id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
