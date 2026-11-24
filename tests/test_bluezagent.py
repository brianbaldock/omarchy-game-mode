"""The pairing agent: who may call it, what it shows, and that every request is answered exactly once."""

import unittest

from gi.repository import GLib

from gaminghome import bluetooth as bt
from gaminghome.bluezagent import (CANCELED, PASSKEY_LIMIT, REJECTED, PairingAgent, six_digits, valid_passkey,
                                   valid_pin)

OWNER = ":1.7"
DEVICE = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_01"
OTHER = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_02"
SIGNATURES = {"Release": "()", "Cancel": "()", "RequestPinCode": "(o)", "DisplayPinCode": "(os)",
              "RequestPasskey": "(o)", "DisplayPasskey": "(ouq)", "RequestConfirmation": "(ou)",
              "RequestAuthorization": "(o)", "AuthorizeService": "(os)"}
SAMPLES = {"Release": (), "Cancel": (), "RequestPinCode": (), "DisplayPinCode": ("123456",), "RequestPasskey": (),
           "DisplayPasskey": (123456, 0), "RequestConfirmation": (123456,), "RequestAuthorization": (),
           "AuthorizeService": ("0000110b-0000-1000-8000-00805f9b34fb",)}


class Invocation:
    """Records what the agent answered, and fails a test that answers twice."""

    def __init__(self):
        self.answers = []

    def return_value(self, value):
        self.answers.append(("value", None if value is None else value.unpack()))

    def return_dbus_error(self, name, _message):
        self.answers.append(("error", name))


class Connection:
    def __init__(self):
        self.registered, self.unregistered = [], []

    def register_object(self, path, info, method_call, _get_property, _set_property):
        self.registered.append((path, info.name, method_call))
        return len(self.registered)

    def unregister_object(self, registration):
        self.unregistered.append(registration)
        return True


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.owner, self.shown = OWNER, []
        self.connection = Connection()
        self.agent = PairingAgent(self.connection, "/io/github/brianbaldock/OmarchyGameMode/agent1", DEVICE,
                                  lambda: self.owner, self.show, lambda: self.shown.append(("hidden",)))
        self.assertTrue(self.agent.export())

    def show(self, kind, **values):
        self.shown.append((kind, values))

    def call(self, member, *arguments, sender=OWNER, device=DEVICE):
        invocation = Invocation()
        values = arguments or SAMPLES[member]
        full = values if member in ("Release", "Cancel") else (device, *values)
        self.agent._on_call(None, sender, self.agent.path, "org.bluez.Agent1", member,
                            GLib.Variant(SIGNATURES[member], full), invocation)
        return invocation.answers

    def test_it_is_exported_once_and_taken_off_the_bus_once(self):
        (path, interface, _handler), = self.connection.registered
        self.assertEqual((path, interface), (self.agent.path, "org.bluez.Agent1"))
        self.agent.close()
        self.agent.close()
        self.assertEqual(self.connection.unregistered, [1])

    def test_a_caller_that_is_not_the_owner_of_org_bluez_is_refused_and_nothing_shows(self):
        for member in SIGNATURES:
            with self.subTest(member=member):
                self.assertEqual(self.call(member, sender=":1.99"), [("error", REJECTED)])
        self.assertEqual((self.shown, self.agent.waiting, self.agent.released), ([], "", False))

    def test_nobody_may_call_while_nobody_owns_org_bluez(self):
        self.owner = None
        for member in SIGNATURES:
            with self.subTest(member=member):
                self.assertEqual(self.call(member, sender=OWNER), [("error", REJECTED)])
        self.assertEqual(self.shown, [])

    def test_once_the_attempt_ended_every_call_is_refused(self):
        self.agent.close()
        for member in SIGNATURES:
            with self.subTest(member=member):
                self.assertEqual(self.call(member), [("error", REJECTED)])
        self.assertEqual(self.shown, [])

    def test_a_call_about_another_device_is_refused(self):
        for member in set(SIGNATURES) - {"Release", "Cancel"}:
            with self.subTest(member=member):
                self.assertEqual(self.call(member, device=OTHER), [("error", REJECTED)])
        self.assertEqual((self.shown, self.agent.waiting), ([], ""))

    def test_authorization_is_given_for_the_device_being_paired(self):
        self.assertEqual(self.call("RequestAuthorization"), [("value", None)])
        self.assertEqual(self.shown, [])

    def test_a_service_is_never_authorised_even_for_the_device_being_paired(self):
        self.assertEqual(self.call("AuthorizeService"), [("error", REJECTED)])
        self.assertEqual(self.shown, [])

    def test_a_number_to_compare_waits_for_the_user_and_keeps_its_leading_zeros(self):
        answers = self.call("RequestConfirmation", 42)
        self.assertEqual((answers, self.agent.waiting), ([], bt.CONFIRM))
        self.assertEqual(self.shown, [(bt.CONFIRM, {"code": "000042"})])
        for wrong in ("yes", 1, None, "True"):
            self.assertFalse(self.agent.answer(wrong))
        self.assertEqual(answers, [])
        self.assertTrue(self.agent.answer(True))
        self.assertFalse(self.agent.answer(True))                       # answered: nothing waits any more
        self.assertEqual(answers, [("value", None)])

    def test_a_number_the_user_says_does_not_match_is_rejected(self):
        answers = self.call("RequestConfirmation", 999999)
        self.assertTrue(self.agent.answer(False))
        self.assertEqual(answers, [("error", REJECTED)])

    def test_a_number_that_is_not_a_passkey_is_refused(self):
        self.assertEqual(self.call("RequestConfirmation", PASSKEY_LIMIT + 1), [("error", REJECTED)])
        self.assertEqual(self.call("DisplayPasskey", 4294967295, 0), [("error", REJECTED)])
        self.assertEqual(self.shown, [])

    def test_a_second_request_while_one_waits_is_refused_and_the_first_still_waits(self):
        first = self.call("RequestPinCode")
        for member in ("RequestPasskey", "RequestConfirmation", "RequestPinCode", "RequestAuthorization",
                       "DisplayPasskey", "DisplayPinCode"):
            with self.subTest(member=member):
                self.assertEqual(self.call(member), [("error", REJECTED)])
        self.assertEqual((first, self.agent.waiting), ([], bt.PIN))
        self.assertEqual(self.shown, [(bt.PIN, {"max_length": bt.PIN_MAX})])
        self.assertTrue(self.agent.answer("0000"))
        self.assertEqual(first, [("value", ("0000",))])

    def test_a_pin_and_a_passkey_are_digits_only(self):
        answers = self.call("RequestPasskey")
        self.assertEqual(self.shown, [(bt.PASSKEY, {"max_length": bt.PASSKEY_MAX})])
        for wrong in ("", "12a", "1234567", "١٢٣", "12 3", "123\n", 123, None, True):
            with self.subTest(reply=wrong):
                self.assertFalse(self.agent.answer(wrong))
        self.assertTrue(self.agent.answer("012345"))
        self.assertEqual(answers, [("value", (12345,))])

    def test_the_rules_for_an_answer(self):
        self.assertEqual([valid_pin(text) for text in ("0", "0000", "1" * 16, "1" * 17, "", "12a", "١", "1\n")],
                         ["0", "0000", "1" * 16, None, None, None, None, None])
        self.assertEqual([valid_passkey(text) for text in ("0", "000000", "999999", "1234567", "-1", "", 5)],
                         [0, 0, 999999, None, None, None, None])
        self.assertEqual([six_digits(value) for value in (0, 42, 999999, 1000000, -1, True, "1")],
                         ["000000", "000042", "999999", None, None, None, None])

    def test_a_passkey_to_type_is_one_prompt_whose_count_goes_up(self):
        for entered in (0, 1, 2, 6, 9):
            self.assertEqual(self.call("DisplayPasskey", 1234, entered), [("value", None)])
        updates = [values["update"] for _kind, values in self.shown]
        typed = [values["typed"] for _kind, values in self.shown]
        self.assertEqual(updates, [False, True, True, True, True])
        self.assertEqual(typed, [0, 1, 2, 6, 6])
        self.assertEqual({values["code"] for _kind, values in self.shown}, {"001234"})
        self.assertEqual(self.agent.waiting, "")                        # nothing to answer: it is typed on the device

    def test_a_pin_to_type_is_made_safe_to_show(self):
        self.assertEqual(self.call("DisplayPinCode", "\u202e12\x0734\u200f"), [("value", None)])
        self.assertEqual(self.call("DisplayPinCode", "9" * 40), [("value", None)])
        self.assertEqual(self.call("DisplayPinCode", "AA:BB:CC:DD:EE:FF"), [("error", REJECTED)])
        self.assertEqual([values["code"] for _kind, values in self.shown], ["1234", "9" * bt.PIN_MAX])

    def test_bluez_taking_the_question_back_answers_it_once_and_hides_it(self):
        waiting = self.call("RequestConfirmation", 7)
        self.assertEqual(self.call("Cancel"), [("value", None)])
        self.assertEqual(waiting, [("error", CANCELED)])
        self.assertEqual(self.shown[-1], ("hidden",))
        self.assertFalse(self.agent.answer(True))
        self.assertEqual(waiting, [("error", CANCELED)])

    def test_release_answers_what_waits_and_ends_the_agent(self):
        waiting = self.call("RequestPasskey")
        self.assertEqual(self.call("Release"), [("value", None)])
        self.assertTrue(self.agent.released)
        self.assertEqual(waiting, [("error", CANCELED)])
        self.assertEqual(self.call("RequestPinCode"), [("error", REJECTED)])

    def test_closing_answers_what_waits_exactly_once(self):
        waiting = self.call("RequestPinCode")
        self.agent.close()
        self.agent.close()
        self.agent.withdraw()
        self.assertEqual(waiting, [("error", CANCELED)])

    def test_the_user_giving_up_answers_canceled(self):
        waiting = self.call("RequestConfirmation", 5)
        self.agent.withdraw()
        self.assertEqual(waiting, [("error", CANCELED)])
        self.assertEqual(self.agent.waiting, "")

    def test_an_exception_while_showing_still_answers_and_leaves_nothing_waiting(self):
        def broken(*_args, **_kwargs):
            raise RuntimeError("the window is gone")
        self.agent._show = broken
        self.assertEqual(self.call("RequestConfirmation", 5), [("error", REJECTED)])
        self.assertEqual(self.agent.waiting, "")
        self.assertEqual(self.call("DisplayPasskey", 5, 0), [("value", None)])     # answered before it was shown


if __name__ == "__main__":
    unittest.main()
