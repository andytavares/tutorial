"""The checked-in OpenAPI document must match what the app actually serves.

`catalog-info.yaml` points Backstage at `services/order-api/openapi.json`. A spec
that drifts from the code is worse than no spec: the portal keeps rendering it,
confidently, long after the route changed. This test is the thing that stops
that, and it is the reason the file is generated rather than written.
"""

import json
import os
from pathlib import Path

os.environ.setdefault("KAFKA_BROKERS", "localhost:9092")
os.environ.setdefault("S3_BUCKET", "test-bucket")
os.environ.setdefault("ORDER_SIGNING_KEY", "test-key")

from order_api.main import app  # noqa: E402

# tests/ sits at the source root (`/services/order-api`), so the spec is one
# level up from this file inside the Pants sandbox.
SPEC = Path(__file__).parent.parent / "openapi.json"


def test_checked_in_spec_matches_the_running_app():
    assert SPEC.exists(), f"{SPEC.name} is missing; run `pants run services/order-api:dump-openapi`"
    assert json.loads(SPEC.read_text()) == app.openapi(), (
        "openapi.json is stale. Regenerate it:\n"
        "  pants run services/order-api:dump-openapi > services/order-api/openapi.json"
    )
