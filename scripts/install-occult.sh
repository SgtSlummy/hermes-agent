#!/usr/bin/env sh
# Signed GitHub-first installer for the Tarot Router local public release.
#
# Download this file from the matching Hermes GitHub release before running it.
# Pipe-to-shell execution is rejected because the running file is compared with
# the signed release copy before any application files are installed.

set -eu

version="1.0.8"
install_root="${XDG_DATA_HOME:-$HOME/.local/share}/occult"
initialize_local=0
skip_council=0
verify_only=0
model="qwen2.5:3b"

usage() {
  cat <<'EOF'
Usage: install-occult.sh [options]

  --version VERSION       Tarot Router GitHub release (default: 1.0.8)
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

version_output_matches() {
  output=$1
  expected=$2
  printf '%s\n' "$output" |
    tr -cs '[:alnum:].+_-' '\n' |
    awk -v expected="$expected" '
      $0 == expected || $0 == "v" expected { found=1 }
      END { exit(found ? 0 : 1) }
    '
}

regular_file_profile() {
  "$sigstore_venv/bin/python" - "$1" <<'PY'
import os
import stat
import sys

status = os.lstat(sys.argv[1])
if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
    raise SystemExit(1)
print(f"{stat.S_IMODE(status.st_mode):o}")
PY
}

real_directory_profile() {
  "$sigstore_venv/bin/python" - "$1" <<'PY'
import os
import stat
import sys

status = os.lstat(sys.argv[1])
reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
if not stat.S_ISDIR(status.st_mode) or (
    getattr(status, "st_file_attributes", 0) & reparse
):
    raise SystemExit(1)
print(f"{stat.S_IMODE(status.st_mode):o}")
PY
}

read_occult_state() {
  python_executable=$1
  "$python_executable" -c '
from hermes_cli import config
raw = config.read_raw_config() or {}
occult = raw.get("occult")
initialized = isinstance(occult, dict) and bool(occult.get("local_model"))
enabled = initialized and occult.get("enabled") is True
print(("true" if initialized else "false") + " " + ("true" if enabled else "false"))
'
}

case "$version" in
  v*) version=${version#v} ;;
esac
case "$version" in
  *[!0-9.]*)
    fail "--version must be a semantic version such as 1.0.8"
    ;;
esac
printf '%s\n' "$version" |
  awk -F. 'NF == 3 && $1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ { ok=1 } END { exit(ok ? 0 : 1) }' ||
  fail "--version must be a semantic version such as 1.0.8"
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
sigstore_requirements_asset="occult-sigstore-requirements.lock"
sigstore_requirements_sha256="a6381e9415344393a827d264cc5bda5c2fa00e95fb92e49b67fcd1aa94285916"
expected_issuer="https://token.actions.githubusercontent.com"
release_tag="v$version"
hermes_release_base="https://github.com/$hermes_repository/releases/download/$release_tag"

tmp_base=${TMPDIR:-/tmp}
tmp=$(mktemp -d "$tmp_base/occult-install.XXXXXX") ||
  fail "could not create a temporary verification directory"
cleanup() {
  if [ -n "${reference_hermes_root:-}" ]; then
    reference_parent=${reference_hermes_root%/*}
    if [ -n "${hermes_environments:-}" ] &&
      [ "$reference_parent" = "$hermes_environments" ] &&
      [ "$reference_hermes_root" != "$hermes_environments" ]; then
      rm -rf -- "$reference_hermes_root"
    fi
  fi
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

sigstore_requirements="$tmp/$sigstore_requirements_asset"
download \
  "$hermes_release_base/$sigstore_requirements_asset" \
  "$sigstore_requirements" \
  "the pinned Sigstore dependency lock"
[ "$(sha256_file "$sigstore_requirements")" = "$sigstore_requirements_sha256" ] ||
  fail "SHA-256 verification failed for the pinned Sigstore dependency lock"
sigstore_venv="$tmp/sigstore-verifier"
"$uv_cmd" venv --no-config --python 3.11 "$sigstore_venv" ||
  fail "Sigstore verifier environment creation failed"
"$uv_cmd" pip sync \
  --no-config \
  --python "$sigstore_venv/bin/python" \
  --require-hashes \
  --only-binary :all: \
  "$sigstore_requirements" ||
  fail "Sigstore verifier dependency installation failed"
sigstore_cmd="$sigstore_venv/bin/sigstore"
[ -x "$sigstore_cmd" ] ||
  fail "the hash-locked verifier did not create the sigstore executable"

verify_sigstore() {
  subject=$1
  bundle=$2
  identity=$3
  step "Verifying Sigstore identity for $(basename "$subject")"
  "$sigstore_cmd" verify identity "$subject" \
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
[ "$(json_get "$manifest_path" sigstore_requirements_asset)" = "$sigstore_requirements_asset" ] ||
  fail "the signed manifest does not match the hash-locked Sigstore dependency set"
[ "$(json_get "$manifest_path" sigstore_requirements_sha256)" = "$sigstore_requirements_sha256" ] ||
  fail "the signed manifest does not match the hash-locked Sigstore dependency set"

signed_script="$tmp/install-occult.sh"
download \
  "$hermes_release_base/install-occult.sh" \
  "$signed_script" \
  "the signed Unix installer"
signed_script_hash=$(verify_hash "$install_checksums" "install-occult.sh" "$signed_script")
running_script_hash=$(sha256_file "$0")
[ "$running_script_hash" = "$signed_script_hash" ] ||
  fail "the running installer does not match the Sigstore-verified release copy"

environment_verifier_asset=$(
  safe_asset_name "$(json_get "$manifest_path" environment_verifier_asset)"
)
environment_verifier="$tmp/$environment_verifier_asset"
download \
  "$hermes_release_base/$environment_verifier_asset" \
  "$environment_verifier" \
  "the authenticated environment verifier"
verify_hash \
  "$install_checksums" \
  "$environment_verifier_asset" \
  "$environment_verifier" >/dev/null

wheel_asset=$(safe_asset_name "$(json_get "$manifest_path" hermes_wheel_asset)")
wheel_path="$tmp/$wheel_asset"
download "$hermes_release_base/$wheel_asset" "$wheel_path" "the Hermes wheel"
wheel_hash=$(verify_hash "$install_checksums" "$wheel_asset" "$wheel_path")
requirements_asset=$(safe_asset_name "$(json_get "$manifest_path" hermes_requirements_asset)")
requirements_path="$tmp/$requirements_asset"
download \
  "$hermes_release_base/$requirements_asset" \
  "$requirements_path" \
  "the locked Hermes dependency set"
requirements_hash=$(
  verify_hash "$install_checksums" "$requirements_asset" "$requirements_path"
)

council_archive=""
council_hash=""
council_tag=""
if [ "$skip_council" -eq 0 ]; then
  council_tag=$(json_get "$manifest_path" council.release_tag)
  if ! printf '%s\n' "$council_tag" |
    grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$'; then
    fail "the signed install manifest contains an invalid Council release tag"
  fi
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
hermes_environments="$install_root/hermes-environments"
receipt="$install_root/occult-install-receipt.json"
hermes_cli_version=$(json_get "$manifest_path" hermes_cli_version)
council_contract_version=$(json_get "$manifest_path" council.contract_version)
council_state_schema=$(json_get "$manifest_path" council.state_schema)
existing_receipt_seen=0
existing_metadata=""
if [ -f "$receipt" ]; then
  existing_receipt_seen=1
  expected_council_tag=$council_tag
  expected_council_hash=$council_hash
  if existing_metadata=$(
    "$uv_cmd" run --no-project --python 3.11 python -c '
import json, re, sys

(
    receipt_path,
    version,
    hermes_cli_version,
    wheel_asset,
    wheel_hash,
    requirements_asset,
    requirements_hash,
    sigstore_asset,
    sigstore_hash,
    manifest_hash,
    council_tag,
    council_hash,
    contract_version,
    state_schema,
) = sys.argv[1:]

try:
    receipt = json.load(open(receipt_path, encoding="utf-8-sig"))
except (OSError, ValueError, TypeError):
    raise SystemExit(1)

required = {
    "schema_version",
    "occult_release_version",
    "hermes_cli_version",
    "hermes_wheel",
    "hermes_wheel_sha256",
    "hermes_requirements",
    "hermes_requirements_sha256",
    "sigstore_requirements",
    "sigstore_requirements_sha256",
    "hermes_environment",
    "install_manifest_sha256",
    "council_release",
    "council_archive_sha256",
    "council_environment",
    "contract_version",
    "council_state_schema",
    "occult_initialized",
    "occult_enabled",
}
if not isinstance(receipt, dict) or not required.issubset(receipt):
    raise SystemExit(1)
if type(receipt.get("occult_initialized")) is not bool:
    raise SystemExit(1)
if type(receipt.get("occult_enabled")) is not bool:
    raise SystemExit(1)

expected = {
    "schema_version": "1.0.0",
    "occult_release_version": version,
    "hermes_cli_version": hermes_cli_version,
    "hermes_wheel": wheel_asset,
    "hermes_wheel_sha256": wheel_hash,
    "hermes_requirements": requirements_asset,
    "hermes_requirements_sha256": requirements_hash,
    "sigstore_requirements": sigstore_asset,
    "sigstore_requirements_sha256": sigstore_hash,
    "install_manifest_sha256": manifest_hash,
    "council_release": council_tag,
    "council_archive_sha256": council_hash,
    "contract_version": contract_version,
}
for key, expected_value in expected.items():
    actual = receipt.get(key)
    actual_text = "" if actual is None else str(actual)
    if actual_text != expected_value:
        raise SystemExit(1)
if receipt.get("council_state_schema") != int(state_schema):
    raise SystemExit(1)

def safe_leaf(value):
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", value)
        and ".." not in value
    )

hermes_environment = receipt.get("hermes_environment")
council_environment = receipt.get("council_environment")
if not safe_leaf(hermes_environment):
    raise SystemExit(1)
if council_tag:
    if not safe_leaf(council_environment):
        raise SystemExit(1)
elif council_environment not in (None, ""):
    raise SystemExit(1)

print(hermes_environment)
print(council_environment or "")
print("true" if receipt["occult_initialized"] else "false")
print("true" if receipt["occult_enabled"] else "false")
' \
      "$receipt" \
      "$version" \
      "$hermes_cli_version" \
      "$wheel_asset" \
      "$wheel_hash" \
      "$requirements_asset" \
      "$requirements_hash" \
      "$sigstore_requirements_asset" \
      "$sigstore_requirements_sha256" \
      "$manifest_hash" \
      "$expected_council_tag" \
      "$expected_council_hash" \
      "$council_contract_version" \
      "$council_state_schema"
  ); then
    :
  else
    existing_metadata=""
  fi
fi

if [ -n "$existing_metadata" ]; then
  existing_hermes_environment=$(printf '%s\n' "$existing_metadata" | sed -n '1p')
  existing_council_environment=$(printf '%s\n' "$existing_metadata" | sed -n '2p')
  existing_receipt_initialized=$(printf '%s\n' "$existing_metadata" | sed -n '3p')
  existing_receipt_enabled=$(printf '%s\n' "$existing_metadata" | sed -n '4p')
  existing_hermes_root="$hermes_environments/$existing_hermes_environment"
  existing_venv_python="$existing_hermes_root/bin/python"
  existing_venv_hermes="$existing_hermes_root/bin/hermes"
  hermes_executable="$bin_root/hermes"
  reuse_ok=1
  receipt_state_changed=0
  [ -x "$existing_venv_python" ] || reuse_ok=0
  [ -x "$existing_venv_hermes" ] || reuse_ok=0
  [ -x "$hermes_executable" ] || reuse_ok=0
  if [ "$reuse_ok" -eq 1 ]; then
    [ -L "$hermes_executable" ] &&
      [ "$(readlink "$hermes_executable")" = "$existing_venv_hermes" ] ||
      reuse_ok=0
  fi
  if [ "$reuse_ok" -eq 1 ]; then
    [ "$(sha256_file "$existing_venv_hermes")" = "$(sha256_file "$hermes_executable")" ] ||
      reuse_ok=0
  fi
  if [ "$reuse_ok" -eq 1 ]; then
    reference_length=${#existing_hermes_environment}
    [ "$reference_length" -ge 6 ] || reuse_ok=0
  fi
  if [ "$reuse_ok" -eq 1 ]; then
    reference_template=$(printf '%*s' "$reference_length" '' | tr ' ' X)
    reference_hermes_root=$(mktemp -d "$hermes_environments/$reference_template") ||
      fail "the isolated Hermes reference path is unavailable"
    "$uv_cmd" venv \
      --no-config \
      --allow-existing \
      --python 3.11 \
      "$reference_hermes_root" ||
      fail "Hermes reference environment creation failed"
    reference_python="$reference_hermes_root/bin/python"
    reference_cache="$tmp/hermes-reference-cache"
    "$uv_cmd" pip sync \
      --no-config \
      --python "$reference_python" \
      --require-hashes \
      --link-mode copy \
      --cache-dir "$reference_cache" \
      "$requirements_path" ||
      fail "Hermes reference dependencies failed verification"
    "$uv_cmd" pip install \
      --no-config \
      --python "$reference_python" \
      --no-deps \
      --no-index \
      --link-mode copy \
      --cache-dir "$reference_cache" \
      "$wheel_path" ||
      fail "Hermes reference wheel installation failed"
    "$sigstore_venv/bin/python" \
      "$environment_verifier" \
      --existing "$existing_hermes_root" \
      --reference "$reference_hermes_root" >/dev/null 2>&1 ||
      reuse_ok=0
    rm -rf -- "$reference_hermes_root"
    reference_hermes_root=""
  fi
  hermes_version_output=""
  if [ "$reuse_ok" -eq 1 ]; then
    if hermes_version_output=$("$hermes_executable" --version 2>/dev/null); then
      version_output_matches "$hermes_version_output" "$hermes_cli_version" ||
        reuse_ok=0
    else
      reuse_ok=0
    fi
  fi

  council_version_output=""
  if [ "$reuse_ok" -eq 1 ] && [ "$skip_council" -eq 0 ]; then
    existing_council_root="$install_root/council-environments/$existing_council_environment"
    existing_packaged_council="$existing_council_root/cli/council"
    council_executable="$bin_root/council"
    real_directory_profile "$existing_council_root" >/dev/null 2>&1 || reuse_ok=0
    [ -f "$existing_packaged_council" ] || reuse_ok=0
    [ -x "$council_executable" ] || reuse_ok=0
    if [ "$reuse_ok" -eq 1 ]; then
      reference_council_root="$tmp/council-reference"
      mkdir -p "$reference_council_root"
      assert_safe_tar_archive "$council_archive"
      tar -xzf "$council_archive" -C "$reference_council_root" ||
        fail "Council reference extraction failed"
      reference_packaged_council="$reference_council_root/cli/council"
      [ -f "$reference_packaged_council" ] || reuse_ok=0
    fi
    if [ "$reuse_ok" -eq 1 ]; then
      [ "$(sha256_file "$reference_packaged_council")" = "$(sha256_file "$existing_packaged_council")" ] ||
        reuse_ok=0
    fi
    if [ "$reuse_ok" -eq 1 ]; then
      [ "$(sha256_file "$existing_packaged_council")" = "$(sha256_file "$council_executable")" ] ||
        reuse_ok=0
    fi
    if [ "$reuse_ok" -eq 1 ]; then
      reference_council_profile=$(regular_file_profile "$reference_packaged_council") ||
        reuse_ok=0
    fi
    if [ "$reuse_ok" -eq 1 ]; then
      [ "$(regular_file_profile "$existing_packaged_council")" = "$reference_council_profile" ] ||
        reuse_ok=0
    fi
    if [ "$reuse_ok" -eq 1 ]; then
      [ "$(regular_file_profile "$council_executable")" = "$reference_council_profile" ] ||
        reuse_ok=0
    fi
    if [ "$reuse_ok" -eq 1 ]; then
      if council_version_output=$("$council_executable" --version 2>/dev/null); then
        version_output_matches "$council_version_output" "${council_tag#v}" ||
          reuse_ok=0
      else
        reuse_ok=0
      fi
    fi
  fi

  user_bin="${XDG_BIN_HOME:-$HOME/.local/bin}"
  if [ "$reuse_ok" -eq 1 ] && [ "$skip_council" -eq 1 ]; then
    if [ -e "$bin_root/council" ] || [ -L "$bin_root/council" ]; then
      reuse_ok=0
    fi
    if [ -L "$user_bin/council" ] &&
      [ "$(readlink "$user_bin/council")" = "$bin_root/council" ]; then
      reuse_ok=0
    fi
  fi
  if [ "$reuse_ok" -eq 1 ]; then
    [ -L "$user_bin/hermes" ] || reuse_ok=0
    if [ "$reuse_ok" -eq 1 ]; then
      [ "$(readlink "$user_bin/hermes")" = "$hermes_executable" ] || reuse_ok=0
    fi
    if [ "$reuse_ok" -eq 1 ] && [ "$skip_council" -eq 0 ]; then
      [ -L "$user_bin/council" ] || reuse_ok=0
      if [ "$reuse_ok" -eq 1 ]; then
        [ "$(readlink "$user_bin/council")" = "$bin_root/council" ] || reuse_ok=0
      fi
    fi
  fi

  if [ "$reuse_ok" -eq 1 ]; then
    state=$(read_occult_state "$existing_venv_python") ||
      fail "could not inspect the preserved Occult initialization state"
    set -- $state
    initialized=${1:-false}
    enabled=${2:-false}
    case "$initialized:$enabled" in
      true:true|true:false|false:false) ;;
      *) fail "the preserved Occult initialization state was invalid" ;;
    esac
    if [ "$existing_receipt_initialized" != "$initialized" ] ||
      [ "$existing_receipt_enabled" != "$enabled" ]; then
      receipt_state_changed=1
    fi
  fi

  if [ "$reuse_ok" -eq 1 ]; then
    if [ "$initialize_local" -eq 1 ]; then
      command -v ollama >/dev/null 2>&1 ||
        fail "Ollama is required for --initialize-local. Install it from https://ollama.com/download and rerun this command"
      step "Pulling the explicitly requested local model $model"
      ollama pull "$model" || fail "Ollama could not pull $model"
      step "Explicitly initializing the local Occult profile"
      "$hermes_executable" occult init --model "$model" ||
        fail "hermes occult init failed"
      state=$(read_occult_state "$existing_venv_python") ||
        fail "could not inspect the preserved Occult initialization state"
      set -- $state
      initialized=${1:-false}
      enabled=${2:-false}
      case "$initialized:$enabled" in
        true:true|true:false|false:false) ;;
        *) fail "the preserved Occult initialization state was invalid" ;;
      esac
    fi

    if [ "$initialize_local" -eq 1 ] || [ "$receipt_state_changed" -eq 1 ]; then
      "$existing_venv_python" -c '
import json, os, sys
path, initialized, enabled = sys.argv[1:]
with open(path, encoding="utf-8-sig") as handle:
    receipt = json.load(handle)
receipt["occult_initialized"] = initialized == "true"
receipt["occult_enabled"] = enabled == "true"
temporary = path + ".tmp"
with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
    json.dump(receipt, handle, indent=2)
    handle.write("\n")
os.replace(temporary, path)
' "$receipt" "$initialized" "$enabled" ||
        fail "could not update the preserved install receipt"
    fi

    step "Verified existing Occult release v$version; no application files changed"
    printf '%s\n' "$hermes_version_output"
    if [ -n "$council_version_output" ]; then
      printf 'Agents Council %s\n' "$council_version_output"
    fi
    if [ "$initialize_local" -eq 1 ]; then
      step "Local initialization completed explicitly with $model"
    elif [ "$receipt_state_changed" -eq 1 ]; then
      step "Mutable Occult state was refreshed in the preserved install receipt"
    elif [ "$initialized" = true ]; then
      step "Existing Occult initialization was preserved and remains $([ "$enabled" = true ] && printf enabled || printf disabled)"
    else
      step "Occult remains disabled. Run this installer again with --initialize-local when ready"
    fi
    case ":$PATH:" in
      *":$user_bin:"*) ;;
      *) printf 'Add the installed commands to this shell: export PATH="%s:$PATH"\n' "$user_bin" ;;
    esac
    exit 0
  fi
fi
if [ "$existing_receipt_seen" -eq 1 ]; then
  step "Existing installation does not match the verified release; staging a repair"
fi

mkdir -p "$bin_root" "$hermes_environments"
hermes_venv=$(mktemp -d "$hermes_environments/$version.XXXXXX") ||
  fail "could not create the staged Hermes environment"
hermes_environment=$(basename "$hermes_venv")

step "Installing the verified Hermes wheel and hash-locked dependencies per-user"
"$uv_cmd" venv \
  --no-config \
  --python 3.11 \
  "$hermes_venv" ||
  fail "Hermes environment creation failed"
venv_python="$hermes_venv/bin/python"
"$uv_cmd" pip sync \
  --no-config \
  --python "$venv_python" \
  --require-hashes \
  --link-mode copy \
  "$requirements_path" ||
  fail "Hermes locked dependency installation failed"
"$uv_cmd" pip install \
  --no-config \
  --python "$venv_python" \
  --no-deps \
  --no-index \
  --link-mode copy \
  "$wheel_path" ||
  fail "Hermes wheel installation failed"

venv_hermes_executable="$hermes_venv/bin/hermes"
[ -x "$venv_hermes_executable" ] ||
  fail "Hermes installed without creating the hermes executable"
hermes_executable="$bin_root/hermes"
hermes_staged="$bin_root/hermes.new.$$"
ln -s "$venv_hermes_executable" "$hermes_staged" ||
  fail "Hermes command staging failed"
hermes_cli_version=$(json_get "$manifest_path" hermes_cli_version)
hermes_version_output=$("$hermes_staged" --version)
version_output_matches "$hermes_version_output" "$hermes_cli_version" ||
  fail "Hermes executable version does not match signed release metadata"

council_version_output=""
council_environment=""
council_staged=""
if [ "$skip_council" -eq 0 ]; then
  command -v tar >/dev/null 2>&1 || fail "tar is required to install Agents Council"
  council_environments="$install_root/council-environments"
  mkdir -p "$council_environments"
  council_root=$(mktemp -d "$council_environments/$council_tag.XXXXXX") ||
    fail "could not create the staged Council environment"
  council_environment=$(basename "$council_root")
  assert_safe_tar_archive "$council_archive"
  tar -xzf "$council_archive" -C "$council_root" ||
    fail "Council archive extraction failed"
  packaged_council="$council_root/cli/council"
  [ -f "$packaged_council" ] ||
    fail "the verified Council archive does not contain cli/council"
  council_staged="$bin_root/council.new.$$"
  cp "$packaged_council" "$council_staged" ||
    fail "Council command staging failed"
  chmod 0755 "$council_staged"
  council_version_output=$("$council_staged" --version)
  version_output_matches "$council_version_output" "${council_tag#v}" ||
    fail "Council executable version does not match signed release metadata"
fi

if [ "$initialize_local" -eq 1 ]; then
  command -v ollama >/dev/null 2>&1 ||
    fail "Ollama is required for --initialize-local. Install it from https://ollama.com/download and rerun this command"
  step "Pulling the explicitly requested local model $model"
  ollama pull "$model" || fail "Ollama could not pull $model"
  step "Explicitly initializing the local Occult profile"
  "$hermes_staged" occult init --model "$model" ||
    fail "hermes occult init failed"
fi

state=$(
  "$venv_python" -c '
from hermes_cli import config
raw = config.read_raw_config() or {}
occult = raw.get("occult")
initialized = isinstance(occult, dict) and bool(occult.get("local_model"))
enabled = initialized and occult.get("enabled") is True
print(("true" if initialized else "false") + " " + ("true" if enabled else "false"))
'
) || fail "could not inspect the preserved Occult initialization state"
set -- $state
initialized=${1:-false}
enabled=${2:-false}
case "$initialized:$enabled" in
  true:true|true:false|false:false) ;;
  *) fail "the preserved Occult initialization state was invalid" ;;
esac

user_bin="${XDG_BIN_HOME:-$HOME/.local/bin}"
mkdir -p "$user_bin"
if [ -e "$user_bin/hermes" ] || [ -L "$user_bin/hermes" ]; then
  if [ ! -L "$user_bin/hermes" ] ||
    [ "$(readlink "$user_bin/hermes")" != "$hermes_executable" ]; then
    fail "the user Hermes command path is not the managed Tarot Router link"
  fi
fi
if [ "$skip_council" -eq 0 ] &&
  { [ -e "$user_bin/council" ] || [ -L "$user_bin/council" ]; }; then
  if [ ! -L "$user_bin/council" ] ||
    [ "$(readlink "$user_bin/council")" != "$bin_root/council" ]; then
    fail "the user Council command path is not the managed Tarot Router link"
  fi
fi
if [ -d "$hermes_executable" ]; then
  fail "the managed Hermes command path is a directory"
fi
if [ "$skip_council" -eq 0 ] && [ -d "$bin_root/council" ]; then
  fail "the managed Council command path is a directory"
fi

step "Activating the fully staged local commands"
mv -f "$hermes_staged" "$hermes_executable" ||
  fail "Hermes command activation failed"
if [ "$skip_council" -eq 1 ]; then
  if [ -d "$bin_root/council" ] && [ ! -L "$bin_root/council" ]; then
    fail "the stale managed Council command is not a file"
  fi
  rm -f -- "$bin_root/council" ||
    fail "the stale managed Council command could not be removed"
  if [ -L "$user_bin/council" ] &&
    [ "$(readlink "$user_bin/council")" = "$bin_root/council" ]; then
    rm -f -- "$user_bin/council" ||
      fail "the stale managed Council command link could not be removed"
  fi
else
  mv -f "$council_staged" "$bin_root/council" ||
    fail "Council command activation failed"
fi

ln -sfn "$hermes_executable" "$user_bin/hermes"
[ -L "$user_bin/hermes" ] &&
  [ "$(readlink "$user_bin/hermes")" = "$hermes_executable" ] ||
  fail "the user Hermes command link could not be verified"
if [ "$skip_council" -eq 0 ]; then
  ln -sfn "$bin_root/council" "$user_bin/council"
  [ -L "$user_bin/council" ] &&
    [ "$(readlink "$user_bin/council")" = "$bin_root/council" ] ||
    fail "the user Council command link could not be verified"
fi

if [ "$skip_council" -eq 1 ]; then
  council_json=null
  council_hash_json=null
else
  council_json="\"$council_tag\""
  council_hash_json="\"$council_hash\""
fi
receipt_tmp="$receipt.tmp"
cat >"$receipt_tmp" <<EOF
{
  "schema_version": "1.0.0",
  "occult_release_version": "$version",
  "hermes_cli_version": "$hermes_cli_version",
  "hermes_wheel": "$wheel_asset",
  "hermes_wheel_sha256": "$wheel_hash",
  "hermes_requirements": "$requirements_asset",
  "hermes_requirements_sha256": "$requirements_hash",
  "sigstore_requirements": "$sigstore_requirements_asset",
  "sigstore_requirements_sha256": "$sigstore_requirements_sha256",
  "hermes_environment": "$hermes_environment",
  "install_manifest_sha256": "$manifest_hash",
  "council_release": $council_json,
  "council_archive_sha256": $council_hash_json,
  "council_environment": $(if [ "$skip_council" -eq 1 ]; then printf null; else printf '"%s"' "$council_environment"; fi),
  "contract_version": "$(json_get "$manifest_path" council.contract_version)",
  "council_state_schema": $(json_get "$manifest_path" council.state_schema),
  "occult_initialized": $initialized,
  "occult_enabled": $enabled
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
elif [ "$initialized" = true ]; then
  step "Existing Occult initialization was preserved and remains $([ "$enabled" = true ] && printf enabled || printf disabled)"
else
  step "Occult remains disabled. Run this installer again with --initialize-local when ready"
fi
case ":$PATH:" in
  *":$user_bin:"*) ;;
  *) printf 'Add the installed commands to this shell: export PATH="%s:$PATH"\n' "$user_bin" ;;
esac
