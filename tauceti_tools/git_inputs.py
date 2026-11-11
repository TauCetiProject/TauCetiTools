"""Bounded Git transport and reusable exact-commit snapshots for source consumers."""

import contextlib
import fcntl
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading

# Blob-id abbreviation on `index` lines, as GitHub renders them for TauCeti today. Cosmetic: the
# patch digest normalises these ids away.
INDEX_ABBREV = 11
# Hard cap on the diff (and on the NUL-separated path list). Real large PRs are far below it: the
# 909-file mathlib4 bump #3780 is about 6 MB, the 338-file mathlib4 #26077 0.36 MB, TauCeti's
# biggest diffs well under 1 MB.
MAX_BYTES = 64 << 20
# Hard cap on everything downloaded into the throwaway repository (trees plus the changed files'
# contents), enforced while git runs and checked again after. mathlib4 #3780 (909 files) needs
# about 6 MB; TauCeti's trees are about 0.4 MB and its largest tracked file under 0.3 MB.
MAX_FETCH_BYTES = 256 << 20
FETCH_TIMEOUT = 300  # seconds, per git call
DIFF_TIMEOUT = 300
# The only variables git sees. Everything else (GH_TOKEN, GITHUB_TOKEN, provider API keys,
# GIT_ASKPASS, SSH_ASKPASS, GIT_SSH*, GIT_CONFIG_*, GIT_DIR, ...) is dropped.
ENV_ALLOW = (
    "PATH",
    "HOME",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "LANG",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "GIT_EXEC_PATH",
)

PREPARATION_WAIT_EXIT = 83

# The parent's pipe owns this process group, including remote helpers. Unlike a
# timer in the CLI, this guardian also observes SIGKILL/owner death on EOF.
_GIT_OWNER = """import os,signal,subprocess,sys,threading
fd=int(sys.argv[1])
def owner_gone():
 while os.read(fd,1): pass
 os.killpg(os.getpgrp(),signal.SIGKILL)
threading.Thread(target=owner_gone,daemon=True).start()
child=subprocess.Popen(sys.argv[2:])
rc=child.wait()
os._exit(rc if rc>=0 else 128-rc)
"""


class PreparationUnavailable(RuntimeError):
    """A transport or host outage before inference; the fleet retries uncharged."""


def transport_unavailable(message):
    return bool(
        re.search(
            r"HTTP (?:401|403|408|429|5\d\d)|(?:returned error:|error:)[ ]*(?:401|403|408|429|5\d\d)\b"
            r"|could not resolve|couldn't connect|failed to connect|connection (?:reset|refused|timed out)"
            r"|timed out|TLS|SSL|authentication failed|could not read Username|rate limit|network is unreachable"
            r"|RPC failed|early EOF|remote end hung up|unexpected disconnect|HTTP/2.*stream"
            r"|error connecting to|i/o timeout|unexpected EOF|context deadline exceeded|operation too slow"
            r"|no space left on device|disk quota exceeded|too many open files|input/output error",
            message,
            re.I,
        )
    )


def _git_env():
    """Allowlisted environment for git: no credentials, no user/system config, no prompts."""
    env = {k: v for k, v in os.environ.items() if k in ENV_ALLOW}
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "LC_ALL": "C",
        }
    )
    return env


def host_resource_unavailable(message):
    return bool(
        re.search(r"no space left on device|disk quota exceeded|too many open files|input/output error", message, re.I)
    )


def _killpg(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass


def _du(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass  # a temporary pack renamed or removed under us
    return total


def _git(d, args, env, timeout, sink=None, limit=MAX_BYTES, stdin=b"", disk_cap=None, config=()):
    """Run `git -C d args` with `stdin`, streaming stdout into `sink` (a binary file) up to `limit`
    bytes, killing git's whole process group at `timeout` or, when `disk_cap` is given, as soon as
    the repository directory `d` grows past it (checked every 0.1 s while git runs, so a fast
    download can overshoot slightly before the kill, and once more after it exits). Returns stdout bytes when `sink` is None. Raises
    RuntimeError on failure, timeout, or overflow."""
    name = args[0]
    with tempfile.TemporaryFile() as err, tempfile.TemporaryFile() as inp:
        inp.write(stdin)
        inp.seek(0)
        reader, owner = os.pipe()
        try:
            p = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _GIT_OWNER,
                    str(reader),
                    "git",
                    "-c",
                    "maintenance.autoDetach=false",
                    "-c",
                    "gc.autoDetach=false",
                    *[a for value in config for a in ("-c", value)],
                    "-C",
                    d,
                    *args,
                ],
                stdin=inp,
                stdout=subprocess.PIPE,
                stderr=err,
                env=env,
                start_new_session=True,
                pass_fds=(reader,),
            )
        except OSError as e:
            os.close(owner)
            raise RuntimeError(f"cannot run git: {e}") from e
        finally:
            os.close(reader)
        timed_out, too_big, done = threading.Event(), threading.Event(), threading.Event()
        lock = threading.Lock()

        def kill(reason):
            # Only while git is unreaped (`done` is set before the reaping wait), so a recycled PID
            # is never signalled.
            with lock:
                if not done.is_set():
                    reason.set()
                    _killpg(p)

        def watch_disk():
            while not done.wait(0.1):
                if _du(d) > disk_cap:
                    kill(too_big)
                    return

        timer = threading.Timer(timeout, kill, args=(timed_out,)) if timeout is not None else None
        if timer:
            timer.start()
        watcher = threading.Thread(target=watch_disk, daemon=True)
        if disk_cap is not None:
            watcher.start()
        out, total = [], 0
        try:
            while True:
                chunk = p.stdout.read(1 << 16)
                if not chunk:
                    break
                total += len(chunk)
                if limit is not None and total > limit:
                    raise RuntimeError(f"git {name} output exceeds the {limit}-byte limit")
                if sink is None:
                    out.append(chunk)
                else:
                    sink.write(chunk)
            # Wait for git to exit WITHOUT reaping it, so the timer and the watcher can still
            # kill it safely until `done` is set below.
            os.waitid(os.P_PID, p.pid, os.WEXITED | os.WNOWAIT)
        except BaseException:
            kill(threading.Event())
            raise
        finally:
            with lock:
                done.set()
            if timer:
                timer.cancel()
            if watcher.is_alive():
                watcher.join()
            p.wait()
            p.stdout.close()
            os.close(owner)
        if disk_cap is not None and _du(d) > disk_cap:
            too_big.set()  # grew past the cap between two checks
        if too_big.is_set():
            raise RuntimeError(f"git {name}: the objects downloaded exceed the {disk_cap}-byte limit")
        if timed_out.is_set():
            error = PreparationUnavailable if name == "fetch" else RuntimeError
            raise error(f"git {name} timed out after {timeout}s")
        if p.returncode != 0:
            err.seek(0)
            msg = err.read()[-2000:].decode("utf-8", "replace").strip()
            error = (
                PreparationUnavailable
                if host_resource_unavailable(msg) or name == "fetch" and transport_unavailable(msg)
                else RuntimeError
            )
            raise error(f"git {name} failed ({p.returncode}): {msg}")
        return b"".join(out)


@contextlib.contextmanager
def snapshot(
    remote,
    base,
    head,
    *,
    run_git=None,
    environment=None,
    cache_dir=None,
    parent=None,
    complete=False,
    fetch_limit=MAX_FETCH_BYTES,
):
    """Fetch both endpoints together; keep protocol/credential policy with the caller."""
    run_git = _git if run_git is None else run_git
    env = _git_env() if environment is None else environment
    with contextlib.ExitStack() as lifetime:
        if cache_dir is None:
            directory = lifetime.enter_context(tempfile.TemporaryDirectory(prefix="pr-diff-", dir=parent))
        else:
            os.makedirs(cache_dir, mode=0o700, exist_ok=True)
            lock = lifetime.enter_context(open(os.fspath(cache_dir) + ".lock", "a"))
            fcntl.flock(lock, fcntl.LOCK_EX)
            directory = os.fspath(cache_dir)
        run_git(directory, ["init", "-q", "--bare"], env, FETCH_TIMEOUT)
        run_git(directory, ["config", "remote.origin.url", remote], env, FETCH_TIMEOUT)
        refs = (
            [base, head]
            if cache_dir is None
            else [f"+{base}:refs/tauceti/review/base", f"+{head}:refs/tauceti/review/head"]
        )
        run_git(
            directory,
            [
                "fetch",
                "-q",
                "--no-tags",
                "--depth",
                "1",
                *([] if complete else ["--filter=blob:none"]),
                "origin",
                *refs,
            ],
            env,
            None if complete else FETCH_TIMEOUT,
            disk_cap=fetch_limit,
            config=("http.lowSpeedLimit=1", f"http.lowSpeedTime={FETCH_TIMEOUT}") if complete else (),
        )
        yield directory, env


def _changed_blobs(raw):
    """Blob ids on either side of a `git diff --raw -z --no-renames` listing (no submodules)."""
    fields = raw.split(b"\0")
    blobs = set()
    for meta in fields[0::2]:
        if not meta.startswith(b":"):
            continue
        old_mode, new_mode, old, new = meta[1:].split(b" ")[:4]
        for mode, oid in ((old_mode, old), (new_mode, new)):
            if mode != b"160000" and oid.strip(b"0"):
                blobs.add(oid)
    return sorted(blobs)


def fetch_changed_blobs(
    directory, base, head, environment, *, run_git=None, limit=MAX_BYTES, fetch_limit=MAX_FETCH_BYTES
):
    run_git = _git if run_git is None else run_git
    local = {**environment, "GIT_NO_LAZY_FETCH": "1"}
    raw = run_git(
        directory,
        ["diff", "--raw", "-z", "--no-renames", "--abbrev=40", base, head, "--"],
        local,
        DIFF_TIMEOUT,
        limit=limit,
        disk_cap=fetch_limit,
    )
    blobs = _changed_blobs(raw)
    if blobs:
        run_git(
            directory,
            ["fetch", "-q", "--no-tags", "--no-write-fetch-head", "--stdin", "origin"],
            environment,
            FETCH_TIMEOUT,
            stdin=b"\n".join(blobs) + b"\n",
            disk_cap=fetch_limit,
        )


def changed_paths(remote, base, head, *, environment=None):
    with snapshot(remote, base, head, environment=environment) as (directory, env):
        fetch_changed_blobs(directory, base, head, env)
        raw = _git(
            directory,
            ["diff", "--name-only", "-z", "-M", base, head, "--"],
            {**env, "GIT_NO_LAZY_FETCH": "1"},
            DIFF_TIMEOUT,
        )
    return [os.fsdecode(path) for path in raw.split(b"\0") if path]
