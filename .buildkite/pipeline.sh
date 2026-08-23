#!/bin/sh
# Emits this build's pipeline as YAML on stdout. It talks to nothing and
# changes nothing - run it locally and read what it prints.
#
# POSIX sh, deliberately: this runs in the default agent image, whose only
# guaranteed shell is /bin/sh.
set -eu

REGISTRY="nexus:8082"
# A build triggered by hand in the UI sets BUILDKITE_COMMIT to the literal
# string "HEAD" rather than a SHA, which would tag images `:HEAD` — a mutable
# tag that means a different image on every build, and the exact thing §10.3
# says a tag must never be. Resolve it to a real SHA before it reaches a tag.
if [ "$BUILDKITE_COMMIT" = "HEAD" ]; then
  BUILDKITE_COMMIT="$(git rev-parse HEAD)"
fi
SHA="$(echo "$BUILDKITE_COMMIT" | cut -c1-12)"

case "$SHA" in
  *[!0-9a-f]*|"") echo "refusing to build: BUILDKITE_COMMIT is not a SHA ($BUILDKITE_COMMIT)" >&2; exit 1 ;;
esac

# Services are discovered, not listed. A directory under services/ with a
# Dockerfile in it is a service, and that is the entire contract. This is what
# lets the Backstage paved path (§14.6) add a service without touching CI.
SERVICES="$(cd services && ls -d */ 2>/dev/null | sed 's#/##' | while read -r s; do
  [ -f "$s/Dockerfile" ] && echo "$s"
done | sort | tr '\n' ' ')"

if [ -z "$SERVICES" ]; then
  echo "no services found under services/*/Dockerfile" >&2
  exit 1
fi

# ── LOOP GUARD ─────────────────────────────────────────────────────────────
# The deploy step at the bottom commits into the repo we build from, so
# without this every deploy triggers a build that makes another deploy,
# forever. Deciding it here, before any step exists, is the whole reason this
# is a script.
if git log -1 --pretty=%s | grep -q '^chore(deploy):'; then
  cat <<'YAML'
steps:
  - label: ":fast_forward: deploy commit, nothing to build"
    agents: { queue: kubernetes }
    command: "echo 'HEAD is a deploy commit; skipping'"
YAML
  exit 0
fi

# ── Header ─────────────────────────────────────────────────────────────────
# Every step runs in its own Kubernetes pod, created by agent-stack-k8s.
# The `kubernetes` plugin is how a step describes the pod it wants.
cat <<'YAML'
env:
  # Nexus proxies. Builds never talk to pypi.org or proxy.golang.org directly:
  # that is the supply-chain choke point from §5.1, made real.
  # pip needs these to install uv itself. uv does NOT get its index from here:
  # it reads [[tool.uv.index]] in services/order-api/pyproject.toml, so the
  # registry is recorded in uv.lock and `--locked` validates the same way on a
  # laptop and in this pod. Setting UV_DEFAULT_INDEX here instead would put the
  # index in CI only, and the committed lock would never match it.
  PIP_INDEX_URL: "http://nexus:8081/repository/pypi-proxy/simple"
  PIP_TRUSTED_HOST: "nexus"
  GOPROXY: "http://nexus:8081/repository/go-proxy"
  # Nothing disables checksum verification. go.sum records a hash for every
  # module in the build list, so the go command verifies against it locally and
  # never reaches for the checksum database. GOSUMDB=off would not help here —
  # it would only disarm verification for the next dependency added.
  GOFLAGS: "-mod=readonly"

steps:
  # ── 1. Tests, in parallel ────────────────────────────────────────────────
  - label: ":python: test order-api"
    key: test-api
    agents: { queue: kubernetes }
    plugins:
      - kubernetes:
          podSpec:
            containers:
              - image: python:3.13-slim
                command:
                  - |
                    set -euo pipefail
                    cd services/order-api
                    pip install --quiet uv
                    uv sync --locked --dev
                    uv run ruff check .
                    uv run pytest -q

  - label: ":go: test order-worker"
    key: test-worker
    agents: { queue: kubernetes }
    plugins:
      - kubernetes:
          podSpec:
            containers:
              # Debian, not alpine: `go test -race` needs cgo, and the alpine
              # image ships CGO_ENABLED=0 with no C toolchain. Adding gcc and
              # musl-dev to alpine also works; this is one word instead.
              - image: golang:1.26
                command:
                  - |
                    set -euo pipefail
                    cd services/order-worker
                    go vet ./...
                    go test -race ./...

  - wait
YAML

# `golang:1.26` and not `golang:1.26-alpine` for the TEST step specifically:
#
#   golang:1.26-alpine : CGO_ENABLED=0, no gcc
#   golang:1.26        : CGO_ENABLED=1, gcc 14.2.0, git 2.47.3
#
# `go test -race` is implemented with a C runtime, so on alpine it refuses with
# `go: -race requires cgo; enable cgo by setting CGO_ENABLED=1` — and setting
# that variable alone doesn't help, because there is no compiler to use. The
# Dockerfile in §3.2 still builds FROM golang:1.26-alpine, and should: there we
# *want* CGO_ENABLED=0 for a static binary in a scratch image. Test and build
# want opposite things from the same toolchain, which is why they differ.

# ── 2. Build and push images ───────────────────────────────────────────────
# One template, one step per service. When these were two hand-maintained
# YAML blocks they differed only in a name, which is exactly the kind of
# duplication that drifts without anyone noticing.
for SVC in $SERVICES; do
  # Only the Go image takes a version stamp (services/order-worker/Dockerfile
  # declares ARG VERSION); buildah warns about build-args the Dockerfile never
  # declares, so don't pass it to the Python one.
  case "$SVC" in
    order-worker) BUILD_ARGS="--build-arg \"VERSION=$SHA\" " ;;
    *)            BUILD_ARGS="" ;;
  esac

  cat <<YAML

  - label: ":docker: build $SVC ($SHA)"
    key: build-$SVC
    agents: { queue: kubernetes }
    plugins:
      - kubernetes:
          podSpec:
            volumes:
              - name: nexus-auth
                secret: { secretName: nexus-push }
            containers:
              - image: quay.io/buildah/stable:v1.40.1
                # Not privileged. Buildah documents chroot isolation plus the
                # vfs storage driver for running inside an unprivileged
                # container: chroot avoids CLONE_NEWUSER, and vfs avoids the
                # overlayfs mknod. What remains is the default OCI capability
                # set, which Buildah hands to each RUN step and therefore has to
                # hold itself. Naming those capabilities is the point — the pod
                # cannot load kernel modules, ptrace, or touch host devices, all
                # of which full privilege grants.
                securityContext:
                  privileged: false
                  allowPrivilegeEscalation: true
                  capabilities:
                    drop: ["ALL"]
                    add:
                      - SYS_ADMIN          # mount(2) for the build root
                      - SYS_CHROOT         # chroot isolation
                      - SETUID
                      - SETGID
                      - SETPCAP
                      - SETFCAP
                      - CHOWN
                      - DAC_OVERRIDE
                      - FOWNER
                      - FSETID
                      - MKNOD
                      - KILL
                      - NET_RAW
                      - NET_BIND_SERVICE
                      - AUDIT_WRITE
                  seccompProfile:
                    type: Unconfined
                env:
                  - name: STORAGE_DRIVER
                    value: vfs
                  - name: BUILDAH_ISOLATION
                    value: chroot
                  - name: BUILDAH_FORMAT
                    value: docker
                  - name: REGISTRY_AUTH_FILE
                    value: /auth/config.json
                volumeMounts:
                  - name: nexus-auth
                    mountPath: /auth
                    readOnly: true
                command:
                  - |
                    set -euo pipefail

                    # --tls-verify=false exists only because §5.8 runs Nexus
                    # on plain HTTP. It disables certificate verification on a
                    # connection that carries push credentials. Behind real TLS
                    # you delete the flag and mount the CA instead — it is not
                    # a Buildah setting you keep.
                    buildah bud \\
                      --tls-verify=false \\
                      ${BUILD_ARGS}--file services/$SVC/Dockerfile \\
                      --tag "$REGISTRY/shop/$SVC:$SHA" \\
                      services/$SVC

                    buildah push --tls-verify=false "$REGISTRY/shop/$SVC:$SHA"

                    echo "pushed $REGISTRY/shop/$SVC:$SHA"
YAML
done

# ── 3. The handoff to CD: write the tag into git ───────────────────────────
cat <<YAML

  - wait

  - label: ":git: bump image tags to $SHA"
    key: deploy
    branches: "main"
    agents: { queue: kubernetes }
    plugins:
      - kubernetes:
          checkout:
            gitCredentialsSecret:
              secretName: git-https-credentials
          podSpec:
            volumes:
              - name: git-creds
                secret: { secretName: git-https-credentials }
            containers:
              - image: alpine/git:2.47.2
                volumeMounts:
                  - name: git-creds
                    mountPath: /gitcreds
                    readOnly: true
                command:
                  - |
                    set -eu
                    git config user.name  "buildkite"
                    git config user.email "buildkite@localtest.me"
                    git config credential.helper "store --file=/gitcreds/.git-credentials"

                    # The overlay has exactly one job - carry the two tags - so
                    # rewriting it wholesale is deterministic and needs no yq.
                    # The tags are literals: pipeline.sh already resolved them.
                    cat > deploy/env/local/values.yaml <<'VALUES'
                    # Generated by Buildkite. Do not edit by hand.
                    orderApi:
                      image:
                        tag: "$SHA"
                    orderWorker:
                      image:
                        tag: "$SHA"
                    # Every scaffolded service (§14.6) is built from this same
                    # commit, so one tag covers all of them.
                    scaffolded:
                      tag: "$SHA"
                    VALUES

                    git add deploy/env/local/values.yaml
                    if git diff --cached --quiet; then
                      echo "no change; nothing to deploy"
                      exit 0
                    fi

                    git commit -m "chore(deploy): order-platform $SHA [skip ci]"
                    git push origin HEAD:main
                    echo "pushed deploy commit; Argo CD will sync"
YAML