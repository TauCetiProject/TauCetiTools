"""Snapshot and compare the compiled outputs of two builds of the same commit.

Run with: python3 scripts/compare-build-outputs.py manifest BUILD_DIR OUT_FILE
          python3 scripts/compare-build-outputs.py compare FRESH_MANIFEST REFERENCE_MANIFEST [REFERENCE_BUILD_DIR]

`manifest` records SHA-256 of every build artifact under BUILD_DIR. `compare` checks that two
such manifests agree: every artifact must appear in both at the same relative path with the
same digest. A difference means the cache handed a build something a clean compile of the same
sources does not produce.

The comparison is on SHA-256 of the file contents, deliberately not on Lake's own recorded
output hashes. `Lake.Hash` is a single UInt64 and explicitly not cryptographic, so comparing
Lake's hashes would inherit the very weakness this check exists to cover.

Splitting snapshot from compare is what lets the caller hash the clean build BEFORE anything
else runs on that machine. It also keeps the two sides symmetric: a manifest must be taken
right after `lake build`, since later commands such as `lake exe axioms` add root-package
outputs on one side only and would otherwise read as divergence.

SUFFIXES is every artifact Lake produces for a module, not just `.olean`. A cache entry can
differ in `Foo.olean.private` or in generated C while `Foo.olean` matches, and private and
server oleans affect what later imports see.

One kind of difference is known to be benign. Lean stores each definition's compiled LCNF code
in the `.olean.private`, and the free variables in that code are named `_uniq.<k>.<n>`, where
`<k>` depends on how parallel elaboration was scheduled. The same source can then give
`.olean.private` files whose bytes differ while everything else, `.ir` and `.c` included, is
identical. So `manifest` also records, for each `.olean.private`, a digest of the file in which
only the number and hash fields of each `_uniq` Name object are masked (see
`private_olean_digest`). `compare`, given REFERENCE_BUILD_DIR, accepts a differing
`.olean.private` as benign private-olean nondeterminism only when:

  * every differing artifact is an `.olean.private`, and no artifact is missing on either side,
    so the module's `.olean`, `.olean.server`, `.ilean`, `.ir` and `.c` all match byte for byte;
  * the cached file hashes to what the module's Lake trace records for it;
  * both files decode, and their digests agree: the two are byte for byte the same apart from
    those masked fields, and the names in them are renumbered one to one, with correct hashes.

Anything else, including a file that does not decode, is a divergence. Each benign case is
named in the log, as a notice and in the step summary, but does not fail the comparison.

Statuses written to $GITHUB_OUTPUT as `status=`:

  agree         every artifact matched, apart from benign private-olean nondeterminism
  disagree      a path or a digest differed, in either direction
  inconclusive  either manifest was empty, so there was nothing to compare

`inconclusive` is a reportable outcome, not a pass. `compare` exits 0 in every case: the caller
decides what to do, and the from-source build's own success must never depend on this script.
"""

import hashlib
import json
import multiprocessing
import os
import struct
import sys

SUFFIXES = (
    ".olean", ".olean.private", ".olean.server",
    ".ilean", ".ir", ".ir.sig", ".c", ".bc",
)
PRIVATE = ".olean.private"
# Manifest lines for the decoded digest of an `.olean.private`; SHA-256 lines are bare hex.
CANONICAL = "canonical "

# --- Decoding a module's oleans (Lean v4.37 format) -------------------------------------------
#
# A module's `.olean`, `.olean.server` and `.olean.private` are three consecutive parts of one
# compacted region: each file is an 88-byte header (magic, version, flags, Lean version, githash,
# and the address the file is mapped at), the data size, the data, then a trailer listing the
# closure fn-pointer fields to relocate and the libraries they come from. The first data word is
# the address of the part's root object, and later parts point into earlier ones. Objects are
# Lean runtime objects with an 8-byte header: rc (0 here), byte size, `other` and tag.

M64 = (1 << 64) - 1
MURMUR = 0xc6a4a7935bd1e995
HEADER = 88
DATA = HEADER + 8
# Header of a Name.str / Name.num constructor: rc 0, 32 bytes, 2 object fields, tag 1 / 2.
NAME_STR = bytes([0, 0, 0, 0, 32, 0, 2, 1])
NAME_NUM = bytes([0, 0, 0, 0, 32, 0, 2, 2])
ANONYMOUS_HASH = 1723
U64 = struct.Struct("<Q")


def murmur64a(data, seed):
    """Lean's `hash_str`, MurmurHash64A."""
    n = len(data)
    h = (seed ^ (n * MURMUR)) & M64
    end = n - n % 8
    for (k,) in struct.iter_unpack("<Q", data[:end]):
        k = (k * MURMUR) & M64
        k ^= k >> 47
        k = (k * MURMUR) & M64
        h ^= k
        h = (h * MURMUR) & M64
    rest = data[end:]
    if rest:
        for i in range(len(rest) - 1, -1, -1):
            h ^= rest[i] << (8 * i)
        h = (h * MURMUR) & M64
    h ^= h >> 47
    h = (h * MURMUR) & M64
    h ^= h >> 47
    return h


def mix_hash(h, k):
    """Lean's `mixHash` (`lean_uint64_mix_hash`)."""
    k = (k * MURMUR) & M64
    k ^= k >> 47
    k ^= MURMUR
    h ^= k
    return (h * MURMUR) & M64


def lake_hash(data):
    """`Lake.Hash` of a binary file: `mixHash nil (ByteArray.hash bytes)`."""
    return mix_hash(ANONYMOUS_HASH, murmur64a(data, 11))


class Undecodable(Exception):
    pass


def private_olean_digest(olean, server, private):
    """Digest of an `.olean.private` up to a renumbering of `_uniq` names, as `HEX/N`.

    Every object in the private part's data section is walked in address order, checking that
    the objects tile the section exactly. A Name.num object there whose prefix chain ends at
    `_uniq` (a Name.str on the anonymous name) and whose stored name hashes are all correct is a
    compiler-internal name; only its number and hash fields are masked. The digest is SHA-256 of
    the whole file with those fields zeroed, the offsets that were masked, and for each masked
    object the order in which its full name first occurs. Two files therefore agree exactly when
    they are byte for byte the same except for those fields, at the same offsets, and the two
    numberings are one-to-one: no padding, flag, pointer, cached hash or table can differ. N is the
    number of masked names.
    """
    parts = []
    for data in (olean, server, private):
        if len(data) < DATA + 8 or data[:5] != b"olean":
            raise Undecodable("not an olean")
        base = U64.unpack_from(data, 80)[0]
        size = U64.unpack_from(data, HEADER)[0]
        if size % 8 or DATA + size > len(data):
            raise Undecodable("truncated")
        parts.append((base + DATA, base + DATA + size, base, data))
    for (_, hi, _, _), (next_lo, _, _, _) in zip(parts, parts[1:], strict=False):
        if hi > next_lo:
            raise Undecodable("overlapping parts")

    def locate(addr):
        if addr & 7:
            return None
        for lo, hi, base, data in parts:
            if lo <= addr < hi:
                return data, addr - base, hi - base
        return None

    def name_hash(addr, header):
        """Stored hash of the Name object of the given header at `addr`, or None."""
        found = locate(addr)
        if found is None:
            return None
        data, off, end = found
        if off + 32 > end or data[off:off + 8] != header:
            return None
        return found

    uniq_hash = mix_hash(ANONYMOUS_HASH, murmur64a(b"_uniq", 11))

    def is_uniq_str(addr):
        found = name_hash(addr, NAME_STR)
        if found is None:
            return False
        data, off, _ = found
        anonymous, text, stored = struct.unpack_from("<QQQ", data, off + 8)
        if anonymous != 1 or stored != uniq_hash:
            return False
        located = locate(text)
        if located is None:
            return False
        sdata, soff, send = located
        return (soff + 40 <= send and sdata[soff:soff + 8] == bytes([0, 0, 0, 0, 1, 0, 0, 249])
                and sdata[soff + 8:soff + 32] == struct.pack("<QQQ", 6, 6, 5)
                and sdata[soff + 32:soff + 40] == b"_uniq\0\0\0")

    names = {}  # address of a Name.num -> interned id of its full name, or None

    def uniq_name(addr):
        """Interned id of the `_uniq.<k>...` name at `addr`, or None; iterative over the chain."""
        chain = []
        while addr not in names:
            found = name_hash(addr, NAME_NUM)
            if found is None:
                break
            data, off, _ = found
            prefix, value, stored = struct.unpack_from("<QQQ", data, off + 8)
            chain.append((addr, prefix, value, stored))
            addr = prefix
        if addr in names:
            known = names[addr]
        elif is_uniq_str(addr):
            known = ("_uniq",)
        else:
            known = None
        for link, prefix, value, stored in reversed(chain):
            if known is not None and value & 1:
                pdata, poff, _ = locate(prefix)
                if stored == mix_hash(U64.unpack_from(pdata, poff + 24)[0], value >> 1):
                    known = interned.setdefault((known, value >> 1), len(interned))
                else:
                    known = None
            else:
                known = None
            names[link] = known
        return names.get(chain[0][0]) if chain else names.get(addr)

    interned = {}
    lo, hi, base, private_data = parts[2]
    masked = []
    off = DATA + 8  # the first data word is the root pointer
    end = hi - base
    while off < end:
        rc, size, other, tag = struct.unpack_from("<iHBB", private_data, off)
        if rc != 0:
            raise Undecodable("object with a reference count")
        if tag <= 245 or tag in (250, 251, 252, 253):
            length = size
        elif tag == 246:
            length = 24 + 8 * U64.unpack_from(private_data, off + 8)[0]
        elif tag == 248:
            length = 24 + other * U64.unpack_from(private_data, off + 8)[0]
        elif tag == 249:
            length = 32 + U64.unpack_from(private_data, off + 8)[0]
        else:
            raise Undecodable(f"unexpected object tag {tag}")
        if length < 8 or off + length > end:
            raise Undecodable("bad object size")
        if private_data[off:off + 8] == NAME_NUM:
            name = uniq_name(base + off)
            if name is not None:
                masked.append((off, name))
        off += (length + 7) & ~7
    if off != end:
        raise Undecodable("objects do not fill the data section")

    blank = bytearray(private_data)
    order = {}
    final = hashlib.sha256()
    for off, name in masked:
        blank[off + 16:off + 32] = bytes(16)
        final.update(struct.pack("<QQ", off, order.setdefault(name, len(order))))
    final.update(b"F" + bytes(blank))
    return f"{final.hexdigest()}/{len(order)}"


def canonical_digest(stem):
    """`private_olean_digest` of the module at path `stem`, `none` or `undecodable:<why>`."""
    try:
        parts = []
        for suffix in (".olean", ".olean.server", PRIVATE):
            with open(stem + suffix, "rb") as f:
                parts.append(f.read())
        if not any(b"_uniq\0" in part for part in parts):
            return "none"
        return private_olean_digest(*parts)
    except Exception as error:  # noqa: BLE001 - any failure to decode is a reportable outcome
        why = str(error) if isinstance(error, Undecodable) else type(error).__name__
        return "undecodable:" + why.replace(" ", "-")


def _digest_file(full):
    h = hashlib.sha256()
    with open(full, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    canonical = canonical_digest(full[:-len(PRIVATE)]) if full.endswith(PRIVATE) else None
    return full, h.hexdigest(), canonical


def manifest(root):
    """Return ({relative path: sha256}, {relative `.olean.private` path: canonical digest})."""
    files = []
    for dirpath, _dirnames, filenames in os.walk(root):
        files.extend(os.path.join(dirpath, name) for name in filenames if name.endswith(SUFFIXES))
    out, canonical = {}, {}
    # Decoding the private oleans dominates; a few workers keep it to minutes without much memory.
    workers = min(16, os.cpu_count() or 1)
    if workers > 1 and len(files) > 64 and "fork" in multiprocessing.get_all_start_methods():
        with multiprocessing.get_context("fork").Pool(workers) as pool:
            results = list(pool.imap_unordered(_digest_file, files, chunksize=32))
    else:
        results = [_digest_file(full) for full in files]
    for full, digest, decoded in results:
        rel = os.path.relpath(full, root)
        out[rel] = digest
        if decoded is not None:
            canonical[rel] = decoded
    return out, canonical


def read_manifest(path):
    out, canonical = {}, {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            digest, _, rel = line.partition("  ")
            if digest.startswith(CANONICAL):
                canonical[rel] = digest[len(CANONICAL):]
            else:
                out[rel] = digest
    return out, canonical


def do_manifest(build_dir, out_file):
    entries, canonical = manifest(build_dir)
    with open(out_file, "w", encoding="utf-8") as f:
        for rel in sorted(entries):
            f.write(f"{entries[rel]}  {rel}\n")
        for rel in sorted(canonical):
            f.write(f"{CANONICAL}{canonical[rel]}  {rel}\n")
    decoded = sum(1 for v in canonical.values() if "/" in v)
    failed = sum(1 for v in canonical.values() if v.startswith("undecodable"))
    print(f"recorded {len(entries)} artifacts from {build_dir} into {out_file} "
          f"({decoded} private oleans decoded, {failed} undecodable)")
    return 0


def module_name(rel):
    path = rel[:-len(PRIVATE)]
    if path.startswith("lib/lean/"):
        path = path[len("lib/lean/"):]
    return path.replace("/", ".")


def regular_file(path):
    return os.path.isfile(path) and not os.path.islink(path)


def explain_private(rel, clean_digest, build_dir):
    """Why the cached `rel` is benign, or None and the reason it is not."""
    full = os.path.join(build_dir, rel)
    stem = full[:-len(PRIVATE)]
    trace = stem + ".trace"
    inside = os.path.realpath(build_dir) + os.sep
    for path in (stem + ".olean", stem + ".olean.server", full, trace):
        if not regular_file(path) or not os.path.realpath(path).startswith(inside):
            return None, f"{os.path.relpath(path, build_dir)} is missing or not a regular file"
    try:
        with open(full, "rb") as f:
            actual = f"{lake_hash(f.read()):016x}"
        with open(trace, encoding="utf-8") as f:
            outputs = json.load(f)["outputs"]["o"]
        recorded = [o[:-len(PRIVATE)] for o in outputs
                    if isinstance(o, str) and o.endswith(PRIVATE)]
        hash_file = full + ".hash"
        sidecar = None
        if regular_file(hash_file):
            with open(hash_file, encoding="utf-8") as f:
                sidecar = f.read().strip()
    except (OSError, ValueError, KeyError, TypeError) as error:
        return None, f"its cached outputs or Lake trace are unreadable ({type(error).__name__})"
    if recorded != [actual]:
        return None, f"the cached file hashes to {actual}, but its Lake trace records {recorded}"
    if sidecar is not None and sidecar != actual:
        return None, "the cached file does not match its .olean.private.hash"
    if clean_digest is None or "/" not in clean_digest:
        return None, f"the clean build's copy could not be decoded ({clean_digest or 'not recorded'})"
    cached_digest = canonical_digest(stem)
    if "/" not in cached_digest:
        return None, f"the cached copy could not be decoded ({cached_digest})"
    if cached_digest != clean_digest:
        return None, "the two copies differ in more than compiler-internal `_uniq` names"
    count = clean_digest.split("/", 1)[1]
    return (f"{module_name(rel)}: its .olean.private differs from the clean build's only in "
            f"compiler-internal `_uniq` names ({count} of them); every other output matches, and "
            "the cached file matches its Lake trace"), None


def do_compare(fresh_path, reference_path, reference_build=None):
    (fresh, fresh_canonical), (reference, _) = read_manifest(fresh_path), read_manifest(reference_path)

    only_fresh = sorted(set(fresh) - set(reference))
    only_reference = sorted(set(reference) - set(fresh))
    differing = sorted(k for k in set(fresh) & set(reference) if fresh[k] != reference[k])

    benign, unexplained = [], []
    if (fresh and reference and not only_fresh and not only_reference and differing
            and all(rel.endswith(PRIVATE) for rel in differing)):
        for rel in differing:
            if reference_build is None:
                unexplained.append((rel, "no cache-restored build to check it against"))
                continue
            try:
                why, failure = explain_private(rel, fresh_canonical.get(rel), reference_build)
            except Exception as error:  # noqa: BLE001 - never let one file stop the report
                why, failure = None, f"it could not be checked ({type(error).__name__})"
            if failure is None:
                benign.append((rel, why))
            else:
                unexplained.append((rel, failure))

    print(f"clean-build artifacts:  {len(fresh)}")
    print(f"cached-build artifacts: {len(reference)}")
    print(f"only in clean:          {len(only_fresh)}")
    print(f"only in cached:         {len(only_reference)}")
    print(f"differing bytes:        {len(differing)}")
    print(f"benign private-olean nondeterminism: {len(benign)}")

    if not fresh or not reference:
        status = "inconclusive"
        detail = f"nothing to compare (clean {len(fresh)}, cached {len(reference)})"
    elif only_fresh or only_reference or (differing and len(benign) != len(differing)):
        status = "disagree"
        detail = (f"{len(differing) - len(benign)} differing, {len(only_fresh)} only in the clean build, "
                  f"{len(only_reference)} only in the cached build")
        if benign:
            detail += f", {len(benign)} benign private-olean nondeterminism"
    elif benign:
        status = "agree"
        modules = ", ".join(module_name(rel) for rel, _ in benign[:5])
        more = f" and {len(benign) - 5} more" if len(benign) > 5 else ""
        detail = (f"{len(fresh) - len(benign)} artifacts identical, {len(benign)} benign private-olean "
                  f"nondeterminism ({modules}{more})")
    else:
        status = "agree"
        detail = f"{len(fresh)} artifacts identical"

    explained = {rel for rel, _ in benign}
    for label, items in (("differing", [rel for rel in differing if rel not in explained]),
                         ("only in the clean build", only_fresh),
                         ("only in the cached build", only_reference)):
        for path in items[:10]:
            print(f"  {label}: {path}")
        if len(items) > 10:
            print(f"  ... and {len(items) - 10} more {label}")
    for rel, failure in unexplained[:10]:
        print(f"  not benign: {rel}: {failure}")
    for _rel, why in benign:
        print(f"  benign private-olean nondeterminism: {why}")
        print(f"::notice title=Benign private-olean nondeterminism::{why}")

    print(f"status: {status} ({detail})")
    step_output = os.environ.get("GITHUB_OUTPUT")
    if step_output:
        with open(step_output, "a", encoding="utf-8") as f:
            f.write(f"status={status}\n")
            f.write(f"detail={detail}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and benign:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### Benign private-olean nondeterminism\n\n")
            f.writelines(f"- {why}\n" for _rel, why in benign)
    return 0


def main(argv):
    if len(argv) == 4 and argv[1] == "manifest":
        return do_manifest(argv[2], argv[3])
    if len(argv) in (4, 5) and argv[1] == "compare":
        return do_compare(argv[2], argv[3], argv[4] if len(argv) == 5 else None)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
