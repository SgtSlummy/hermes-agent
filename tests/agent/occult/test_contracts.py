from copy import deepcopy

import pytest

from agent.occult.contracts import (
    OCCULT_CONTRACT_VERSION,
    ContractVersionMismatch,
    InvalidContractPayload,
    UnsupportedCapability,
    contract_json_schema,
    is_occult_enabled,
    load_contract_fixture,
    load_contract_schema,
    validate_event_stream,
    validate_invocation,
)


def test_occult_feature_gate_is_explicit_and_fail_closed():
    assert is_occult_enabled(None) is False
    assert is_occult_enabled({}) is False
    assert is_occult_enabled({"occult": True}) is False
    assert is_occult_enabled({"occult": {"enabled": False}}) is False
    assert is_occult_enabled({"occult": {"enabled": True}}) is True
    assert is_occult_enabled({"occult": {"enabled": 1}}) is False


def test_packaged_invocation_fixture_validates():
    fixture = load_contract_fixture("invocation.valid.json")

    invocation = validate_invocation(fixture)

    assert invocation.contract_version == OCCULT_CONTRACT_VERSION
    assert invocation.routing.free_only is True
    assert invocation.routing.maximum_cost_usd == 0


def test_contract_version_mismatch_fails_before_model_validation():
    fixture = load_contract_fixture("invocation.valid.json")
    fixture["contract_version"] = "2.0.0"

    with pytest.raises(ContractVersionMismatch, match="expected '1.0.0'"):
        validate_invocation(fixture)


def test_unknown_required_capability_is_rejected():
    fixture = load_contract_fixture("invocation.valid.json")
    fixture["required_capabilities"].append("telepathy")

    with pytest.raises(
        UnsupportedCapability, match="unsupported required capabilities: telepathy"
    ):
        validate_invocation(fixture)


@pytest.mark.parametrize(
    "secret_field",
    ["api_key", "Authorization", "refresh-token", "credentials", "password"],
)
def test_secret_shaped_fields_are_rejected_recursively(secret_field):
    fixture = load_contract_fixture("invocation.valid.json")
    fixture["metadata"]["nested"] = {secret_field: "must-not-cross-boundary"}

    with pytest.raises(InvalidContractPayload, match="forbidden secret-shaped"):
        validate_invocation(fixture)


def test_validation_errors_do_not_echo_payload_values():
    fixture = load_contract_fixture("invocation.valid.json")
    fixture["input"]["message"] = ""
    fixture["metadata"]["private_note"] = "do-not-echo-this-value"

    with pytest.raises(InvalidContractPayload) as exc_info:
        validate_invocation(fixture)

    assert "do-not-echo-this-value" not in str(exc_info.value)
    assert "input.message" in str(exc_info.value)


def test_packaged_event_stream_has_one_terminal_event():
    fixture = load_contract_fixture("events.valid.json")

    events = validate_event_stream(fixture)

    assert [event.sequence for event in events] == [0, 1]
    assert events[-1].event_type.value == "reading.completed"


def test_event_stream_rejects_gaps_and_events_after_terminal():
    fixture = load_contract_fixture("events.valid.json")
    with_gap = deepcopy(fixture)
    with_gap[1]["sequence"] = 2

    with pytest.raises(InvalidContractPayload, match="contiguous"):
        validate_event_stream(with_gap)

    after_terminal = fixture + [
        {
            **deepcopy(fixture[0]),
            "event_id": "evt_example_003",
            "sequence": 2,
        }
    ]
    with pytest.raises(InvalidContractPayload, match="terminal"):
        validate_event_stream(after_terminal)


def test_contract_schema_exports_all_public_models():
    schema = contract_json_schema()

    assert schema["contract_version"] == OCCULT_CONTRACT_VERSION
    assert {
        "DeckDescriptor",
        "MajorArcanaAgent",
        "MinorArcanaRoute",
        "OccultError",
        "OccultInvocation",
        "ReadingDescriptor",
        "ReadingEvent",
        "RouteSummary",
        "SpreadDescriptor",
    } == set(schema["models"])


def test_checked_in_contract_schema_matches_runtime_export():
    assert load_contract_schema() == contract_json_schema()


def test_fixture_loader_does_not_accept_paths():
    with pytest.raises(InvalidContractPayload, match="plain filename"):
        load_contract_fixture("../invocation.valid.json")
