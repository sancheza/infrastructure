#!/usr/bin/env bash
set -e

docker run --rm -i \
  --net=host \
  --entrypoint "" \
  -v /opt/infra/infrastructure/services/vault-provision:/workspace \
  -v /root/.ssh:/root/.ssh:ro \
  -v /opt/infra/tfstate:/tfstate \
  -w /workspace \
  infra-runner:latest "$@"
