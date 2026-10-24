from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from contracts import DisputeRequest, Route, RunResult, RunStatus


def _errors(payload) -> str:
    with pytest.raises(ValidationError) as exc:
        DisputeRequest.model_validate(payload)
    return str(exc.value)


def test_valid_request_passes(payload):
    req = DisputeRequest.model_validate(payload)
    assert req.transaction_amount_inr == 4999.0
    assert req.customer_claims.card_in_possession is True


def test_minimal_request_defaults_claims(payload):
    del payload["customer_claims"]
    del payload["bank_alert_datetime"]
    req = DisputeRequest.model_validate(payload)
    assert req.customer_claims.shared_otp_or_pin is None
    assert req.bank_alert_datetime is None


def test_reported_before_transaction_rejected(payload):
    payload["reported_datetime"] = "2026-08-31T10:00:00+05:30"
    assert "reported_datetime must be >= transaction_datetime" in _errors(payload)


def test_alert_before_transaction_rejected(payload):
    payload["bank_alert_datetime"] = "2026-09-01T14:00:00+05:30"
    assert "bank_alert_datetime must be >= transaction_datetime" in _errors(payload)


@pytest.mark.parametrize(
    "field", ["transaction_datetime", "bank_alert_datetime", "reported_datetime"]
)
def test_future_datetime_rejected(payload, field):
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    # Keep the other cross-field rules satisfied so only the future check fires.
    for name in ("transaction_datetime", "bank_alert_datetime", "reported_datetime"):
        if name == field or field == "transaction_datetime":
            payload[name] = future
    assert f"{field} must not be in the future" in _errors(payload)


def test_upi_with_card_in_possession_rejected(payload):
    payload["channel"] = "UPI"
    assert "card_in_possession must be null when channel is UPI" in _errors(payload)


def test_upi_without_card_claim_passes(payload):
    payload["channel"] = "UPI"
    payload["customer_claims"]["card_in_possession"] = None
    assert DisputeRequest.model_validate(payload).channel == "UPI"


@pytest.mark.parametrize("amount", [0, -1, 10_000_000.01, float("inf")])
def test_out_of_range_amount_rejected(payload, amount):
    payload["transaction_amount_inr"] = amount
    assert "transaction_amount_inr" in _errors(payload)


def test_amount_upper_bound_inclusive(payload):
    payload["transaction_amount_inr"] = 10_000_000
    assert DisputeRequest.model_validate(payload).transaction_amount_inr == 10_000_000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_type", "GOLD"),
        ("channel", "CHEQUE"),
        ("customer_id", ""),
        ("complaint_text", "too short"),
        ("complaint_text", "x" * 4001),
        ("transaction_datetime", "2026-09-01T14:05:00"),  # naive datetime
    ],
)
def test_field_rules_rejected(payload, field, value):
    payload[field] = value
    assert field in _errors(payload)


def test_unknown_field_rejected(payload):
    payload["approve_refund"] = True
    assert "approve_refund" in _errors(payload)


def test_run_result_completed_requires_citations():
    with pytest.raises(ValidationError):
        RunResult(
            run_id="r", status=RunStatus.completed, route=Route.chargeback, prompt_version="v"
        )


def test_run_result_failed_cannot_carry_route():
    with pytest.raises(ValidationError):
        RunResult(run_id="r", status=RunStatus.failed, route=Route.decline, prompt_version="v")
