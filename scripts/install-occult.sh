#!/usr/bin/env sh
# Signed GitHub-first installer for the Occult System local public release.
#
# Download this file from the matching Hermes GitHub release before running it.
# Pipe-to-shell execution is rejected because the running file is compared with
# the signed release copy before any application files are installed.

set -eu

version="1.0.1"
install_root="${XDG_DATA_HOME:-$HOME/.local/share}/occult"
initialize_local=0
skip_council=0
verify_only=0
model="qwen2.5:3b"

usage() {
  cat <<'EOF'
Usage: install-occult.sh [options]

  --version VERSION       Occult GitHub release (default: 1.0.1)
  --install-root PATH     Per-user installation root
  --initialize-local      Explicitly pull the approved Ollama model and enable Occult
  --skip-council          Verify and install Hermes without Agents Council
  --verify-only           Verify signed assets without installing
  --model MODEL           Ollama model used with --initialize-local
  -h, --help              Show this help
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --version)
      [ "$#" -ge 2 ] || { echo "Missing value for --version" >&2; exit 2; }
      version=$2
      shift 2
      ;;
    --install-root)
      [ "$#" -ge 2 ] || { echo "Missing value for --install-root" >&2; exit 2; }
      install_root=$2
      shift 2
      ;;
    --initialize-local)
      initialize_local=1
      shift
      ;;
    --skip-council)
      skip_council=1
      shift
      ;;
    --verify-only)
      verify_only=1
      shift
      ;;
    --model)
      [ "$#" -ge 2 ] || { echo "Missing value for --model" >&2; exit 2; }
      model=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

fail() {
  echo "Occult installation stopped safely: $*" >&2
  exit 1
}

step() {
  printf '[Occult] %s\n' "$*"
}

case "$version" in
  v*) version=${version#v} ;;
esac
case "$version" in
  *[!0-9.]*)
    fail "--version must be a semantic version such as 1.0.1"
    ;;
esac
printf '%s\n' "$version" |
  awk -F. 'NF == 3 && $1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ { ok=1 } END { exit(ok ? 0 : 1) }' ||
  fail "--version must be a semantic version such as 1.0.1"
[ -n "$model" ] || fail "--model cannot be empty"
[ -f "$0" ] || fail "download the script to a file before running it; direct pipe-to-shell is not supported"

os_name=$(uname -s)
machine=$(uname -m)
case "$os_name:$machine" in
  Linux:x86_64|Linux:amd64)
    platform_key="linux-x64"
    uv_asset="uv-x86_64-unknown-linux-gnu.tar.gz"
    uv_sha256="e490a6464492183c5d4534a5527fb4440f7f2bb2f228162ad7e4afe076dc0224"
    ;;
  Linux:aarch64|Linux:arm64)
    platform_key="linux-arm64"
    uv_asset="uv-aarch64-unknown-linux-gnu.tar.gz"
    uv_sha256="03e9fe0a81b0718d0bc84625de3885df6cc3f89a8b6af6121d6b9f6113fb6533"
    ;;
  Darwin:x86_64|Darwin:amd64)
    platform_key="macos-x64"
    uv_asset="uv-x86_64-apple-darwin.tar.gz"
    uv_sha256="2ad79983127ffca7d77b77ce6a24278d7e4f7b817a1acf72fea5f8124b4aac5e"
    ;;
  Darwin:arm64|Darwin:aarch64)
    platform_key="macos-arm64"
    uv_asset="uv-aarch64-apple-darwin.tar.gz"
    uv_sha256="33540eb7c883ab857eff79bd5ac2aa31fe27b595abecb4a9c003a2c998447232"
    ;;
  *)
    fail "unsupported platform $os_name/$machine"
    ;;
esac

hermes_repository="SgtSlummy/hermes-agent"
council_repository="SgtSlummy/agents-council"
bootstrap_uv_version="0.11.28"
pinned_sigstore_version="4.5.0"
expected_issuer="https://token.actions.githubusercontent.com"
release_tag="v$version"
hermes_release_base="https://github.com/$hermes_repository/releases/download/$release_tag"

tmp_base=${TMPDIR:-/tmp}
tmp=$(mktemp -d "$tmp_base/occult-install.XXXXXX") ||
  fail "could not create a temporary verification directory"
cleanup() {
  case "$tmp" in
    "$tmp_base"/occult-install.*)
      rm -rf -- "$tmp"
      ;;
  esac
}
trap cleanup EXIT HUP INT TERM

download() {
  url=$1
  destination=$2
  label=$3
  if command -v curl >/dev/null 2>&1; then
    curl --fail --location --silent --show-error \
      --connect-timeout 20 --retry 2 \
      --output "$destination" "$url" ||
      fail "could not download $label; check the version, network connection, and GitHub status"
  elif command -v wget >/dev/null 2>&1; then
    wget --quiet --timeout=20 --tries=3 --output-document="$destination" "$url" ||
      fail "could not download $label; check the version, network connection, and GitHub status"
  else
    fail "curl or wget is required"
  fi
  [ -f "$destination" ] || fail "$label download did not create a file"
}

safe_asset_name() {
  value=$1
  case "$value" in
    ""|*/*|*\\*|*..*|*[!A-Za-z0-9._-]*)
      fail "release metadata contains an unsafe asset name"
      ;;
  esac
  printf '%s\n' "$value"
}

assert_safe_tar_archive() {
  archive=$1
  listing="$tmp/council-archive-entries.txt"
  tar -tzf "$archive" >"$listing" ||
    fail "Council archive could not be inspected safely"
  while IFS= read -r entry; do
    case "$entry" in
      /*|[A-Za-z]:*|*\\*)
        fail "Council archive contains an unsafe path"
        ;;
    esac
    case "/$entry/" in
      */../*)
        fail "Council archive contains an unsafe path"
        ;;
    esac
  done <"$listing"
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{ print $1 }'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{ print $1 }'
  else
    fail "sha256sum or shasum is required"
  fi
}

expected_hash() {
  checksum_file=$1
  asset_name=$2
  hash=$(
    awk -v target="$asset_name" '
      length($1) == 64 && $1 ~ /^[0-9a-fA-F]+$/ {
        name=$2
        sub(/^\*/, "", name)
        sub(/^\.\//, "", name)
        if (name == target) {
          print tolower($1)
          exit
        }
      }
    ' "$checksum_file"
  )
  [ -n "$hash" ] || fail "signed checksum manifest does not list $asset_name"
  printf '%s\n' "$hash"
}

verify_hash() {
  checksum_file=$1
  asset_name=$2
  file_path=$3
  expected=$(expected_hash "$checksum_file" "$asset_name")
  actual=$(sha256_file "$file_path")
  [ "$actual" = "$expected" ] || fail "SHA-256 verification failed for $asset_name"
  printf '%s\n' "$actual"
}

step "Bootstrapping verified uv $bootstrap_uv_version in the temporary verifier directory"
uv_archive="$tmp/$uv_asset"
download \
  "https://github.com/astral-sh/uv/releases/download/$bootstrap_uv_version/$uv_asset" \
  "$uv_archive" \
  "the pinned uv archive"
[ "$(sha256_file "$uv_archive")" = "$uv_sha256" ] ||
  fail "SHA-256 verification failed for the pinned uv archive"
uv_bin="$tmp/uv-bin"
mkdir -p "$uv_bin"
tar -xzf "$uv_archive" -C "$uv_bin" ||
  fail "the verified uv archive could not be extracted"
uv_cmd=$(find "$uv_bin" -type f -name uv -perm -u+x -print | sed -n '1p')
[ -n "$uv_cmd" ] && [ -x "$uv_cmd" ] ||
  fail "the verified uv archive did not contain the uv executable"
uv_version_output=$("$uv_cmd" --version)
case "$uv_version_output" in
  "uv $bootstrap_uv_version"|"uv $bootstrap_uv_version "*) ;;
  *) fail "the verified uv executable reported an unexpected version" ;;
esac

verify_sigstore() {
  subject=$1
  bundle=$2
  identity=$3
  step "Verifying Sigstore identity for $(basename "$subject")"
  "$uv_cmd" tool run \
    --from "sigstore==$pinned_sigstore_version" \
    sigstore verify identity "$subject" \
    --bundle "$bundle" \
    --offline \
    --cert-identity "$identity" \
    --cert-oidc-issuer "$expected_issuer" ||
    fail "Sigstore identity verification failed"
}

json_get() {
  file=$1
  path=$2
  "$uv_cmd" run --no-project --python 3.11 python -c '
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
for part in sys.argv[2].split("."):
    value = value[part]
if isinstance(value, bool):
    print("true" if value else "false")
else:
    print(value)
' "$file" "$path"
}

install_checksums="$tmp/OCCULT-INSTALL-SHA256SUMS.txt"
install_bundle="$install_checksums.sigstore.json"
download \
  "$hermes_release_base/OCCULT-INSTALL-SHA256SUMS.txt" \
  "$install_checksums" \
  "the Hermes install checksum manifest"
download \
  "$hermes_release_base/OCCULT-INSTALL-SHA256SUMS.txt.sigstore.json" \
  "$install_bundle" \
  "the Hermes Sigstore bundle"
verify_sigstore \
  "$install_checksums" \
  "$install_bundle" \
  "https://github.com/$hermes_repository/.github/workflows/occult-production-gate.yml@refs/heads/main"

manifest_path="$tmp/occult-install-manifest.json"
download \
  "$hermes_release_base/occult-install-manifest.json" \
  "$manifest_path" \
  "the Occult install manifest"
manifest_hash=$(verify_hash "$install_checksums" "occult-install-manifest.json" "$manifest_path")
[ "$(json_get "$manifest_path" schema_version)" = "1.0.0" ] ||
  fail "unsupported install manifest schema"
[ "$(json_get "$manifest_path" occult_release_version)" = "$version" ] ||
  fail "the requested version does not match the signed install manifest"
[ "$(json_get "$manifest_path" uv_version)" = "$bootstrap_uv_version" ] ||
  fail "the signed manifest does not match the pinned uv verifier"
[ "$(json_get "$manifest_path" sigstore_python_version)" = "$pinned_sigstore_version" ] ||
  fail "the signed manifest does not match the pinned Sigstore verifier"

signed_script="$tmp/install-occult.sh"
download \
  "$hermes_release_base/install-occult.sh" \
  "$signed_script" \
  "the signed Unix installer"
signed_script_hash=$(verify_hash "$install_checksums" "install-occult.sh" "$signed_script")
running_script_hash=$(sha256_file "$0")
[ "$running_script_hash" = "$signed_script_hash" ] ||
  fail "the running installer does not match the Sigstore-verified release copy"

wheel_asset=$(safe_asset_name "$(json_get "$manifest_path" hermes_wheel_asset)")
wheel_path="$tmp/$wheel_asset"
download "$hermes_release_base/$wheel_asset" "$wheel_path" "the Hermes wheel"
wheel_hash=$(verify_hash "$install_checksums" "$wheel_asset" "$wheel_path")

council_archive=""
council_hash=""
council_tag=""
if [ "$skip_council" -eq 0 ]; then
  council_tag=$(json_get "$manifest_path" council.release_tag)
  [ "$council_tag" = "v0.5.2" ] ||
    fail "the signed install manifest does not pin Agents Council v0.5.2"
  council_asset=$(safe_asset_name "$(json_get "$manifest_path" "council.assets.$platform_key")")
  council_base="https://github.com/$council_repository/releases/download/$council_tag"
  council_checksums="$tmp/RELEASE-SHA256SUMS.txt"
  council_bundle="$council_checksums.sigstore.json"
  council_archive="$tmp/$council_asset"
  download \
    "$council_base/RELEASE-SHA256SUMS.txt" \
    "$council_checksums" \
    "the Council release checksum manifest"
  download \
    "$council_base/RELEASE-SHA256SUMS.txt.sigstore.json" \
    "$council_bundle" \
    "the Council Sigstore bundle"
  verify_sigstore \
    "$council_checksums" \
    "$council_bundle" \
    "https://github.com/$council_repository/.github/workflows/release.yml@refs/tags/$council_tag"
  download \
    "$council_base/$council_asset" \
    "$council_archive" \
    "the Council $platform_key archive"
  council_hash=$(verify_hash "$council_checksums" "$council_asset" "$council_archive")
fi

if [ "$verify_only" -eq 1 ]; then
  step "All requested release assets passed Sigstore and SHA-256 verification"
  exit 0
fi

case "$install_root" in
  /*) ;;
  *) install_root="$(pwd)/$install_root" ;;
esac
bin_root="$install_root/bin"
tool_root="$install_root/uv-tools"
mkdir -p "$bin_root" "$tool_root"

step "Installing the verified Hermes wheel per-user"
UV_TOOL_DIR="$tool_root" UV_TOOL_BIN_DIR="$bin_root" \
  "$uv_cmd" tool install \
  --no-config \
  --force \
  --python 3.11 \
  "${wheel_path}[occult]" ||
  fail "Hermes wheel installation failed"

hermes_executable="$bin_root/hermes"
[ -x "$hermes_executable" ] || fail "Hermes installed without creating the hermes executable"
hermes_cli_version=$(json_get "$manifest_path" hermes_cli_version)
hermes_version_output=$("$hermes_executable" --version)
case "$hermes_version_output" in
  *"$hermes_cli_version"*) ;;
  *) fail "Hermes executable version does not match signed release metadata" ;;
esac

council_version_output=""
if [ "$skip_council" -eq 0 ]; then
  command -v tar >/dev/null 2>&1 || fail "tar is required to install Agents Council"
  council_root="$install_root/council/$council_tag"
  mkdir -p "$council_root"
  assert_safe_tar_archive "$council_archive"
  tar -xzf "$council_archive" -C "$council_root" ||
    fail "Council archive extraction failed"
  packaged_council="$council_root/cli/council"
  [ -f "$packaged_council" ] ||
    fail "the verified Council archive does not contain cli/council"
  cp "$packaged_council" "$bin_root/council"
  chmod 0755 "$bin_root/council"
  council_version_output=$("$bin_root/council" --version)
  case "$council_version_output" in
    *"${council_tag#v}"*) ;;
    *) fail "Council executable version does not match signed release metadata" ;;
  esac
fi

user_bin="${XDG_BIN_HOME:-$HOME/.local/bin}"
mkdir -p "$user_bin"
ln -sfn "$hermes_executable" "$user_bin/hermes"
if [ "$skip_council" -eq 0 ]; then
  ln -sfn "$bin_root/council" "$user_bin/council"
fi

initialized=false
if [ "$initialize_local" -eq 1 ]; then
  command -v ollama >/dev/null 2>&1 ||
    fail "Ollama is required for --initialize-local. Install it from https://ollama.com/download and rerun this command"
  step "Pulling the explicitly requested local model $model"
  ollama pull "$model" || fail "Ollama could not pull $model"
  step "Explicitly initializing the local Occult profile"
  "$hermes_executable" occult init --model "$model" ||
    fail "hermes occult init failed"
  initialized=true
fi

if [ "$skip_council" -eq 1 ]; then
  council_json=null
  council_hash_json=null
else
  council_json="\"$council_tag\""
  council_hash_json="\"$council_hash\""
fi
receipt_tmp="$install_root/occult-install-receipt.json.tmp"
receipt="$install_root/occult-install-receipt.json"
cat >"$receipt_tmp" <<EOF
{
  "schema_version": "1.0.0",
  "occult_release_version": "$version",
  "hermes_cli_version": "$hermes_cli_version",
  "hermes_wheel": "$wheel_asset",
  "hermes_wheel_sha256": "$wheel_hash",
  "install_manifest_sha256": "$manifest_hash",
  "council_release": $council_json,
  "council_archive_sha256": $council_hash_json,
  "contract_version": "$(json_get "$manifest_path" council.contract_version)",
  "council_state_schema": $(json_get "$manifest_path" council.state_schema),
  "occult_initialized": $initialized
}
EOF
mv -f "$receipt_tmp" "$receipt"

step "Installed Occult release v$version in $install_root"
printf '%s\n' "$hermes_version_output"
if [ -n "$council_version_output" ]; then
  printf 'Agents Council %s\n' "$council_version_output"
fi
if [ "$initialize_local" -eq 1 ]; then
  step "Local initialization completed explicitly with $model"
else
  step "Occult remains disabled. Run this installer again with --initialize-local when ready"
fi
