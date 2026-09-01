"""P2-T14 — redaction, masking and the denial-vocabulary tripwire."""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.policy import messages
from app.policy.redaction import (
    contains_protected_data,
    digest,
    mask,
    mask_date,
    mask_email,
    mask_phone,
    mask_zip,
    redact_args,
    redact_text,
    redact_value,
)

# Values that appear in the patient fixture. Nothing the system writes or says
# may contain one of these.
FIXTURE_VALUES = [
    "1978-03-04",
    "+12065550142",
    "206-555-0142",
    "98101",
    "amara.osei@example.invalid",
]


# --------------------------------------------------------- field-aware ---


@pytest.mark.parametrize(
    "field,value,token",
    [
        ("date_of_birth", "1978-03-04", "<dob>"),
        ("phone_number", "+12065550142", "<phone>"),
        ("identifier_1_value", "98101", "<identifier>"),
        ("identifier_2_value", "1978-03-04", "<identifier>"),
        ("email", "amara.osei@example.invalid", "<email>"),
        ("address_zip", "98101", "<zip>"),
        ("notes", "Patient sounded upset", "<notes>"),
        ("first_name", "Amara", "<name>"),
        ("last_name", "Osei", "<name>"),
    ],
)
def test_sensitive_fields_become_type_tokens(field, value, token):
    assert redact_value(field, value) == token


@pytest.mark.parametrize(
    "field", ["patient_id", "appointment_id", "new_appointment_slot_id", "ticket_id"]
)
def test_clinic_issued_references_survive(field):
    """These are references the clinic minted, not facts about a person —
    and the audit log is useless without them."""
    assert redact_value(field, "PT-4101") == "PT-4101"


def test_a_none_value_stays_none():
    assert redact_value("email", None) is None


def test_nested_structures_are_walked():
    payload = {"patient_id": "PT-4101", "contact": {"phone_number": "+12065550142"}}
    assert redact_value("root", payload) == {
        "patient_id": "PT-4101",
        "contact": {"phone_number": "<phone>"},
    }


def test_redact_args_produces_a_log_safe_view():
    view = redact_args(
        {
            "first_name": "Amara",
            "last_name": "Osei",
            "date_of_birth": "1978-03-04",
            "phone_number": "+12065550142",
            "patient_id": "PT-4101",
        }
    )

    assert view["date_of_birth"] == "<dob>"
    assert view["phone_number"] == "<phone>"
    assert view["first_name"] == "<name>"
    # The clinic-issued reference survives: it is what makes the log traceable.
    assert view["patient_id"] == "PT-4101"
    assert "1978-03-04" not in json.dumps(view)
    assert "Amara" not in json.dumps(view)


# ------------------------------------------------------- pattern sweep ---


@pytest.mark.parametrize(
    "text,token",
    [
        ("born 1978-03-04", "<dob>"),
        ("born 3/4/1978", "<dob>"),
        ("call me on 206-555-0142", "<phone>"),
        ("call me on (206) 555 0142", "<phone>"),
        ("email amara.osei@example.invalid", "<email>"),
        ("zip 98101", "<zip>"),
        ("ssn 123-45-6789", "<ssn>"),
    ],
)
def test_the_sweep_catches_values_that_leaked_into_free_text(text, token):
    """Field-aware redaction alone would miss these."""
    swept = redact_text(text)
    assert token in swept
    assert contains_protected_data(text)


def test_ordinary_text_is_left_alone():
    text = "The patient asked about parking at the main clinic."
    assert redact_text(text) == text
    assert not contains_protected_data(text)


def test_a_free_text_field_is_swept_even_when_not_named_sensitive():
    assert redact_value("some_note", "reach me on 206-555-0142") == "reach me on <phone>"


def test_a_bare_date_loses_its_precision():
    assert redact_value("checked_at", date(1978, 3, 4)) == "<date>"


# ------------------------------------------------------------- hashing ---


def test_a_digest_is_stable_within_a_salt():
    assert digest("98101", "salt-a") == digest("98101", "salt-a")


def test_a_digest_is_case_and_space_insensitive():
    assert digest(" 98101 ", "salt-a") == digest("98101", "salt-a")


def test_a_digest_does_not_correlate_across_salts():
    assert digest("98101", "salt-a") != digest("98101", "salt-b")


def test_a_digest_does_not_contain_the_value():
    assert "98101" not in digest("98101", "salt-a")


# ------------------------------------------------------------- masking ---


def test_masking_keeps_just_enough_to_confirm():
    """Enough for the patient to recognise, not enough to disclose (spec §4.2)."""
    assert mask_phone("+12065550142") == "(•••) •••-0142"
    assert mask_date(date(1978, 3, 4)) == "••/••/1978"
    assert mask_date("1978-03-04") == "••/••/1978"
    assert mask_zip("98101") == "•••01"
    assert mask_email("amara.osei@example.invalid") == "a•••@example.invalid"


def test_masking_by_kind():
    assert mask("phone", "+12065550142") == "(•••) •••-0142"
    assert mask("dob", "1978-03-04") == "••/••/1978"
    assert mask("address_zip", "98101") == "•••01"


def test_an_unknown_kind_is_fully_masked_not_passed_through():
    """Failing closed: an unrecognised identifier kind must not print in full."""
    assert mask("passport", "X1234567") == "•" * 8


def test_a_malformed_date_masks_rather_than_raising():
    assert mask_date("not a date") == "••/••/••••"


@pytest.mark.parametrize("value", FIXTURE_VALUES)
def test_no_masked_value_leaks_the_original(value):
    for kind in ["phone", "dob", "address_zip", "email"]:
        masked = mask(kind, value)
        assert masked != value


# ------------------------------------------ the denial-vocabulary tripwire ---


@pytest.mark.parametrize("text", messages.all_patient_facing_strings())
def test_no_denial_string_contains_protected_data(text):
    """spec §3 rule 5.

    The whole point of a fixed vocabulary is that it cannot interpolate a record
    value. This asserts that nobody has added a string that does.
    """
    assert not contains_protected_data(text), text


@pytest.mark.parametrize("text", messages.all_patient_facing_strings())
@pytest.mark.parametrize("value", FIXTURE_VALUES)
def test_no_denial_string_contains_a_fixture_value(text, value):
    assert value not in text


def test_every_denial_code_has_a_message():
    for code in messages.DenialCode:
        assert messages.denial_message(code)


def test_every_remedy_has_text():
    for remedy in messages.Remedy:
        assert messages.remedy_text(remedy)


def test_remedies_say_what_to_do_next():
    """A remedy that does not name an action is not a remedy."""
    actionable = (
        "call",
        "ask",
        "collect",
        "offer",
        "correct",
        "verify",
        "take",
        "confirm",
        "escalate",
        "transfer",
        # r3 — where the answer is "not here", the action is to send the person
        # somewhere it can be answered.
        "direct",
        "establish",
    )
    for remedy in messages.Remedy:
        text = messages.remedy_text(remedy).lower()
        assert any(verb in text for verb in actionable), f"{remedy}: {text}"
