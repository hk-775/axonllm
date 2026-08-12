#!/usr/bin/env bash
set -euo pipefail

readonly ORAS_VERSION="1.3.3"

usage() {
  printf 'usage: %s INSTALL_DIR\n' "$0" >&2
  exit 2
}

[[ $# -eq 1 ]] || usage
install_dir=$1
mkdir -p "${install_dir}"

case "$(uname -s):$(uname -m)" in
  Linux:x86_64)
    archive="oras_${ORAS_VERSION}_linux_amd64.tar.gz"
    sha256="9ce999f8d2de03fc03968b29d743077a58783e545e5eaa53917ca177352d0e59"
    ;;
  Linux:aarch64 | Linux:arm64)
    archive="oras_${ORAS_VERSION}_linux_arm64.tar.gz"
    sha256="ac7156f93a21e903f7ad606c792f3560f17e0cd0e36365634701b1e7cc4e4eca"
    ;;
  Darwin:x86_64)
    archive="oras_${ORAS_VERSION}_darwin_amd64.tar.gz"
    sha256="aeb684d8c24c18dce28fd1f7326636e4782b573108e244a93d4b1c4a5ec50f48"
    ;;
  Darwin:arm64)
    archive="oras_${ORAS_VERSION}_darwin_arm64.tar.gz"
    sha256="f33fc12753c54172b0d0d19eaa0318d3f90fe9b094d96e8b259c881713c92e1c"
    ;;
  *)
    printf 'unsupported registry-tool platform: %s %s\n' \
      "$(uname -s)" "$(uname -m)" >&2
    exit 1
    ;;
esac

work_dir=$(mktemp -d)
trap 'rm -rf "${work_dir}"' EXIT

curl --fail --silent --show-error --location \
  --connect-timeout 15 \
  --max-time 180 \
  --retry 8 --retry-all-errors --retry-max-time 120 \
  --output "${work_dir}/${archive}" \
  "https://github.com/oras-project/oras/releases/download/v${ORAS_VERSION}/${archive}"
if command -v sha256sum >/dev/null 2>&1; then
  printf '%s  %s\n' "${sha256}" "${work_dir}/${archive}" |
    sha256sum --check -
else
  printf '%s  %s\n' "${sha256}" "${work_dir}/${archive}" |
    shasum -a 256 --check
fi

tar -xzf "${work_dir}/${archive}" -C "${work_dir}" oras
install -m 0755 "${work_dir}/oras" "${install_dir}/oras"
"${install_dir}/oras" version
