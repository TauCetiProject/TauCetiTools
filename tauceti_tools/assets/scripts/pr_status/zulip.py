#!/usr/bin/env python3
"""Zulip transport and credential checks for automation alerts.

Callers configure ZULIP_CHANNEL and ZULIP_TOPIC before importing this module.
The client sends and updates alert messages; it has no PR feed or reaction sink.
"""

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

CHANNEL = os.environ.get("ZULIP_CHANNEL", "Tau Ceti")
TOPIC = os.environ.get("ZULIP_TOPIC", "Stuck PRs")


class ConfigError(RuntimeError):
    """A persistent auth/permission/config failure that will not self-heal:
    missing or empty creds, a bad API key (401), a forbidden bot (403), or the
    bot not being subscribed to the channel. Raised so the caller can surface it
    loudly (non-zero exit + ::error:: annotation) instead of the quiet cosmetic
    path, because it prevents every alert from being delivered."""


def log(msg):
    print(msg, flush=True)


def sleep_for(delay):
    """Sleep for all of `delay`, in chunks so one wait never exceeds 60 seconds."""
    remaining = max(0.1, delay)
    while remaining > 0:
        chunk = min(remaining, 60)
        time.sleep(chunk)
        remaining -= chunk


class Zulip:
    def __init__(self, email, api_key, site):
        self.base = site.rstrip("/") + "/api/v1"
        self.auth = "Basic " + base64.b64encode(f"{email}:{api_key}".encode()).decode()

    def _call(self, method, path, params, tolerate=(), retry=True):
        attempts = 6 if retry else 1
        for attempt in range(attempts):
            data = urllib.parse.urlencode(params).encode() if params else None
            url = self.base + path
            if method in ("GET", "DELETE") and data:
                url += "?" + data.decode()
                data = None
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", self.auth)
            if data:
                req.add_header("Content-Type", "application/x-www-form-urlencoded")
            try:
                with urllib.request.urlopen(req) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                payload = {}
                try:
                    payload = json.loads(e.read().decode())
                except Exception:
                    pass
                if payload.get("code") in tolerate:
                    return payload
                if attempt + 1 < attempts and (e.code == 429 or 500 <= e.code < 600):
                    if e.code == 429:
                        raw_delay = (
                            e.headers.get("Retry-After")
                            or payload.get("retry-after")
                            or 1
                        )
                        try:
                            delay = max(0.1, float(raw_delay))
                        except (TypeError, ValueError):
                            delay = 1
                        reason = "rate limit"
                    else:
                        delay = 2 ** attempt
                        reason = f"HTTP {e.code}"
                    log(f"Zulip {reason}; retrying in {delay:.1f}s")
                    sleep_for(delay)
                    continue
                detail = f"Zulip {method} {path} failed: {e.code} {payload or e.reason}"
                # A persistent creds/permission break (vs a one-off hiccup) is a
                # ConfigError, so main() fails loudly instead of swallowing it. 401
                # and Zulip's UNAUTHORIZED code (e.g. "Malformed API key") are always
                # auth. A 403 counts only when Zulip itself answered with a JSON body
                # (a real permission error); an opaque 403 from a WAF/proxy has no
                # payload and is treated as a transient hiccup, not a config break.
                if (e.code == 401
                        or payload.get("code") == "UNAUTHORIZED"
                        or (e.code == 403 and payload)):
                    raise ConfigError(detail)
                raise RuntimeError(detail)
            except urllib.error.URLError as e:
                if attempt + 1 < attempts:
                    delay = 2 ** attempt
                    log(f"Zulip network error; retrying in {delay:.1f}s: {e.reason}")
                    sleep_for(delay)
                    continue
                raise RuntimeError(f"Zulip {method} {path} failed: {e.reason}") from e

    def my_user_id(self):
        return self._call("GET", "/users/me", None)["user_id"]

    def my_subscriptions(self):
        return [s["name"] for s in
                self._call("GET", "/users/me/subscriptions", None)["subscriptions"]]

    def get_message_page(self, narrow, anchor="newest", num_before=1000):
        params = {
            "anchor": anchor, "num_before": num_before, "num_after": 0,
            "apply_markdown": "false",  # raw markdown, so content compares exactly
            "narrow": json.dumps(narrow),
        }
        return self._call("GET", "/messages", params)

    def get_messages(self, narrow, num_before=1000):
        return self.get_message_page(narrow, num_before=num_before)["messages"]

    def send_message(self, content):
        # Sending is not idempotent: if Zulip accepts the request but the
        # response is lost, retrying would create a permanent duplicate post.
        r = self._call("POST", "/messages", {
            "type": "stream", "to": CHANNEL, "topic": TOPIC, "content": content,
        }, retry=False)
        return r["id"]

    def update_message(self, message_id, content):
        self._call("PATCH", f"/messages/{message_id}", {"content": content})


def check(z):
    """Health probe: authenticate and confirm the bot can act in the channel.

    A bad key raises ConfigError from `my_user_id` (the 401 path); a bot that is
    not subscribed to the channel can authenticate but cannot post or react, so
    that is treated as a config failure too. Touches no PR and no message."""
    bot_id = z.my_user_id()
    subs = z.my_subscriptions()
    if CHANNEL not in subs:
        raise ConfigError(
            f"bot (user {bot_id}) is not subscribed to channel {CHANNEL!r}; "
            f"it cannot post or react there. Subscribe it and re-run.")
    log(f"OK: authenticated as user {bot_id}, subscribed to {CHANNEL!r}")


def fail_config(msg):
    """Report a persistent config break loudly: a GitHub Actions error
    annotation (visible in the run summary even under `continue-on-error`) plus a
    non-zero exit so the workflow run goes red. See the module docstring."""
    log(f"CONFIG ERROR: {msg}")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        # Escape the workflow-command payload so a %, CR, or LF in the error text
        # (e.g. a Zulip JSON body) can't malform or truncate the annotation.
        safe = msg.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error title=Zulip alert integration is broken::{safe}", flush=True)
    return 1
