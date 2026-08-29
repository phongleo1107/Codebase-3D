"""Suite-wide guarantees that hold for every test.

docs/SECURITY.md requires that no security test touches the network. That is
enforced here rather than trusted: the socket entry points are replaced for the
whole session, so a test that forgets to stub `getaddrinfo` fails loudly
instead of quietly resolving a real name (or quietly passing on a machine with
no DNS). Tests that need a resolver stub theirs on top of this; because the
block is installed once at session scope, a per-test `monkeypatch.setattr`
undo restores the *block*, not the real socket module.

The one gap is collection time: fixtures do not run during import, so a module
that called out to the network at import would not be caught. Nothing in this
suite does, and a test-time call is the shape this is guarding against.
"""

import socket
from collections.abc import Iterator
from typing import NoReturn

import pytest


class NetworkAccessAttempted(RuntimeError):
    """A test reached for the network. Stub the call instead."""


def _blocked(*args: object, **kwargs: object) -> NoReturn:
    raise NetworkAccessAttempted(
        "tests must not touch the network; monkeypatch the call under test"
    )


@pytest.fixture(scope="session", autouse=True)
def _no_network() -> Iterator[None]:
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(socket, "getaddrinfo", _blocked)
        patcher.setattr(socket, "create_connection", _blocked)
        patcher.setattr(socket, "gethostbyname", _blocked)
        patcher.setattr(socket.socket, "connect", _blocked)
        patcher.setattr(socket.socket, "connect_ex", _blocked)
        yield
