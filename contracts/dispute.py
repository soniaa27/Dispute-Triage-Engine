"""Ingestion data contract for POST /disputes.

Every rule here is enforced at the ingestion boundary, before any agent run starts.
A request that fails validation is quarantined with its field-level errors.
"""

from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

# Tolerated clock skew between the client and the server when rejecting future datetimes.
FUTURE_SKEW = timedelta(minutes=5)

MAX_TRANSACTION_AMOUNT_INR = 10_000_000


class AccountType(StrEnum):
    """Account categories, aligned with the RBI circular's liability caps table."""

    BSBD = "BSBD"
    SAVINGS = "SAVINGS"
    CURRENT_MSME = "CURRENT_MSME"
    CURRENT_INDIVIDUAL_UPTO_25L = "CURRENT_INDIVIDUAL_UPTO_25L"
    CURRENT_OTHER = "CURRENT_OTHER"
    CREDIT_CARD_UPTO_5L = "CREDIT_CARD_UPTO_5L"
    CREDIT_CARD_ABOVE_5L = "CREDIT_CARD_ABOVE_5L"
    PREPAID = "PREPAID"


class Channel(StrEnum):
    CARD_PRESENT = "CARD_PRESENT"
    CARD_NOT_PRESENT = "CARD_NOT_PRESENT"
    UPI = "UPI"
    NET_BANKING = "NET_BANKING"
    MOBILE_BANKING = "MOBILE_BANKING"
    ATM = "ATM"


class CustomerClaims(BaseModel):
    """What the customer asserts. Each is optional: null means 'not stated'."""

    model_config = ConfigDict(extra="forbid")

    shared_otp_or_pin: bool | None = None
    card_in_possession: bool | None = None
    received_alert: bool | None = None


class DisputeRequest(BaseModel):
    """A customer's report of an unauthorised card / UPI transaction.

    All datetimes must carry a timezone offset (e.g. +05:30) so reporting delays are unambiguous.
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [
                {
                    "customer_id": "CUST-000123",
                    "account_type": "SAVINGS",
                    "channel": "CARD_NOT_PRESENT",
                    "transaction_id": "TXN-2026-000001",
                    "transaction_amount_inr": 4999.0,
                    "transaction_datetime": "2026-09-01T14:05:00+05:30",
                    "bank_alert_datetime": "2026-09-01T14:06:00+05:30",
                    "reported_datetime": "2026-09-02T10:00:00+05:30",
                    "home_branch_state": "Karnataka",
                    "complaint_text": "An online purchase I did not make appeared on my card.",
                    "customer_claims": {
                        "shared_otp_or_pin": False,
                        "card_in_possession": True,
                        "received_alert": True,
                    },
                }
            ]
        },
    )

    customer_id: str = Field(
        min_length=3, max_length=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
        description="Bank customer identifier (synthetic).",
    )
    account_type: AccountType
    channel: Channel
    transaction_id: str = Field(
        min_length=4, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
    )
    transaction_amount_inr: float = Field(
        gt=0, le=MAX_TRANSACTION_AMOUNT_INR, allow_inf_nan=False,
        description="Disputed amount in INR; > 0 and <= 1,00,00,000.",
    )
    transaction_datetime: AwareDatetime
    bank_alert_datetime: AwareDatetime | None = Field(
        default=None, description="When the bank's SMS/email alert for the transaction was sent.",
    )
    reported_datetime: AwareDatetime = Field(
        description="When the customer reported the transaction to the bank.",
    )
    home_branch_state: str = Field(
        min_length=2, max_length=50, pattern=r"^[A-Za-z][A-Za-z .&-]*$",
        description="State/UT of the home branch; used for the working-day calendar.",
    )
    complaint_text: str = Field(min_length=10, max_length=4000)
    customer_claims: CustomerClaims = Field(default_factory=CustomerClaims)

    @model_validator(mode="after")
    def _cross_field_rules(self) -> "DisputeRequest":
        now = datetime.now(UTC) + FUTURE_SKEW
        for name in ("transaction_datetime", "bank_alert_datetime", "reported_datetime"):
            value = getattr(self, name)
            if value is not None and value > now:
                raise ValueError(f"{name} must not be in the future")
        if self.reported_datetime < self.transaction_datetime:
            raise ValueError("reported_datetime must be >= transaction_datetime")
        if (
            self.bank_alert_datetime is not None
            and self.bank_alert_datetime < self.transaction_datetime
        ):
            raise ValueError("bank_alert_datetime must be >= transaction_datetime")
        if self.channel == Channel.UPI and self.customer_claims.card_in_possession is not None:
            raise ValueError("customer_claims.card_in_possession must be null when channel is UPI")
        return self


class DisputeAccepted(BaseModel):
    """202 response for an accepted dispute."""

    run_id: str
    status: str
    status_url: str
