"""The pairing agent Gaming Home registers with BlueZ for one pairing attempt at a time.

BlueZ asks a pairing agent (``org.bluez.Agent1``) for whatever a remote device needs from the user: a number to
compare, a code to type on the device, a PIN or a passkey to enter. ``bluez.BlueZ`` exports one ``PairingAgent``
on its own bus connection when the user presses Pair, registers it with BlueZ, and closes it when the pairing step
ends, whichever way it ends.

Every method checks its caller before it does anything else: the sender must be the unique name that owns
``org.bluez`` at that moment, the attempt must still be running, and a method that is about a device must be about
the device being paired. Any other call is answered ``org.bluez.Error.Rejected``, and nothing is shown or changed.
After that check:

* ``RequestAuthorization`` is accepted: pressing Pair was the authorisation.
* ``AuthorizeService`` is always refused. The trust given after pairing covers the device's later connections, and
  a service is never authorised just because its device is the one being paired.
* ``RequestConfirmation``, ``RequestPinCode`` and ``RequestPasskey`` wait for the user's answer, one at a time: a
  request that arrives while another one waits is refused.
* ``DisplayPasskey`` and ``DisplayPinCode`` show a code and are answered at once. ``Cancel`` takes the question
  back and ``Release`` says that BlueZ dropped the agent.

A request that waits is answered exactly once: by the user, by ``Cancel``, or with ``org.bluez.Error.Canceled``
when the attempt ends. Codes, PINs and passkeys are only ever shown: they are not logged and not kept after the
prompt ends.
"""

from __future__ import annotations

import re
from typing import Callable

from gi.repository import Gio, GLib

from . import bluetooth

AGENT_IFACE = "org.bluez.Agent1"
CAPABILITY = "KeyboardDisplay"     # can show a code and take digits: every way of pairing BlueZ offers
REJECTED = "org.bluez.Error.Rejected"
CANCELED = "org.bluez.Error.Canceled"
PASSKEY_LIMIT = 999_999            # a passkey has six decimal digits
MAX_PIN_TEXT = 256                 # how much of a PIN to display is looked at before it is cut to PIN_MAX

# Written from the method names and signatures of BlueZ's documented Agent1 interface.
_XML = """<node><interface name="org.bluez.Agent1">
  <method name="Release"/>
  <method name="RequestPinCode"><arg type="o" direction="in"/><arg type="s" direction="out"/></method>
  <method name="DisplayPinCode"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
  <method name="RequestPasskey"><arg type="o" direction="in"/><arg type="u" direction="out"/></method>
  <method name="DisplayPasskey">
    <arg type="o" direction="in"/><arg type="u" direction="in"/><arg type="q" direction="in"/>
  </method>
  <method name="RequestConfirmation"><arg type="o" direction="in"/><arg type="u" direction="in"/></method>
  <method name="RequestAuthorization"><arg type="o" direction="in"/></method>
  <method name="AuthorizeService"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
  <method name="Cancel"/>
</interface></node>"""
INTERFACE = Gio.DBusNodeInfo.new_for_xml(_XML).interfaces[0]

# The methods whose first argument is the object path of the device they are about.
ABOUT_DEVICE = frozenset({"RequestPinCode", "DisplayPinCode", "RequestPasskey", "DisplayPasskey",
                          "RequestConfirmation", "RequestAuthorization", "AuthorizeService"})

_PIN = re.compile(f"[0-9]{{1,{bluetooth.PIN_MAX}}}")
_PASSKEY = re.compile(f"[0-9]{{1,{bluetooth.PASSKEY_MAX}}}")


def valid_pin(reply) -> str | None:
    """The PIN to send for this answer, or None: one to ``PIN_MAX`` ASCII digits and nothing else."""
    if isinstance(reply, str) and _PIN.fullmatch(reply):
        return str(reply)
    return None


def valid_passkey(reply) -> int | None:
    """The passkey to send for this answer, or None: one to ``PASSKEY_MAX`` ASCII digits and nothing else."""
    if isinstance(reply, str) and _PASSKEY.fullmatch(reply):
        return int(reply)
    return None


def six_digits(passkey) -> str | None:
    """A passkey as the six digits both sides show, leading zeros kept; None when it is not a passkey."""
    if isinstance(passkey, int) and not isinstance(passkey, bool) and 0 <= passkey <= PASSKEY_LIMIT:
        return f"{passkey:06d}"
    return None


class _Call:
    """One method call to the agent, which is answered once at most."""

    __slots__ = ("invocation", "answered")

    def __init__(self, invocation):
        self.invocation, self.answered = invocation, False

    def accept(self, value: GLib.Variant | None = None):
        if not self.answered:
            self.answered = True
            self.invocation.return_value(value)

    def refuse(self, name: str = REJECTED):
        if not self.answered:
            self.answered = True
            self.invocation.return_dbus_error(name, name.rsplit(".", 1)[-1])


class PairingAgent:
    """``org.bluez.Agent1`` on one connection, for one attempt to pair with one device.

    ``device`` is the object path of that device and ``owner()`` returns the unique name that owns ``org.bluez``
    now (None while nobody does). ``show(kind, code="", typed=0, max_length=0, update=False)`` puts a question or a
    code in front of the user, ``update`` meaning the same prompt with new values, and ``hide()`` takes it away.
    Both are only ever called for a call that passed the checks.
    """

    def __init__(self, connection, path: str, device: str, owner: Callable[[], str | None],
                 show: Callable[..., None], hide: Callable[[], None]):
        self.connection, self.path, self.device = connection, path, device
        self._owner, self._show, self._hide = owner, show, hide
        self.active = False         # exported, and the attempt has not ended
        self.released = False       # BlueZ dropped this agent by itself
        self._registration = None
        self._pending: tuple[str, _Call] | None = None   # the request that waits for the user
        self._showing = ""          # the method whose code is on screen
        self._handlers = {
            "Release": self._release, "Cancel": self._cancel,
            "RequestAuthorization": self._request_authorization, "AuthorizeService": self._authorize_service,
            "RequestConfirmation": self._request_confirmation, "RequestPinCode": self._request_pin_code,
            "RequestPasskey": self._request_passkey, "DisplayPasskey": self._display_passkey,
            "DisplayPinCode": self._display_pin_code,
        }

    # ---- life ---------------------------------------------------------------
    def export(self) -> bool:
        try:
            self._registration = self.connection.register_object(self.path, INTERFACE, self._on_call, None, None)
        except (GLib.Error, TypeError, ValueError):
            return False
        self.active = True
        return True

    def close(self):
        """The attempt ended: answer what still waits, and take the object off the bus. Idempotent."""
        self.active = False
        self._withdraw()
        self._showing = ""
        registration, self._registration = self._registration, None
        if registration is not None:
            try:
                self.connection.unregister_object(registration)
            except (GLib.Error, TypeError):
                pass

    @property
    def exported(self) -> bool:
        return self._registration is not None

    @property
    def waiting(self) -> str:
        """The kind of prompt whose answer BlueZ waits for, or an empty string."""
        return self._pending[0] if self._pending is not None else ""

    # ---- the user's side ----------------------------------------------------
    def answer(self, reply) -> bool:
        """Send the user's answer to the request that waits. False, and nothing sent, when it does not fit."""
        if self._pending is None:
            return False
        kind, call = self._pending
        if kind == bluetooth.CONFIRM:
            if type(reply) is not bool:
                return False
            self._pending = None
            if reply:
                call.accept()
            else:
                call.refuse()
            return True
        value = None
        if kind == bluetooth.PIN:
            pin = valid_pin(reply)
            value = None if pin is None else GLib.Variant("(s)", (pin,))
        elif kind == bluetooth.PASSKEY:
            passkey = valid_passkey(reply)
            value = None if passkey is None else GLib.Variant("(u)", (passkey,))
        if value is None:
            return False
        self._pending = None
        call.accept(value)
        return True

    def withdraw(self):
        """The user gave up: the request that waits is answered ``Canceled`` and the code on screen goes."""
        self._withdraw()
        self._showing = ""

    def _withdraw(self):
        pending, self._pending = self._pending, None
        if pending is not None:
            pending[1].refuse(CANCELED)

    # ---- BlueZ's side -------------------------------------------------------
    def _allowed(self, sender, device) -> bool:
        owner = self._owner()
        return (self.active and not self.released and bool(owner) and sender == owner
                and (device is None or device == self.device))

    def _on_call(self, _connection, sender, _path, _interface, member, parameters, invocation):
        call = _Call(invocation)
        try:
            device = None
            if member in ABOUT_DEVICE:
                device = parameters.get_child_value(0).get_string()
            handler = self._handlers.get(member)
            if handler is None or not self._allowed(sender, device):
                call.refuse()
                return
            handler(call, parameters)
        except Exception:  # noqa: BLE001 - whatever happened, BlueZ gets an answer and the main loop no exception
            if self._pending is not None and self._pending[1] is call:
                self._pending = None
            call.refuse()
        finally:
            if not call.answered and (self._pending is None or self._pending[1] is not call):
                call.refuse()

    def _release(self, call, _parameters):
        self.released = True
        self.withdraw()
        self._hide()
        call.accept()

    def _cancel(self, call, _parameters):
        self.withdraw()
        self._hide()
        call.accept()

    def _request_authorization(self, call, _parameters):
        if self._pending is not None:
            call.refuse()
        else:
            call.accept()

    @staticmethod
    def _authorize_service(call, _parameters):
        call.refuse()

    def _request_confirmation(self, call, parameters):
        code = six_digits(parameters.get_child_value(1).get_uint32())
        if self._pending is not None or code is None:
            call.refuse()
            return
        self._pending, self._showing = (bluetooth.CONFIRM, call), ""
        self._show(bluetooth.CONFIRM, code=code)

    def _request_pin_code(self, call, _parameters):
        self._ask(call, bluetooth.PIN, bluetooth.PIN_MAX)

    def _request_passkey(self, call, _parameters):
        self._ask(call, bluetooth.PASSKEY, bluetooth.PASSKEY_MAX)

    def _ask(self, call, kind, max_length):
        if self._pending is not None:
            call.refuse()
            return
        self._pending, self._showing = (kind, call), ""
        self._show(kind, max_length=max_length)

    def _display_passkey(self, call, parameters):
        code = six_digits(parameters.get_child_value(1).get_uint32())
        if self._pending is not None or code is None:
            call.refuse()
            return
        typed = max(0, min(parameters.get_child_value(2).get_uint16(), len(code)))
        update = self._showing == "DisplayPasskey"
        self._showing = "DisplayPasskey"
        call.accept()
        self._show(bluetooth.CODE, code=code, typed=typed, update=update)

    def _display_pin_code(self, call, parameters):
        code = bluetooth.display_name(parameters.get_child_value(1).get_string()[:MAX_PIN_TEXT])
        code = code[:bluetooth.PIN_MAX].rstrip()
        if self._pending is not None or not code:
            call.refuse()
            return
        self._showing = "DisplayPinCode"
        call.accept()
        self._show(bluetooth.CODE, code=code)
