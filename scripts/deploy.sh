#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

ACTION="${1:-deploy}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
SERVICE="${SERVICE:-skill-api}"
CONTAINER_NAME="${CONTAINER_NAME:-skill-api}"
IMAGE_NAME="${SKILL_API_IMAGE:-${IMAGE_NAME:-skill-api:latest}}"
GIT_REMOTE="${GIT_REMOTE:-origin}"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-dev}"
MINERU_CONTAINER_NAME="${MINERU_CONTAINER_NAME:-mineru}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-300}"
LOG_TAIL="${LOG_TAIL:-200}"

cd "${PROJECT_DIR}"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

die() {
    log "ERROR: $*" >&2
    exit 1
}

compose() {
    docker compose -f "${COMPOSE_FILE}" "$@"
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

validate_common() {
    require_command docker
    [[ -f "${COMPOSE_FILE}" ]] || die "Compose file not found: ${COMPOSE_FILE}"
    [[ -f .env ]] || die ".env not found. Create it from .env.example and fill production secrets."
    docker info >/dev/null 2>&1 || die "Docker daemon is unavailable or current user has no permission"
    compose config --quiet
    # 生产安全基线（fail-closed）：deploy 版 compose 必须携带鉴权配置，
    # 否则拒绝部署，避免无鉴权端口暴露。限流暂不强制（RATE_LIMIT_ENABLED 仍可自行开启）。
    case "${COMPOSE_FILE}" in
        *deploy*)
            if ! grep -qE '^[[:space:]]*API_KEY=[^[:space:]]' .env; then
                die "API_KEY is not set in .env; refusing to deploy an unauthenticated service"
            fi
            ;;
    esac
}

wait_for_health() {
    local container_name="$1"
    local deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
    local status

    while ((SECONDS < deadline)); do
        status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container_name}" 2>/dev/null || true)"
        case "${status}" in
            healthy)
                return 0
                ;;
            unhealthy|exited|dead)
                log "Container state: ${status}"
                return 1
                ;;
        esac
        sleep 2
    done

    log "Health check timed out after ${HEALTH_TIMEOUT_SECONDS}s"
    return 1
}

start_and_verify() {
    compose up -d --no-build --remove-orphans
    log "Waiting for ${MINERU_CONTAINER_NAME} health check"
    wait_for_health "${MINERU_CONTAINER_NAME}"
    log "Waiting for ${CONTAINER_NAME} health check"
    wait_for_health "${CONTAINER_NAME}"
}

rollback_image() {
    local old_image_id="$1"
    local old_mineru_image_id="${2:-}"
    local old_mineru_image_ref="${3:-}"

    [[ -n "${old_image_id}" ]] || return 1
    log "Restoring previous image ${old_image_id}"
    compose down --remove-orphans || true
    docker tag "${old_image_id}" "${IMAGE_NAME}"
    if [[ -n "${old_mineru_image_id}" && -n "${old_mineru_image_ref}" ]]; then
        docker tag "${old_mineru_image_id}" "${old_mineru_image_ref}"
    fi
    start_and_verify
}

deploy() {
    local current_branch
    local old_image_id
    local old_mineru_image_id
    local old_mineru_image_ref
    local old_revision

    require_command git
    require_command flock
    validate_common

    exec 9>"${DEPLOY_LOCK_FILE:-/tmp/skill-api-deploy.lock}"
    flock -n 9 || die "Another deployment is already running"

    current_branch="$(git branch --show-current)"
    [[ "${current_branch}" == "${DEPLOY_BRANCH}" ]] || die "Current branch is ${current_branch:-detached}; expected ${DEPLOY_BRANCH}"
    [[ -z "$(git status --porcelain --untracked-files=no)" ]] || die "Tracked files have local changes; refusing to overwrite a production checkout"

    old_revision="$(git rev-parse --short HEAD)"
    old_image_id="$(docker image inspect "${IMAGE_NAME}" --format '{{.Id}}' 2>/dev/null || true)"
    old_mineru_image_id="$(docker inspect --format '{{.Image}}' "${MINERU_CONTAINER_NAME}" 2>/dev/null || true)"
    old_mineru_image_ref="$(docker inspect --format '{{.Config.Image}}' "${MINERU_CONTAINER_NAME}" 2>/dev/null || true)"

    log "Pulling ${GIT_REMOTE}/${DEPLOY_BRANCH} (current revision: ${old_revision})"
    git pull --ff-only "${GIT_REMOTE}" "${DEPLOY_BRANCH}"

    # Validate the newly pulled definition before touching the running container.
    compose config --quiet
    mkdir -p storage
    log "Building skill-api and MinerU images"
    compose build --pull

    log "Stopping old container"
    compose down --remove-orphans

    log "Starting new container"
    if start_and_verify; then
        log "Deployment succeeded: $(git rev-parse --short HEAD)"
        compose ps
        return 0
    fi

    log "New container failed its health check"
    compose logs --tail="${LOG_TAIL}" || true
    if rollback_image "${old_image_id}" "${old_mineru_image_id}" "${old_mineru_image_ref}"; then
        die "Deployment failed; the previous image has been restored"
    fi
    die "Deployment failed and automatic rollback was unavailable; inspect container logs"
}

case "${ACTION}" in
    deploy)
        deploy
        ;;
    start)
        validate_common
        start_and_verify
        compose ps
        ;;
    stop)
        validate_common
        compose down --remove-orphans
        ;;
    restart)
        validate_common
        compose restart
        wait_for_health "${MINERU_CONTAINER_NAME}" || {
            compose logs --tail="${LOG_TAIL}" mineru || true
            die "MinerU failed its health check after restart"
        }
        wait_for_health "${CONTAINER_NAME}" || {
            compose logs --tail="${LOG_TAIL}" "${SERVICE}" || true
            die "Container failed its health check after restart"
        }
        compose ps
        ;;
    status)
        validate_common
        compose ps
        ;;
    logs)
        validate_common
        compose logs --tail="${LOG_TAIL}" -f
        ;;
    *)
        die "Usage: $0 {deploy|start|stop|restart|status|logs}"
        ;;
esac
