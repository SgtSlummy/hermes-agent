"""Occult System contracts and feature-gated integration helpers.

The package is deliberately inert. Importing it does not register providers,
change prompts, or alter Hermes runtime behavior.
"""

from agent.occult.contracts import (
    OCCULT_CONTRACT_VERSION,
    SUPPORTED_CAPABILITIES,
    ContractVersionMismatch,
    InvalidContractPayload,
    OccultContractError,
    UnsupportedCapability,
    contract_json_schema,
    is_occult_enabled,
    load_contract_fixture,
    validate_event_stream,
    validate_invocation,
)

__all__ = [
    "OCCULT_CONTRACT_VERSION",
    "SUPPORTED_CAPABILITIES",
    "ContractVersionMismatch",
    "InvalidContractPayload",
    "OccultContractError",
    "UnsupportedCapability",
    "contract_json_schema",
    "is_occult_enabled",
    "load_contract_fixture",
    "validate_event_stream",
    "validate_invocation",
]
