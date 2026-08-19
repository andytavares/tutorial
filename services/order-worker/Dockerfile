# syntax=docker/dockerfile:1.7
FROM docker.io/library/golang:1.26-alpine AS builder
WORKDIR /src

COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download

COPY . .
# CGO_ENABLED=0 → a static binary, which is the only kind that runs on
# distroless/static. -trimpath and -s -w strip build paths and debug info for a
# smaller, more reproducible artifact.
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=linux go build \
      -trimpath \
      -ldflags="-s -w" \
      -o /out/order-worker .


FROM gcr.io/distroless/static-debian12:nonroot
# --chmod=0755, not a bare COPY. Once the binary is built outside this Dockerfile
# and shipped in as a CI artifact (Phase 3), the executable bit does not survive
# the transfer, and the pod fails at `exec: "/order-worker": permission denied`
# with no application output at all. Setting it here is correct either way.
COPY --chmod=0755 --from=builder /out/order-worker /order-worker
USER 65532:65532
EXPOSE 9090
ENTRYPOINT ["/order-worker"]