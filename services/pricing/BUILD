python_sources(
    name="lib",
    sources=["pricing/**/*.py"],
    # Dependency inference resolves the `shop.v1.pricing_pb2*` imports back to the
    # protobuf_sources target, but it's declared explicitly here too since it's
    # cross-source-root (protos/ vs services/pricing/) and easy to miss on inference
    # cache invalidation.
    dependencies=["protos/shop/v1:protos"],
)

python_sources(
    name="tests_pkg",
    sources=["tests/__init__.py"],
)

python_tests(
    name="tests",
    sources=["tests/test_*.py"],
    # The `app` top-level package name collides with order-api's own `app` package
    # under the shared `/services/*` source root, so dependency inference can't
    # disambiguate `from pricing.main import ...` on its own.
    dependencies=[":lib"],
)

pex_binary(
    name="bin",
    tags=["deployable"],
    entry_point="pricing/main.py:main",
    dependencies=[":lib"],
    # The cluster runs linux/aarch64 CPython 3.13, not this macOS laptop. grpcio
    # is a native extension; without this, `pants package` resolves macOS wheels
    # that build and pass every check here, then fail to import in the cluster.
    complete_platforms=["3rdparty/python:linux-platform"],
    # Land in dist/ under a predictable name so dist/ can be used
    # directly as the Buildah build context and the Dockerfile can COPY by
    # name. Default would be dist/services.<name>/bin.pex.
    output_path="pricing.pex",
)
