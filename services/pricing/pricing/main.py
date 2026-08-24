import logging
import signal
import sys
import time
from concurrent import futures
from types import FrameType

import grpc

# typeshed publishes types-grpcio-health-checking and types-grpcio-reflection,
# but neither is in 3rdparty/python/requirements.txt, so mypy has nothing to
# read. The ignore is per-import and per-error-code: it silences these two
# packages and nothing else.
from grpc_health.v1 import (  # type: ignore[import-untyped]
    health,
    health_pb2,
    health_pb2_grpc,
)
from grpc_reflection.v1alpha import reflection  # type: ignore[import-untyped]
from prometheus_client import Counter, Histogram, start_http_server
from shop.v1 import pricing_pb2, pricing_pb2_grpc

from .settings import settings  # pants: no-infer-dep (already colocated in this target)

# ---------- logging ----------
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
)
log = logging.getLogger("pricing")

# ---------- metrics ----------
PRICING_REQUESTS = Counter(
    "pricing_requests_total",
    "Priced orders, by rule and serving version",
    ["rule", "version"],
)
PRICING_DURATION = Histogram(
    "pricing_request_duration_seconds", "Time to price one order"
)

# Service names are read off the generated descriptors rather than written out
# as string literals, so renaming a service in the .proto cannot leave a stale
# name registered with health checking or reflection.
PRICING_SERVICE_NAME = pricing_pb2.DESCRIPTOR.services_by_name["Pricing"].full_name
HEALTH_SERVICE_NAME = health_pb2.DESCRIPTOR.services_by_name["Health"].full_name

# Seconds in-flight RPCs get to finish after SIGTERM before they are aborted.
SHUTDOWN_GRACE_SECONDS = 5


def calculate_price(
    quantity: int, unit_amount_cents: int, version: str
) -> tuple[int, int, str]:
    """Pure pricing logic, version-switched.

    v1: list price, no discount.
    v2: same, but a 3+ quantity line gets 10% off (integer math, rounded down).
    """
    list_total = unit_amount_cents * quantity
    discount_cents = 0
    rule_applied = "list-price"
    if version == "v2" and quantity >= 3:
        discount_cents = (list_total * 10) // 100
        rule_applied = "bulk-10pct"
    return list_total - discount_cents, discount_cents, rule_applied


def validate_request(sku: str, quantity: int, unit_amount_cents: int) -> None:
    """Raises ValueError with a useful message on invalid input."""
    if not sku:
        raise ValueError("sku must not be empty")
    if quantity < 1:
        raise ValueError(f"quantity must be >= 1, got {quantity}")
    if unit_amount_cents < 1:
        raise ValueError(f"unit_amount_cents must be >= 1, got {unit_amount_cents}")


class PricingServicer(pricing_pb2_grpc.PricingServicer):
    def PriceOrder(
        self, request: pricing_pb2.PriceOrderRequest, context: grpc.ServicerContext
    ) -> pricing_pb2.PriceOrderResponse:
        started = time.perf_counter()
        try:
            validate_request(request.sku, request.quantity, request.unit_amount_cents)
        except ValueError as exc:
            # abort() raises, so it terminates the RPC in one call and cannot be
            # followed by an accidental successful return.
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))

        total_amount_cents, discount_cents, rule_applied = calculate_price(
            request.quantity, request.unit_amount_cents, settings.version
        )

        PRICING_REQUESTS.labels(rule=rule_applied, version=settings.version).inc()
        PRICING_DURATION.observe(time.perf_counter() - started)
        log.info(
            "priced sku=%s quantity=%d rule=%s served_by=%s",
            request.sku,
            request.quantity,
            rule_applied,
            settings.version,
        )
        return pricing_pb2.PriceOrderResponse(
            total_amount_cents=total_amount_cents,
            discount_cents=discount_cents,
            rule_applied=rule_applied,
            served_by=settings.version,
        )


def build_server() -> tuple[grpc.Server, health.HealthServicer]:
    """Assemble the gRPC server: pricing, health checking and reflection.

    Nothing here binds a port or starts a thread, so tests can build the exact
    server the process runs and drive it on an ephemeral port.
    """
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    pricing_pb2_grpc.add_PricingServicer_to_server(PricingServicer(), server)

    # The non-blocking servicer answers Watch from its own pool instead of
    # parking a request thread per watcher, which is what the gRPC health
    # example uses.
    health_servicer = health.HealthServicer(
        experimental_non_blocking=True,
        experimental_thread_pool=futures.ThreadPoolExecutor(max_workers=10),
    )
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    # The protocol requires the server to register every service explicitly,
    # including the empty service name that carries overall server health. A
    # name that is not registered gets NOT_FOUND, not NOT_SERVING, so anything
    # a probe may ask for has to be set here.
    for service_name in ("", PRICING_SERVICE_NAME):
        health_servicer.set(service_name, health_pb2.HealthCheckResponse.SERVING)

    # Python reflection has no automatic service discovery: every service the
    # server exposes has to be named, including reflection itself. Without this,
    # grpcurl needs a local copy of the .proto to call anything.
    reflection.enable_server_reflection(
        (PRICING_SERVICE_NAME, HEALTH_SERVICE_NAME, reflection.SERVICE_NAME),
        server,
    )
    return server, health_servicer


def serve() -> None:
    server, health_servicer = build_server()
    server.add_insecure_port(f"[::]:{settings.grpc_port}")

    # prometheus_client's own server, in a daemon thread, is the documented way
    # to expose metrics from a process that is not a web application.
    start_http_server(settings.http_port)
    server.start()

    log.info(
        "pricing started version=%s grpc_port=%d metrics_port=%d",
        settings.version,
        settings.grpc_port,
        settings.http_port,
    )

    def handle_sigterm(signum: int, frame: FrameType | None) -> None:
        log.info("received signal %d, shutting down", signum)
        # Flips every registered service to NOT_SERVING and fails later Check
        # calls, so readiness probes see the pod leaving before the listener
        # disappears.
        health_servicer.enter_graceful_shutdown()
        server.stop(SHUTDOWN_GRACE_SECONDS)

    signal.signal(signal.SIGTERM, handle_sigterm)

    server.wait_for_termination()
    log.info("pricing stopped")


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
