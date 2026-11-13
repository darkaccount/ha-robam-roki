"""Declarative command execution for the ROBAM / ROKI integration.

The executor only knows how to send a command: a property write, a vendor
function call or one raw frame.  Everything that is policy -- which command to
send, optimistic state, countdown deadlines, gear re-asserts -- stays in the
coordinator.

This module must not import Home Assistant (nor the devices package at
runtime): it is pure enough to be exercised directly by tests and the CLI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # avoid the devices -> coordinator -> commands cycle
    from .devices.base import CommandSpec


class UnverifiedCommandError(ValueError):
    """An unverified CommandSpec was executed without the opt-in option."""


@runtime_checkable
class Transport(Protocol):
    """How one device accepts the three kinds of write."""

    async def async_set_property(self, key: str, value: Any) -> None:
        """Write one device property (the cloud property API)."""

    async def async_invoke(self, function: str, params: dict[str, Any]) -> None:
        """Call a vendor function with its parameter map."""

    async def async_send_frame(self, command: int, body: bytes) -> None:
        """Send one raw protocol frame (the MQTT transport)."""


async def execute(
    spec: CommandSpec,
    transport: Transport,
    *,
    allow_unverified: bool = False,
    **kwargs: Any,
) -> None:
    """Send one CommandSpec through the given transport.

    kind=property writes the on/off payload (the caller passes on=True/False);
    kind=invoke calls spec.function and fills spec.param from the keyword
    arguments plus spec.params; kind=mqtt sends one raw frame.  Unknown kinds
    are a programming error and raise ValueError.

    An unverified spec is refused unless allow_unverified is set: that is the
    second half of the platform gate, so an entity that predates the gate can
    never quietly send an experimental command.
    """
    if not spec.verified and not allow_unverified:
        raise UnverifiedCommandError(
            f"command {spec.kind!r} is not verified; "
            "enable allow_unverified_controls to use it"
        )
    if spec.kind == "property":
        if "on" not in kwargs:
            raise ValueError("property commands need an on/off keyword")
        payload = spec.on if kwargs["on"] else spec.off
        if not payload:
            raise ValueError("the command spec has no payload for that state")
        for key, value in payload.items():
            await transport.async_set_property(str(key), value)
        return

    if spec.kind == "invoke":
        if spec.function is None:
            raise ValueError("invoke commands need a function")
        params = dict(spec.params or {})
        if spec.param:
            if spec.param not in kwargs:
                raise ValueError(f"missing keyword {spec.param!r}")
            params[spec.param] = kwargs[spec.param]
        await transport.async_invoke(str(spec.function), params)
        return

    if spec.kind == "mqtt":
        if spec.command is None:
            raise ValueError("mqtt commands need a command number")
        body = bytes.fromhex(str(spec.body_hex or ""))
        await transport.async_send_frame(int(spec.command), body)
        return

    raise ValueError(f"unknown command kind {spec.kind!r}")
