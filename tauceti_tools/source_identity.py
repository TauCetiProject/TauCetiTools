"""Qualified source actors shared by forge clients and domain adapters.

Native IDs identify actors; display logins never confer authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .forge import Forge, Incomplete, PrincipalRef


def _login(row):
    if not isinstance(row, dict):
        raise Incomplete("source actor record is unavailable")
    login = row.get("login")
    if not isinstance(login, str) or not login or login.strip() != login or any(c in login for c in "\0\r\n"):
        raise Incomplete("source actor display login is unavailable")
    try:
        login.encode("utf-8")
    except UnicodeError:
        raise Incomplete("source actor display login is not valid UTF-8") from None
    return login


@dataclass(frozen=True)
class SourceActor:
    principal: PrincipalRef
    login: str = field(compare=False)

    def __post_init__(self):
        if (
            not isinstance(self.principal, PrincipalRef)
            or not isinstance(self.principal.forge, Forge)
            or not re.fullmatch(r"[1-9][0-9]*", self.principal.native_id)
        ):
            raise ValueError("source actor requires a canonical qualified native user ID")
        try:
            _login({"login": self.login})
        except Incomplete:
            raise ValueError("source actor requires a valid display login") from None

    @classmethod
    def from_native(cls, forge, row):
        if not isinstance(forge, Forge):
            raise Incomplete("source actor origin is unavailable")
        login = _login(row)
        if type(row.get("id")) is not int or row["id"] < 1:
            raise Incomplete("source actor has no authenticatable native identity")
        return cls(PrincipalRef(forge, str(row["id"])), login)

    @classmethod
    def from_projection(cls, forge, row):
        if not isinstance(forge, Forge):
            raise Incomplete("projected actor origin is unavailable")
        login = _login(row)
        identity = row.get("id")
        prefix = forge.key + "/users/"
        if (
            not isinstance(identity, str)
            or not identity.startswith(prefix)
            or not re.fullmatch(r"[1-9][0-9]*", identity[len(prefix) :])
        ):
            raise Incomplete("projected actor has no identity on the selected source")
        return cls(PrincipalRef(forge, identity[len(prefix) :]), login)
