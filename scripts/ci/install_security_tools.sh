#!/usr/bin/env bash
set -euo pipefail

readonly GITLEAKS_VERSION="8.30.1"
readonly TRIVY_VERSION="0.72.0"

usage() {
  printf 'usage: %s INSTALL_DIR\n' "$0" >&2
  exit 2
}

[[ $# -eq 1 ]] || usage
install_dir=$1
mkdir -p "${install_dir}"

case "$(uname -s):$(uname -m)" in
  Linux:x86_64)
    gitleaks_archive="gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"
    gitleaks_sha256="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
    trivy_archive="trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz"
    trivy_sha256="bbb64b9695866ce4a7a8f5c9592002c5961cab378577fa3f8a040df362b9b2ea"
    ;;
  Linux:aarch64 | Linux:arm64)
    gitleaks_archive="gitleaks_${GITLEAKS_VERSION}_linux_arm64.tar.gz"
    gitleaks_sha256="e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080"
    trivy_archive="trivy_${TRIVY_VERSION}_Linux-ARM64.tar.gz"
    trivy_sha256="2ca2c023109c2db6b2b77366b6717291452d4531167377d95c79547f0c8e3467"
    ;;
  Darwin:x86_64)
    gitleaks_archive="gitleaks_${GITLEAKS_VERSION}_darwin_x64.tar.gz"
    gitleaks_sha256="dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709"
    trivy_archive="trivy_${TRIVY_VERSION}_macOS-64bit.tar.gz"
    trivy_sha256="ee5e60df8a98e5b89fd74a6d86f9e5c7e9a266a35002cb1e43291698b3bfee08"
    ;;
  Darwin:arm64)
    gitleaks_archive="gitleaks_${GITLEAKS_VERSION}_darwin_arm64.tar.gz"
    gitleaks_sha256="b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5"
    trivy_archive="trivy_${TRIVY_VERSION}_macOS-ARM64.tar.gz"
    trivy_sha256="88f208680dc05da2b459e19b4f5aa2b4dc7c2117892ba4aab2ae63baba330016"
    ;;
  *)
    printf 'unsupported scanner platform: %s %s\n' "$(uname -s)" "$(uname -m)" >&2
    exit 1
    ;;
esac

work_dir=$(mktemp -d)
trap 'rm -rf "${work_dir}"' EXIT

download_and_verify() {
  local url=$1
  local archive=$2
  local sha256=$3

  curl --fail --silent --show-error --location \
    --retry 3 --retry-all-errors \
    --output "${work_dir}/${archive}" \
    "${url}/${archive}"
  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s  %s\n' "${sha256}" "${work_dir}/${archive}" | sha256sum --check -
  else
    printf '%s  %s\n' "${sha256}" "${work_dir}/${archive}" | shasum -a 256 --check
  fi
}

download_and_verify \
  "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}" \
  "${gitleaks_archive}" \
  "${gitleaks_sha256}"
tar -xzf "${work_dir}/${gitleaks_archive}" -C "${work_dir}" gitleaks
install -m 0755 "${work_dir}/gitleaks" "${install_dir}/gitleaks"

download_and_verify \
  "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}" \
  "${trivy_archive}" \
  "${trivy_sha256}"
tar -xzf "${work_dir}/${trivy_archive}" -C "${work_dir}" trivy
install -m 0755 "${work_dir}/trivy" "${install_dir}/trivy"

"${install_dir}/gitleaks" version
"${install_dir}/trivy" --version
