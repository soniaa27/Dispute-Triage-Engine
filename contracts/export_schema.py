"""Export the published JSON Schema of DisputeRequest.

Usage:  python -m contracts.export_schema
"""

import json
import os
from pathlib import Path

from contracts.dispute import DisputeRequest

SCHEMA_PATH = Path(__file__).parent / "schema" / "dispute_request.schema.json"


def main() -> None:
    schema = DisputeRequest.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$comment"] = (
        "Generated from contracts/dispute.py by `make schema`. Do not edit by hand. "
        "Cross-field rules (enforced server-side, not expressible here): "
        "reported_datetime >= transaction_datetime; bank_alert_datetime >= transaction_datetime; "
        "no datetime in the future (5 min skew tolerated); "
        "customer_claims.card_in_possession must be null when channel is UPI."
    )
    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_text(json.dumps(schema, indent=2) + "\n")
    print(f"wrote {os.path.relpath(SCHEMA_PATH)}")


if __name__ == "__main__":
    main()
