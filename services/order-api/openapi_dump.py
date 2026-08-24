"""Print order-api's OpenAPI document to stdout.

The Backstage catalog needs a spec file it can read from git (`API/orders` in
`catalog-info.yaml`). FastAPI builds that document from the route signatures at
runtime, so the checked-in copy is derived, not authored:

    pants run services/order-api:dump-openapi > services/order-api/openapi.json

`tests/test_openapi_spec.py` fails if the two drift, which is the only thing
keeping the catalog honest — a hand-maintained API spec is a lie with a
timestamp on it.
"""

import json
import os
import sys

# Settings are read at import time and fail closed on a missing broker list
# (§3.1). Nothing here connects to anything — the document is built from the
# route signatures — so placeholders are enough to get the module imported.
os.environ.setdefault("KAFKA_BROKERS", "localhost:9092")
os.environ.setdefault("S3_BUCKET", "unused")
os.environ.setdefault("ORDER_SIGNING_KEY", "unused")

from order_api.main import app  # noqa: E402


def main() -> None:
    json.dump(app.openapi(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
