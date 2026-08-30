#!/usr/bin/env bash
#
# Keep a repeatable, fully seeded local AxonLLM customer-demo backup ready.
#
# Usage:
#   ./scripts/local_demo_backup.sh start
#   ./scripts/local_demo_backup.sh status
#   ./scripts/local_demo_backup.sh logs
#   ./scripts/local_demo_backup.sh stop
#   ./scripts/local_demo_backup.sh personas
#   ./scripts/local_demo_backup.sh copy-key tenant-acme

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/compose.local-demo.yml"
PORT="${AXON_LOCAL_DEMO_PORT:-8001}"
BASE_URL="http://127.0.0.1:${PORT}"
IMAGE="${AXON_LOCAL_DEMO_IMAGE:-axonllm-local-demo:codex-compat}"
COMMAND="${1:-status}"
ACCESS_DIR="${AXON_LOCAL_DEMO_ACCESS_DIR:-${ROOT_DIR}/.demo}"
ACCESS_FILE="${ACCESS_DIR}/personas.json"
DEFAULT_TENANT="${AXON_LOCAL_DEMO_TENANT:-tenant-acme}"

export AXON_LOCAL_DEMO_IMAGE="$IMAGE"
export AXON_LOCAL_DEMO_PORT="$PORT"
export AXON_LOCAL_GID="${AXON_LOCAL_GID:-$(id -g)}"
export AXON_LOCAL_UID="${AXON_LOCAL_UID:-$(id -u)}"
export AXON_LOCAL_DEMO_ACCESS_DIR="$ACCESS_DIR"

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

demo_key() {
    local tenant_id="${1:-$DEFAULT_TENANT}"
    python3 - "$ACCESS_FILE" "$tenant_id" <<'PY'
import json
import sys

path, tenant_id = sys.argv[1:]
with open(path, encoding="utf-8") as stream:
    document = json.load(stream)
for persona in document.get("personas", []):
    if persona.get("tenant_id") == tenant_id:
        key = persona.get("api_key")
        if isinstance(key, str) and key:
            print(key, end="")
            raise SystemExit(0)
raise SystemExit(f"No demo credential found for tenant {tenant_id!r}")
PY
}

authorized_curl() {
    local tenant_id="${1:-$DEFAULT_TENANT}"
    local url="$2"
    local key
    key="$(demo_key "$tenant_id")"
    curl -fsS --max-time 10 \
        -H "Authorization: Bearer ${key}" \
        "$url"
}

seed_summary() {
    authorized_curl tenant-acme "${BASE_URL}/admin/overview" |
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
    authorized_curl "$DEFAULT_TENANT" "${BASE_URL}/api/models" |
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

tenant_summary() {
    local tenant_id
    for tenant_id in tenant-acme tenant-globex; do
        authorized_curl "$tenant_id" "${BASE_URL}/admin/session"
        authorized_curl "$tenant_id" "${BASE_URL}/admin/projects"
    done |
        python3 -c '
import json
import sys

decoder = json.JSONDecoder()
text = sys.stdin.read()
documents = []
while text.strip():
    value, offset = decoder.raw_decode(text)
    documents.append(value)
    text = text[offset:]
if len(documents) != 4:
    raise SystemExit("expected two tenant sessions and two project lists")
acme_session, acme_projects, globex_session, globex_projects = documents
if acme_session.get("tenant_id") != "tenant-acme":
    raise SystemExit("Acme credential resolved to the wrong tenant")
if globex_session.get("tenant_id") != "tenant-globex":
    raise SystemExit("Globex credential resolved to the wrong tenant")
acme_alpha = next(
    project for project in acme_projects
    if project.get("project_id") == "proj-alpha"
)
globex_alpha = next(
    project for project in globex_projects
    if project.get("project_id") == "proj-alpha"
)
if acme_alpha.get("name") == globex_alpha.get("name"):
    raise SystemExit("tenant project namespaces are not isolated")
print(
    "Tenants verified: tenant-acme and tenant-globex each resolve "
    "their own proj-alpha"
)
'
}

show_urls() {
    echo "Landing:    ${BASE_URL}/"
    echo "Dashboard:  ${BASE_URL}/admin/dashboard"
    echo "Chat:       ${BASE_URL}/chat"
    echo "Playground: ${BASE_URL}/playground"
    echo "Traces:     ${BASE_URL}/admin/traces"
    echo "Credentials: ${ACCESS_FILE}"
    echo "Copy Acme key:   $0 copy-key tenant-acme"
    echo "Copy Globex key: $0 copy-key tenant-globex"
}

wait_for_credentials() {
    for _ in $(seq 1 120); do
        if [ -s "$ACCESS_FILE" ]; then
            chmod 600 "$ACCESS_FILE"
            tenant_summary
            return
        fi
        if "${COMPOSE[@]}" ps --all demo-access |
            grep -Eq "Exit [1-9]|exited \\([1-9]"; then
            echo "Local demo credential bootstrap failed." >&2
            "${COMPOSE[@]}" logs --tail 100 demo-access >&2
            exit 1
        fi
        sleep 0.5
    done
    echo "Local demo credentials were not created within 60 seconds." >&2
    "${COMPOSE[@]}" logs --tail 100 demo-access >&2
    exit 1
}

wait_until_ready() {
    for _ in $(seq 1 120); do
        if curl -fsS --max-time 2 "${BASE_URL}/health" >/dev/null 2>&1; then
            wait_for_credentials
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
    local app_healthy=false
    if (
        "${COMPOSE[@]}" ps --status running --quiet axonllm |
            grep -q .
    ) && curl -fsS --max-time 2 "${BASE_URL}/health" >/dev/null 2>&1; then
        app_healthy=true
    fi
    if [ "$app_healthy" = "true" ] && [ -s "$ACCESS_FILE" ]; then
        echo "Local AxonLLM demo is already ready."
        status_demo
        return
    fi
    mkdir -p "$ACCESS_DIR"
    chmod 700 "$ACCESS_DIR"
    rm -f "$ACCESS_FILE"
    if [ "$app_healthy" = "true" ]; then
        "${COMPOSE[@]}" up \
            --detach \
            --no-build \
            --force-recreate \
            demo-access
        wait_until_ready
        echo "Local AxonLLM demo credentials were recovered."
        show_urls
        return
    fi
    ensure_image
    "${COMPOSE[@]}" up \
        --detach \
        --no-build \
        --force-recreate \
        --remove-orphans \
        dynamodb-local \
        dynamodb-init \
        axonllm
    "${COMPOSE[@]}" up \
        --detach \
        --no-build \
        --force-recreate \
        demo-access
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
    if [ ! -s "$ACCESS_FILE" ]; then
        echo "Local demo credential file is missing." >&2
        exit 1
    fi
    echo "Local AxonLLM demo is healthy."
    tenant_summary
    seed_summary
    provider_summary
    show_urls
}

list_personas() {
    require_command python3
    python3 - "$ACCESS_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    document = json.load(stream)
for persona in document.get("personas", []):
    print(
        "{tenant_id}: {label} (project {project_id})".format(**persona)
    )
PY
}

copy_key() {
    local tenant_id="${2:-$DEFAULT_TENANT}"
    require_command pbcopy
    require_command python3
    demo_key "$tenant_id" | pbcopy
    echo "Copied the ${tenant_id} demo key to the clipboard."
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
        mkdir -p "$ACCESS_DIR"
        chmod 700 "$ACCESS_DIR"
        rm -f "$ACCESS_FILE"
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
    personas)
        list_personas
        ;;
    copy-key)
        copy_key "$@"
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|rebuild|status|logs|personas|copy-key [tenant]}" >&2
        exit 2
        ;;
esac
