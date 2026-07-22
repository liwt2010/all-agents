#!/bin/bash
# Rebuild Docker image with latest code
# Usage: ./scripts/rebuild-docker.sh [tag]
set -e
TAG=${1:-v0.3.0}
DOCKER_IMAGE="liwt2010/all-agents"
echo "Building: ${DOCKER_IMAGE}:${TAG}"
docker build -t "${DOCKER_IMAGE}:${TAG}" -t "${DOCKER_IMAGE}:${TAG}-$(git rev-parse --short HEAD)" .
docker push "${DOCKER_IMAGE}:${TAG}"
docker push "${DOCKER_IMAGE}:${TAG}-$(git rev-parse --short HEAD)"
echo "Done: ${DOCKER_IMAGE}:${TAG}"
