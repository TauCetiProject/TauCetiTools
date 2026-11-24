#!/usr/bin/env python3
"""Prepare a fresh private TauCeti build, then reconcile satisfied cache records.

Temporary workaround for https://github.com/leanprover/lean4/pull/15868.
Native Lake remains responsible for input/output validity and publication. Retain
the original archive at each module's `ltarFile` so the final native output map reuses it
without repacking. Run inside the offline PR sandbox before library code.
"""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time

HASH = re.compile(r"[0-9a-f]{16}")
ARCHIVE = re.compile(r"[0-9a-f]{16}\.ltar")
SCHEMA = "2026-02-25"
MODULE = re.compile(r"TauCeti(?:\.[A-Za-z_][A-Za-z0-9_']*)*")
OUTPUTS = (("lib", "lib/lean", ("trace", "olean", "olean.server", "olean.private", "ilean", "ir", "ir.sig")),
           ("ir", "ir", ("c", "bc")))


def regular(path):
    """Return a regular file path; reject symlinks and special files via lstat."""
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("expected an ordinary file: " + str(path))
    return path


def layout(project):
    """Reject project/library paths outside the supported TauCeti default layout."""
    import tomllib
    config = tomllib.loads(regular(project / "lakefile.toml").read_text())
    libs = config.get("lean_lib", [])
    if ((project / "lakefile.lean").exists() or config.get("name") != "TauCeti"
            or any(config.get(key, default) != default for key, default in (
                ("buildDir", ".lake/build"), ("leanLibDir", "lib/lean"),
                ("irDir", "ir"), ("srcDir", ".")))
            or not isinstance(libs, list) or len(libs) != 1
            or not isinstance(libs[0], dict) or libs[0].get("name") != "TauCeti"
            or libs[0].get("srcDir", ".") != "."):
        raise ValueError("unsupported Lake layout")


HASHER = Path(__file__).resolve().parent / "CacheHash.lean"
# Lake's module trace output keys (`ModuleOutputDescrs.toJson`) and their extensions.
DESCRIPTORS = {"o": ("olean", "olean.server", "olean.private"), "i": ("ilean",),
               "rs": ("ir.sig",), "r": ("ir",), "c": ("c",), "b": ("bc",)}
PREFERRED = "repair-revisions"
UNRANKED = 1 << 30
# The oldest ilean format whose "module" field this script relies on.
ILEAN_VERSION = 5


def preferred_inputs(project):
    """Rank input hashes by the candidate revisions repair-cache-get.sh fetched.

    ``.lake/cache/repair-revisions`` lists those revisions, the candidate's own
    tip first; each names a native revision map ``revisions/TauCeti/<rev>.jsonl``
    of ``[input hash, archive]`` rows. A record from an earlier revision ranks
    first. Without the list every record ranks equally, as before.
    """
    cache = project / ".lake/cache"
    try:
        revisions = (cache / PREFERRED).read_text().split()
    except OSError:
        return {}
    ranks = {}
    for rank, revision in enumerate(revisions):
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            continue
        try:
            lines = regular(cache / "revisions/TauCeti" / (revision + ".jsonl")).read_text().splitlines()
        except (OSError, ValueError):
            continue
        for line in lines[1:]:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, list) and row and isinstance(row[0], str) and HASH.fullmatch(row[0]):
                ranks.setdefault(row[0], rank)
    return ranks


def selected_records(project):
    """Read native archive-only selections and retain original mapping bytes.

    Reject unsupported schemas and unsafe archive names. Prepare one selection
    for each archive and module; every alternative mapping stays available to Lake.
    This reads existing native selections; it does not select another ancestor.
    """
    outputs = project / ".lake/cache/outputs/TauCeti"
    artifacts = project / ".lake/cache/artifacts"
    records, archives = [], set()
    # An archive carries no input hash: extraction stamps the selected record's
    # hash into the trace, and Lake restores the module again, one leantar per
    # module, when that hash is not the current input. Archives are shared by
    # every input whose outputs are byte-identical, so the record chosen for a
    # shared archive matters as much as the version chosen for a module. Prefer
    # the candidate revisions' own records, the candidate's tip first, then the
    # base cache's records, which arrive first, in arrival order.
    ranks = preferred_inputs(project)
    paths = sorted(outputs.glob("*.json"),
                   key=lambda path: (ranks.get(path.stem, UNRANKED), path.stat().st_mtime_ns, path.name))
    for path in paths:
        if not HASH.fullmatch(path.stem):
            raise ValueError("unexpected cache record")
        original = regular(path).read_bytes()
        value = json.loads(original)
        if not isinstance(value, dict) or value.get("schemaVersion") != SCHEMA:
            raise ValueError("unknown cache schema")
        archive = value.get("data")
        if not isinstance(archive, str) or not ARCHIVE.fullmatch(archive):
            raise ValueError("cache is not an archive-only revision")
        if archive in archives:
            continue
        archives.add(archive)
        records.append(dict(file=str(regular(artifacts / archive)), hash=path.stem,
                            archive=archive, path=path, original=original))
    return records


def module_paths(module):
    """Return permitted build-relative outputs for a supported TauCeti module."""
    if not isinstance(module, str) or not MODULE.fullmatch(module):
        raise ValueError("unknown module identity")
    parts = module.split(".")
    return {str(Path(destination, *parts[:-1], parts[-1] + "." + ext))
            for _, destination, extensions in OUTPUTS for ext in extensions}


def source_present(project, module):
    """Check that the candidate has a regular module source without symlink parents."""
    source = project.joinpath(*module.split(".")).with_suffix(".lean")
    # Source symlinks are never evidence that this module belongs to the candidate.
    for parent in source.parents:
        if parent == project:
            break
        if parent.is_symlink():
            return False
    return source.is_file() and not source.is_symlink()


def raise_stack_limit():
    """leantar 0.1.21 overflows the default 8 MiB stack on a large olean; lift it to the hard limit."""
    try:
        import resource
        _, hard = resource.getrlimit(resource.RLIMIT_STACK)
        resource.setrlimit(resource.RLIMIT_STACK, (hard, hard))
    except (ImportError, OSError, ValueError):
        pass


def extract_records(records, staging):
    """One native extraction for all selected archives, never into a live build."""
    if not records:
        return []
    requests = []
    for record in records:
        base = staging / record["hash"]
        requests.append(dict(file=record["file"], hash=record["hash"],
                             base=[str(base / "lib"), str(base / "ir")]))
    sysroot = subprocess.check_output(["lean", "--print-prefix"], text=True).strip()
    leantar = Path(sysroot) / "bin/leantar"
    env = dict(os.environ)
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    env.setdefault("RAYON_NUM_THREADS", str(min(16, cpus)))
    # The rlimit raised below sizes only leantar's main thread. A `-j` batch unpacks on Rust worker
    # threads, whose stacks default to 2 MiB whatever the rlimit: on Lean v4.37.0-rc1 (leantar
    # 0.1.21) every staging run's batch aborted with "has overflowed its stack" (23 Nov 2026), and
    # Lake then restored the 39,000 modules one process each. RUST_MIN_STACK sizes those threads;
    # it reserves address space, and only touched pages use memory (TauCetiWorker does the same).
    env.setdefault("RUST_MIN_STACK", str(256 * 1024 * 1024))
    print(f"bulk-unpack-cache: restoring {len(records)} archives in one invocation", flush=True)
    subprocess.run([str(leantar), "-x", "-j", "-"], input=json.dumps(requests), text=True, check=True, env=env,
                   preexec_fn=raise_stack_limit)
    extracted = []
    for record in records:
        base = staging / record["hash"]
        for directory in ("lib", "ir"):
            if (base / directory).is_symlink() or not (base / directory).is_dir():
                raise ValueError("unexpected archive directory")
        (ilean,) = (base / "lib").glob("*.ilean")
        info = json.loads(regular(ilean).read_text())
        module = info.get("module")
        allowed = module_paths(module)
        # Only the ilean's "module" field is read. Lean bumps the ilean version when other
        # fields change (5 through v4.36, 6 from v4.37.0-rc1) and keeps "module" the module
        # name; an exact version check sent every rc1 build to stock Lake's per-module
        # restore. module_paths and the file name below still validate the name itself.
        version = info.get("version")
        if (not isinstance(version, int) or isinstance(version, bool) or version < ILEAN_VERSION
                or ilean.name != module.split(".")[-1] + ".ilean"):
            raise ValueError("unknown archive module layout")
        files = []
        for directory, destination, _ in OUTPUTS:
            for source in (base / directory).iterdir():
                relative = str(Path(destination, *module.split(".")[:-1], source.name))
                if relative not in allowed:
                    raise ValueError("unexpected archive output")
                files.append((regular(source), relative))
        extracted.append((record, module, files))
    return extracted


def expanded_artifacts(record, module, files):
    """Name each restored output by the content hash its archive's own trace records.

    Lake's writable artifact cache first resolves a module's trace outputs against
    `.lake/cache/artifacts`. When they are absent, Lake reads, hashes and caches
    every output of every restored module itself, which dominates a fully cached
    build. Return `(extracted file, build-relative path, artifact name)` triples, or
    nothing when the trace and the outputs do not correspond one to one in the
    shape Lake writes for the trace's module mode.
    """
    parts = module.split(".")
    stem = Path(*parts[:-1], parts[-1])
    by_relative = {relative: source for source, relative in files}
    trace_relative = str(Path("lib/lean", stem)) + ".trace"
    try:
        trace = json.loads(regular(by_relative[trace_relative]).read_text())
    except (KeyError, OSError, ValueError):
        return []
    outputs = trace.get("outputs") if isinstance(trace, dict) else None
    if (not isinstance(outputs, dict) or trace.get("depHash") != record["hash"]
            or not set(outputs) <= set(DESCRIPTORS) | {"m", "l"}
            or outputs.get("l", record["archive"]) != record["archive"]):
        return []
    # Lake reads the module mode from "m" and the IR from "c" alone. Accept only the
    # complete shapes it caches for compiled modules: a module-system build has
    # three olean parts, the IR signature, IR and C; any other has one olean and C.
    # Postponed compilation and anything else stays with stock Lake.
    module_system = outputs.get("m")
    if (not isinstance(module_system, bool) or not isinstance(outputs.get("o"), list)
            or len(outputs["o"]) != (3 if module_system else 1)
            or set(outputs) - {"b", "l"} != ({"m", "o", "i", "rs", "r", "c"} if module_system
                                             else {"m", "o", "i", "c"})):
        return []
    named = {}
    for key, extensions in DESCRIPTORS.items():
        if key not in outputs:
            continue
        values = outputs[key] if key == "o" else [outputs[key]]
        if not isinstance(values, list) or len(values) > len(extensions):
            return []
        for extension, name in zip(extensions, values):
            if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{16}\." + re.escape(extension), name):
                return []
            directory = "ir" if extension in ("c", "bc") else "lib/lean"
            named[str(Path(directory, stem)) + "." + extension] = name
    if set(named) != set(by_relative) - {trace_relative}:
        return []
    return [(by_relative[relative], relative, name) for relative, name in sorted(named.items())]


def verified(groups, staging):
    """Return the indices of the modules whose every output hashes to its name.

    Stock Lake names cached outputs by hashing them. Hash the same bytes with the
    toolchain's own Lake hash (CacheHash.lean, as TauCetiWorker does) so a cache
    entry never names other bytes. Return None when the helper fails.
    """
    paths = [str(source) for group in groups for source, _, _ in group]
    if not paths:
        return set()
    helpers = []
    try:
        lean = Path(subprocess.check_output(["lean", "--print-prefix"], text=True).strip()) / "bin/lean"
        cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
        for index in range(min(16, cpus, len(paths))):
            request = staging / f"hash-request-{index}.json"
            request.write_text(json.dumps(paths[index::min(16, cpus, len(paths))]))
            helpers.append(subprocess.Popen([str(lean), "--run", str(HASHER), str(request)],
                                            stdout=subprocess.PIPE, text=True))
        hashes = {}
        for helper in helpers:
            output, _ = helper.communicate(timeout=1800)
            if helper.returncode != 0:
                raise subprocess.CalledProcessError(helper.returncode, "CacheHash.lean")
            for line in output.splitlines():
                path, value = json.loads(line)
                hashes[path] = f"{value:016x}"
        if len(hashes) != len(paths):
            raise ValueError("incomplete artifact hashes")
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        for helper in helpers:
            helper.kill()
            helper.wait()
        print(f"::warning::bulk-unpack-cache: cannot verify expanded artifacts ({error}); "
              "Lake will cache every module itself", flush=True)
        return None
    return {index for index, group in enumerate(groups)
            if group and all(hashes[str(source)] == name.split(".", 1)[0] for source, _, name in group)}


def install(project, expanded):
    """Hard-link restored outputs into the artifact cache under their trace names.

    Lake's own `cacheArtifact` makes both names read-only and keeps an existing
    cache entry. A failure leaves the remaining modules to stock Lake.
    """
    build, artifacts = project / ".lake/build", project / ".lake/cache/artifacts"
    for relative, name in expanded:
        os.chmod(build / relative, 0o444)
        try:
            os.link(build / relative, artifacts / name)
        except FileExistsError:
            os.chmod(artifacts / name, 0o444)


def assemble(project, records, staging):
    """Validate one native bulk extraction and assemble a private build tree."""
    extracted = extract_records(records, staging / "unpacked")
    combined = staging / "build"
    combined.mkdir()
    modules, candidates = set(), []
    for record, module, files in extracted:
        # Prepare one version, retaining every alternative mapping. A different
        # current input still hits its original record and Lake restores that
        # version normally; no extra compilation or guessed validity is required.
        if module in modules:
            continue
        modules.add(module)
        if source_present(project, module):
            candidates.append((record, module, files, expanded_artifacts(record, module, files)))
    # None installs nothing and keeps every module, exactly as before expansion.
    accepted = verified([group for *_, group in candidates], staging) or None
    targets, satisfied, archives, plans, expanded = set(), [], [], [], []
    for index, (record, module, files, group) in enumerate(candidates):
        # Once any module's names are installed, a module that failed verification
        # could resolve through them; leave its mapping to stock Lake's restoration,
        # which hashes what it extracts.
        if accepted is not None and index not in accepted:
            continue
        for source, relative in files:
            if relative in targets:
                raise ValueError("overlapping module output")
            targets.add(relative)
            plans.append((source, combined / relative))
        # Lake's `Module.ltarFile` is the IR path, `ir/<module>.ltar`.
        archive_target = combined / "ir" / Path(*module.split(".")).with_suffix(".ltar")
        archives.append((Path(record["file"]), archive_target))
        satisfied.append(record)
        if accepted is not None:
            expanded.extend((relative, name) for _, relative, name in group)
    # Validate all modules before publishing or changing any selected mappings.
    for source, target in plans:
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
    for source, target in archives:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(source, target)
        if accepted is not None:
            os.chmod(target, 0o444)  # Lake's own caching makes the archive read-only too.
    return combined, satisfied, expanded, dict(
        bulk_archives=len(records), satisfied_records=len(satisfied),
        historical_archives=len(extracted) - len(modules), expanded_artifacts=len(expanded),
        unverified_modules=0 if accepted is None else len(candidates) - len(accepted))


def publish(project, combined, satisfied, expanded=()):
    """Publish a complete private tree, install its expanded artifacts, then remove
    only satisfied mappings.

    Recheck captured mapping bytes before the atomic rename. Archive bytes and
    publication snapshots remain untouched. If a later step fails, the complete
    tree stays published and remaining mappings permit native Lake restoration;
    mapping cleanup is not a single atomic transaction with tree publication.
    """
    # Recheck originals before making any live change. Snapshot/reconnect data
    # and every archive remain intact; only proven satisfied mappings may leave.
    for record in satisfied:
        if regular(record["path"]).read_bytes() != record["original"]:
            raise ValueError("selected cache record changed during preparation")
    combined.rename(project / ".lake/build")
    install(project, expanded)
    for record in satisfied:
        record["path"].unlink()


def unpack(project: Path) -> int:
    started = time.monotonic()
    project = project.resolve()
    build = project / ".lake/build"
    outputs = project / ".lake/cache/outputs/TauCeti"
    if build.exists() or build.is_symlink() or not outputs.is_dir():
        print("bulk-unpack-cache: no fresh cache; using stock Lake restoration")
        return 0
    try:
        layout(project)
        records = selected_records(project)
        if not records:
            return 0
        with tempfile.TemporaryDirectory(prefix="bulk-unpack-", dir=project / ".lake") as temporary:
            combined, satisfied, expanded, result = assemble(project, records, Path(temporary))
            if not satisfied:
                print("bulk-unpack-cache: no unambiguous current sources; using stock Lake restoration")
                return 0
            publish(project, combined, satisfied, expanded)
        result["seconds"] = time.monotonic() - started
        print("bulk-unpack-cache: " + json.dumps(result, sort_keys=True), flush=True)
        return len(satisfied)
    except (ImportError, OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError) as error:
        print(f"::warning::bulk-unpack-cache: {error}; using stock Lake restoration")
        return 0


def main(project: Path) -> None:
    try:
        expected = project.resolve() / ".lake/cache"
        configured = os.environ.get("LAKE_CACHE_DIR", "")
        if (not configured or Path(configured).resolve() != expected
                or os.environ.get("LAKE_ARTIFACT_CACHE") != "true"
                or os.environ.get("LAKE_RESTORE_ARTIFACTS") != "true"):
            print("bulk-unpack-cache: private CI cache not enabled; using stock Lake restoration")
        else:
            unpack(project)
    except Exception as error:
        print(f"::warning::bulk-unpack-cache: {error}; using stock Lake restoration")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path, nargs="?", default=Path.cwd())
    args = parser.parse_args()
    main(args.project)
