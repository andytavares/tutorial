from collections.abc import Iterator

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc  # type: ignore[import-untyped]
from grpc_reflection.v1alpha import (  # type: ignore[import-untyped]
    reflection,
    reflection_pb2,
    reflection_pb2_grpc,
)
from shop.v1 import pricing_pb2, pricing_pb2_grpc

from pricing.main import (
    HEALTH_SERVICE_NAME,
    PRICING_SERVICE_NAME,
    build_server,
    calculate_price,
    validate_request,
)
from pricing.settings import Settings


class TestCalculatePriceV1:
    def test_no_discount_regardless_of_quantity(self) -> None:
        total, discount, rule = calculate_price(
            quantity=5, unit_amount_cents=1000, version="v1"
        )
        assert total == 5000
        assert discount == 0
        assert rule == "list-price"

    def test_single_unit(self) -> None:
        total, discount, rule = calculate_price(
            quantity=1, unit_amount_cents=250, version="v1"
        )
        assert total == 250
        assert discount == 0
        assert rule == "list-price"


class TestCalculatePriceV2:
    def test_discounts_at_quantity_three(self) -> None:
        total, discount, rule = calculate_price(
            quantity=3, unit_amount_cents=1000, version="v2"
        )
        assert discount == 300  # 10% of 3000
        assert total == 2700
        assert rule == "bulk-10pct"

    def test_discounts_above_quantity_three(self) -> None:
        total, discount, rule = calculate_price(
            quantity=10, unit_amount_cents=999, version="v2"
        )
        assert discount == 999  # floor(9990 * 0.10)
        assert total == 9990 - 999
        assert rule == "bulk-10pct"

    def test_no_discount_below_quantity_three(self) -> None:
        total, discount, rule = calculate_price(
            quantity=2, unit_amount_cents=1000, version="v2"
        )
        assert total == 2000
        assert discount == 0
        assert rule == "list-price"

    def test_discount_rounds_down(self) -> None:
        # list_total = 3 * 101 = 303; 10% = 30.3 -> floor to 30
        total, discount, rule = calculate_price(
            quantity=3, unit_amount_cents=101, version="v2"
        )
        assert discount == 30
        assert total == 273
        assert rule == "bulk-10pct"


def test_served_by_reflects_version() -> None:
    # calculate_price itself doesn't set served_by (that's the servicer's job),
    # but the version argument it's given is what would end up in served_by.
    _, _, rule_v1 = calculate_price(quantity=3, unit_amount_cents=1000, version="v1")
    _, _, rule_v2 = calculate_price(quantity=3, unit_amount_cents=1000, version="v2")
    assert rule_v1 == "list-price"
    assert rule_v2 == "bulk-10pct"


class TestValidateRequest:
    def test_valid_request_does_not_raise(self) -> None:
        validate_request(sku="widget-1", quantity=1, unit_amount_cents=1)

    def test_empty_sku_raises(self) -> None:
        with pytest.raises(ValueError, match="sku"):
            validate_request(sku="", quantity=1, unit_amount_cents=100)

    def test_zero_quantity_raises(self) -> None:
        with pytest.raises(ValueError, match="quantity"):
            validate_request(sku="widget-1", quantity=0, unit_amount_cents=100)

    def test_negative_quantity_raises(self) -> None:
        with pytest.raises(ValueError, match="quantity"):
            validate_request(sku="widget-1", quantity=-1, unit_amount_cents=100)

    def test_zero_unit_amount_raises(self) -> None:
        with pytest.raises(ValueError, match="unit_amount_cents"):
            validate_request(sku="widget-1", quantity=1, unit_amount_cents=0)

    def test_negative_unit_amount_raises(self) -> None:
        with pytest.raises(ValueError, match="unit_amount_cents"):
            validate_request(sku="widget-1", quantity=1, unit_amount_cents=-5)


class TestSettings:
    def test_reads_prefixed_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRICING_GRPC_PORT", "50999")
        monkeypatch.setenv("PRICING_HTTP_PORT", "9111")
        monkeypatch.setenv("PRICING_VERSION", "v2")
        loaded = Settings()
        assert loaded.grpc_port == 50999
        assert loaded.http_port == 9111
        assert loaded.version == "v2"

    def test_ports_are_coerced_to_int(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRICING_GRPC_PORT", "50051")
        assert Settings().grpc_port == 50051

    def test_non_numeric_port_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRICING_GRPC_PORT", "not-a-port")
        with pytest.raises(ValueError, match="grpc_port"):
            Settings()


@pytest.fixture
def channel() -> Iterator[grpc.Channel]:
    """The real server, on an ephemeral port, over a real channel.

    Health checking and reflection are wire protocols; asserting on them from
    inside the process would prove nothing about what a kubelet or grpcurl sees.
    """
    server, _ = build_server()
    port = server.add_insecure_port("[::]:0")
    server.start()
    with grpc.insecure_channel(f"localhost:{port}") as chan:
        yield chan
    server.stop(None).wait()


class TestHealthChecking:
    def test_pricing_service_reports_serving(self, channel: grpc.Channel) -> None:
        stub = health_pb2_grpc.HealthStub(channel)
        response = stub.Check(
            health_pb2.HealthCheckRequest(service="shop.v1.Pricing"), timeout=5
        )
        assert response.status == health_pb2.HealthCheckResponse.SERVING

    def test_overall_health_reports_serving(self, channel: grpc.Channel) -> None:
        # The empty service name is the server's overall status, and it is what
        # a `grpc:` probe with no `service:` field asks for.
        stub = health_pb2_grpc.HealthStub(channel)
        response = stub.Check(health_pb2.HealthCheckRequest(service=""), timeout=5)
        assert response.status == health_pb2.HealthCheckResponse.SERVING

    def test_unregistered_service_is_not_found(self, channel: grpc.Channel) -> None:
        stub = health_pb2_grpc.HealthStub(channel)
        with pytest.raises(grpc.RpcError) as excinfo:
            stub.Check(health_pb2.HealthCheckRequest(service="shop.v1.Nope"), timeout=5)
        assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND

    def test_graceful_shutdown_flips_every_service(self) -> None:
        server, health_servicer = build_server()
        port = server.add_insecure_port("[::]:0")
        server.start()
        try:
            with grpc.insecure_channel(f"localhost:{port}") as chan:
                stub = health_pb2_grpc.HealthStub(chan)
                health_servicer.enter_graceful_shutdown()
                for service_name in ("", "shop.v1.Pricing"):
                    response = stub.Check(
                        health_pb2.HealthCheckRequest(service=service_name), timeout=5
                    )
                    assert response.status == health_pb2.HealthCheckResponse.NOT_SERVING
        finally:
            server.stop(None).wait()


class TestReflection:
    def test_lists_pricing_health_and_reflection(self, channel: grpc.Channel) -> None:
        stub = reflection_pb2_grpc.ServerReflectionStub(channel)
        responses = stub.ServerReflectionInfo(
            iter([reflection_pb2.ServerReflectionRequest(list_services="")]),
            timeout=5,
        )
        listed = {
            service.name
            for response in responses
            for service in response.list_services_response.service
        }
        assert PRICING_SERVICE_NAME in listed
        assert HEALTH_SERVICE_NAME in listed
        assert reflection.SERVICE_NAME in listed


class TestPriceOrderRpc:
    def test_valid_request_is_priced(self, channel: grpc.Channel) -> None:
        stub = pricing_pb2_grpc.PricingStub(channel)
        response = stub.PriceOrder(
            pricing_pb2.PriceOrderRequest(
                sku="widget-1", quantity=1, unit_amount_cents=250
            ),
            timeout=5,
        )
        assert response.total_amount_cents == 250
        assert response.rule_applied == "list-price"

    def test_invalid_request_aborts_with_invalid_argument(
        self, channel: grpc.Channel
    ) -> None:
        # abort() must terminate the RPC. set_code/set_details followed by a
        # return would arrive here as a successful empty response instead.
        stub = pricing_pb2_grpc.PricingStub(channel)
        with pytest.raises(grpc.RpcError) as excinfo:
            stub.PriceOrder(
                pricing_pb2.PriceOrderRequest(
                    sku="", quantity=1, unit_amount_cents=250
                ),
                timeout=5,
            )
        assert excinfo.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        details = excinfo.value.details()
        assert details is not None
        assert "sku" in details
