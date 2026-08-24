import os

# Settings are read at import time, so the environment must be set first.
os.environ.setdefault("KAFKA_BROKERS", "localhost:9092")
os.environ.setdefault("S3_BUCKET", "test-bucket")
os.environ.setdefault("ORDER_SIGNING_KEY", "test-key")

import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from order_api.main import OrderIn, app, healthz, state  # noqa: E402


def test_healthz_needs_no_dependencies():
    """Liveness must answer without Kafka or S3, or a broker outage kills every pod."""
    assert healthz()["status"] == "ok"


def test_readyz_is_not_ready_before_startup():
    assert state["ready"] is False


@pytest.mark.parametrize(
    "field,value",
    [("quantity", 0), ("quantity", 1001), ("amount_cents", 0), ("customer", "")],
)
def test_invalid_orders_are_rejected(field, value):
    payload = {"customer": "ada", "sku": "W-1", "quantity": 1, "amount_cents": 100}
    payload[field] = value
    with pytest.raises(ValidationError):
        OrderIn(**payload)


def test_valid_order_is_accepted():
    order = OrderIn(customer="ada", sku="W-1", quantity=3, amount_cents=4999)
    assert order.quantity == 3


def test_routes_are_registered():
    paths = {r.path for r in app.routes}
    assert {"/orders", "/healthz", "/readyz", "/metrics"} <= paths
