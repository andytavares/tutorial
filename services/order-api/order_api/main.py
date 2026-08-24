import hashlib
import hmac
import json
import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import boto3
from aiokafka import AIOKafkaProducer
from botocore.config import Config as BotoConfig
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from prometheus_client import Counter, Histogram, make_asgi_app
from pydantic import BaseModel, Field

from .settings import settings

# ---------- logging ----------
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
)
log = logging.getLogger("order-api")

# ---------- metrics ----------
ORDERS_RECEIVED = Counter("orders_received_total", "Orders accepted by the API", ["result"])
ORDER_LATENCY = Histogram("order_ingest_duration_seconds", "Time to persist and publish one order")

# ---------- state ----------
state: dict = {"producer": None, "s3": None, "ready": False}


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Start dependencies before serving; drain them on shutdown."""
    state["s3"] = boto3.client(
        "s3",
        endpoint_url=settings.aws_endpoint_url,
        region_name=settings.aws_region,
        # Floci, like S3-compatible stores generally, needs path-style addressing.
        config=BotoConfig(s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
    )
    producer = AIOKafkaProducer(
        bootstrap_servers=settings.kafka_brokers,
        acks="all",  # do not consider a write done until all ISRs have it
        enable_idempotence=True,  # no duplicates on internal retry
        linger_ms=5,
    )
    await producer.start()
    state["producer"] = producer
    state["ready"] = True
    log.info("order-api started version=%s", settings.service_version)
    try:
        yield
    finally:
        state["ready"] = False
        await producer.stop()
        log.info("order-api stopped")


app = FastAPI(title="order-api", version=settings.service_version, lifespan=lifespan)


class OrderIn(BaseModel):
    customer: str = Field(min_length=1, max_length=128)
    sku: str = Field(min_length=1, max_length=64)
    quantity: int = Field(ge=1, le=1000)
    amount_cents: int = Field(ge=1)


@app.get("/healthz")
def healthz() -> dict:
    """Liveness: the process is running. Deliberately checks nothing else."""
    return {"status": "ok", "version": settings.service_version}


@app.get("/readyz")
def readyz() -> dict:
    """Readiness: dependencies are up. Kubernetes pulls us out of the Service if this fails."""
    if not state["ready"]:
        raise HTTPException(status_code=503, detail="dependencies not ready")
    return {"status": "ready"}


# prometheus_client ships an ASGI app for this; mounting it is what its docs
# prescribe for FastAPI. Mounting also keeps /metrics out of the OpenAPI schema,
# where a scrape endpoint has no business being.
app.mount("/metrics", make_asgi_app())


@app.post("/orders", status_code=202)
async def create_order(order: OrderIn) -> dict:
    started = time.perf_counter()
    order_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()

    payload = order.model_dump() | {"order_id": order_id, "created_at": created_at}
    body = json.dumps(payload, separators=(",", ":")).encode()

    # Sign the payload with a key that only ever exists in OpenBao.
    signature = hmac.new(settings.signing_key.encode(), body, hashlib.sha256).hexdigest()

    key = f"orders/{created_at[:10]}/{order_id}.json"
    try:
        # boto3 is synchronous. Calling it directly from an `async def` would
        # block the event loop for the whole S3 round trip — every other
        # in-flight request, and the liveness probe with them. FastAPI runs
        # plain `def` endpoints in a threadpool for exactly this reason; this
        # handler needs `await` for Kafka, so it reaches for the same threadpool
        # explicitly.
        await run_in_threadpool(
            state["s3"].put_object,
            Bucket=settings.s3_bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            Metadata={"signature": signature},
        )
        event = payload | {"s3_key": key, "signature": signature}
        await state["producer"].send_and_wait(
            settings.kafka_topic,
            value=json.dumps(event, separators=(",", ":")).encode(),
            key=order_id.encode(),  # partition by order id → per-order ordering
        )
    except Exception:
        ORDERS_RECEIVED.labels(result="error").inc()
        log.exception("failed to ingest order_id=%s", order_id)
        raise HTTPException(status_code=502, detail="downstream failure")
    finally:
        ORDER_LATENCY.observe(time.perf_counter() - started)

    ORDERS_RECEIVED.labels(result="ok").inc()
    log.info("accepted order_id=%s key=%s", order_id, key)
    return {"order_id": order_id, "status": "accepted", "s3_key": key}
