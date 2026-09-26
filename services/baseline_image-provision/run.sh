#!/usr/bin/env bash
set -e

docker run --rm -i \
  --net=host \
  --entrypoint "" \
  -v /opt/infra/infrastructure/services/baseline_image-provision:/workspace \
  -v /root/.ssh:/root/.ssh:ro \
  -w /workspace \
  infra-runner:latest "$@"
