"""P3-T6 / P3-T7 — every function is registered, gated and policy-mapped.

The coverage tests here are the structural guarantee behind the safety
argument: it is not enough for the gate to be correct if a function can be
added that bypasses it.
"""

from __future__ import annotations

import json

import pytest

from app.policy.decorator import is_gated
from app.policy.gates import TOOL_POLICY
from app.tools import registry
from app.tools.idempotency import MUTATING_FUNCTIONS, canonical, idempotency_key
from app.tools.schemas import ARGUMENT_MODELS

TOOLS = registry.load()


# ------------------------------------------------------------- coverage ---


def test_every_argument_model_is_registered_as_a_tool():
    assert set(TOOLS) == set(ARGUMENT_MODELS)
    assert "suggest_appointment_type" in TOOLS, "the knowledge extension's tool"


def test_every_registered_tool_has_a_policy():
    """A tool without a policy entry would be unreachable by the gate."""
    assert set(TOOLS) == set(TOOL_POLICY)


def test_every_tool_passes_through_the_gate():
    """The structural half of AD-01.

    A correct gate protects nothing if a function can be registered around it.
    """
    for name, built in TOOLS.items():
        assert is_gated(built.func), f"{name} is registered but not gated"


def test_the_registry_is_idempotent():
    assert registry.load() is not TOOLS
    assert set(registry.load()) == set(TOOLS)


def test_registering_an_unknown_function_fails_loudly():
    with pytest.raises(KeyError, match="no argument model"):

        @registry.tool("delete_patient_record")
        def delete_patient_record():
            return None


# --------------------------------------------------------------- schema ---


def test_tool_definitions_are_well_formed():
    for definition in registry.tool_definitions():
        assert definition["name"] in ARGUMENT_MODELS
        assert definition["description"], f"{definition['name']} has no description"
        assert definition["strict"] is True
        schema = definition["input_schema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False


def test_the_schema_comes_from_the_pydantic_model(snapshot_dir):
    """AD-02 — one source of truth.

    The schema Claude receives must be the one the gate validates against, not a
    second hand-written copy.
    """
    for definition in registry.tool_definitions():
        model = ARGUMENT_MODELS[definition["name"]]
        assert definition["input_schema"]["properties"] == model.model_json_schema()["properties"]


def test_required_fields_match_the_argument_models():
    for definition in registry.tool_definitions():
        model = ARGUMENT_MODELS[definition["name"]]
        expected = set(model.model_json_schema().get("required", []))
        actual = set(definition["input_schema"].get("required", []))
        assert actual == expected, definition["name"]


def test_enum_values_reach_the_model():
    """The enums of spec §4 must appear in the schema Claude sees."""
    definitions = {d["name"]: d for d in registry.tool_definitions()}
    blob = json.dumps(definitions)

    for value in [
        "new_patient",
        "follow_up",
        "sick_visit",
        "wellness",
        "telehealth",
        "in_person",
        "dob",
        "phone",
        "address_zip",
        "intake_forms",
        "appointment_confirmation",
        "telehealth_link",
        "directions",
        "portal_access",
        "main_clinic",
        "satellite_office",
        "complex_symptoms",
        "ada_accommodation",
        "billing_issue",
        "prescription_refill",
        "test_results",
        "routine",
        "urgent",
        "emergency",
    ]:
        assert value in blob, f"{value} never reaches the model"


def test_descriptions_tell_the_model_about_ordering():
    """Tool descriptions carry the workflow rules the gate will enforce.

    Without them the model learns the ordering only by being denied, which
    wastes a turn on every conversation.
    """
    definitions = {d["name"]: d["description"] for d in registry.tool_definitions()}

    assert "check_patient_exists" in definitions["create_new_patient_record"]
    assert "verification" in definitions["get_patient_demographics"].lower()
    assert "search" in definitions["book_appointment"].lower()
    assert "get_patient_appointments" in definitions["cancel_appointment"]
    assert "escalate_to_staff" in definitions["check_insurance_eligibility"]


def test_no_description_leaks_internal_jargon():
    """These are read by the model, not by us."""
    for definition in registry.tool_definitions():
        assert "spec§" not in definition["description"], definition["name"]
        assert "AD-0" not in definition["description"], definition["name"]


# ---------------------------------------------------------- idempotency ---


def test_the_five_mutating_functions_take_a_key():
    assert {
        "create_new_patient_record",
        "book_appointment",
        "cancel_appointment",
        "reschedule_appointment",
        "send_secure_text",
    } == MUTATING_FUNCTIONS


def test_read_only_functions_take_no_key():
    assert registry.key_for.__doc__
    for name in ["check_patient_exists", "get_patient_appointments", "get_clinic_hours"]:
        assert name not in MUTATING_FUNCTIONS


def test_identical_calls_produce_the_same_key():
    first = idempotency_key("s_1", "book_appointment", {"a": 1, "b": 2})
    second = idempotency_key("s_1", "book_appointment", {"b": 2, "a": 1})

    assert first == second, "argument order must not change the key"


def test_different_calls_produce_different_keys():
    base = idempotency_key("s_1", "book_appointment", {"time": "09:30"})

    assert base != idempotency_key("s_1", "book_appointment", {"time": "10:00"})
    assert base != idempotency_key("s_2", "book_appointment", {"time": "09:30"})
    assert base != idempotency_key("s_1", "cancel_appointment", {"time": "09:30"})


def test_canonical_form_is_stable_across_types():
    from datetime import date

    assert canonical({"d": date(2026, 9, 8)}) == '{"d":"2026-09-08"}'


@pytest.fixture
def snapshot_dir(tmp_path):
    return tmp_path
