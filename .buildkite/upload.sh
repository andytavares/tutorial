#!/bin/sh
# Generate, validate, keep a copy, then upload. The dry-run is what a static
# pipeline gives you for free and a generated one does not: it rejects invalid
# YAML before any of it becomes real steps.
set -eu

.buildkite/pipeline.sh > /tmp/pipeline.yml

buildkite-agent pipeline upload --dry-run < /tmp/pipeline.yml > /dev/null
buildkite-agent artifact upload /tmp/pipeline.yml
buildkite-agent pipeline upload /tmp/pipeline.yml