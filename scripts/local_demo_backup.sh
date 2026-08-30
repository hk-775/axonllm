#!/usr/bin/env bash
#
# Keep a repeatable, fully seeded local AxonLLM customer-demo backup ready.
#
# Usage:
#   ./scripts/local_demo_backup.sh start
#   ./scripts/local_demo_backup.sh status
#   ./scripts/local_demo_backup.sh logs
#   ./scripts/local_demo_backup.sh stop

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/compose.local-demo.yml"
PORT="${AXON_LOCAL_DEMO_PORT:-8001}"
BASE_URL="http://127.0.0.1:${PORT}"
IMAGE="${AXON_LOCAL_DEMO_IMAGE:-axonllm-local-demo:codex-compat}"
COMMAND="${1:-status}"

export AXON_LOCAL_DEMO_IMAGE="$IMAGE"
export AXON_LOCAL_DEMO_PORT="$PORT"
export AXON_LOCAL_GID="${AXON_LOCAL_GID:-$(id -g)}"
export AXON_LOCAL_UID="${AXON_LOCAL_UID:-$(id -u)}"

COMPOSE=(
    docker compose
    --project-directory "$ROOT_DIR"
    --file "$COMPOSE_FILE"
)

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "Required command not found: $1" >&2
        exit 1
    fi
}

seed_summary() {
    curl -fsS --max-time 10 "${BASE_URL}/admin/overview" |
        python3 -c '
import json
import sys

data = json.load(sys.stdin)
actual = {
    "projects": data.get("active_projects"),
    "requests": data.get("total_requests"),
    "spend": round(float(data.get("total_cost", 0.0)), 2),
    "users": data.get("active_users"),
}
minimum = {"projects": 2, "requests": 66, "spend": 1.26, "users": 3}
if any(actual[name] < value for name, value in minimum.items()):
    raise SystemExit(f"seed below expected minimum: {minimum}, got {actual}")
print(
    "Seed verified: {requests} requests, ${spend:.2f}, "
    "{projects} projects, {users} users".format(**actual)
)
'
}

provider_summary() {
    curl -fsS --max-time 10 "${BASE_URL}/api/models" |
        python3 -c '
import json
import sys

models = json.load(sys.stdin)
providers = sorted(
    {
        provider
        for model in models
        for provider in model.get("providers", [])
    }
)
print(
    "Models: {count} across {providers}".format(
        count=len(models),
        providers=", ".join(providers),
    )
)
'
}

show_urls() {
    echo "Landing:    ${BASE_URL}/"
    echo "Dashboard:  ${BASE_URL}/admin/dashboard"
    echo "Chat:       ${BASE_URL}/chat"
    echo "Playground: ${BASE_URL}/playground"
    echo "Traces:     ${BASE_URL}/admin/traces"
}

wait_until_ready() {
    for _ in $(seq 1 120); do
        if curl -fsS --max-time 2 "${BASE_URL}/health" >/dev/null 2>&1; then
            seed_summary
            provider_summary
            return
        fi
        if ! "${COMPOSE[@]}" ps --status running --quiet | grep -q .; then
            echo "Local AxonLLM demo container exited during startup." >&2
            "${COMPOSE[@]}" logs --tail 100 >&2
            exit 1
        fi
        sleep 0.5
    done
    echo "Local AxonLLM demo did not become healthy within 60 seconds." >&2
    "${COMPOSE[@]}" logs --tail 100 >&2
    exit 1
}

ensure_image() {
    if docker image inspect "$IMAGE" >/dev/null 2>&1; then
        return
    fi
    echo "Building local Linux/AMD64 demo image ${IMAGE}..."
    docker buildx build \
        --platform linux/amd64 \
        --provenance=false \
        --load \
        --tag "$IMAGE" \
        "$ROOT_DIR"
}

start_demo() {
    require_command curl
    require_command docker
    require_command python3
    docker info >/dev/null
    ensure_image
    "${COMPOSE[@]}" up --detach --no-build --remove-orphans
    wait_until_ready
    echo "Local AxonLLM demo is ready and managed by Docker."
    show_urls
}

stop_demo() {
    require_command docker
    "${COMPOSE[@]}" down --remove-orphans
    echo "Local AxonLLM demo stopped."
}

status_demo() {
    require_command curl
    require_command docker
    require_command python3
    if ! "${COMPOSE[@]}" ps --status running --quiet | grep -q .; then
        echo "Local AxonLLM demo container is not running."
        exit 1
    fi
    if ! curl -fsS --max-time 2 "${BASE_URL}/health" >/dev/null 2>&1; then
        echo "Local AxonLLM demo container is running but not healthy." >&2
        exit 1
    fi
    echo "Local AxonLLM demo is healthy."
    seed_summary
    provider_summary
    show_urls
}

case "$COMMAND" in
    start)
        start_demo
        ;;
    stop)
        stop_demo
        ;;
    restart)
        stop_demo
        start_demo
        ;;
    rebuild)
        docker buildx build \
            --platform linux/amd64 \
            --provenance=false \
            --load \
            --tag "$IMAGE" \
            "$ROOT_DIR"
        "${COMPOSE[@]}" up --detach --no-build --force-recreate --remove-orphans
        wait_until_ready
        echo "Local AxonLLM demo rebuilt and ready."
        show_urls
        ;;
    status)
        status_demo
        ;;
    logs)
        "${COMPOSE[@]}" logs --follow --tail 100
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|rebuild|status|logs}" >&2
        exit 2
        ;;
esac
