#!/usr/bin/env python3
"""Compare an installed Hermes environment with a freshly verified reference.

The installer builds the reference from the signed Hermes wheel and the
hash-locked dependency set. This verifier therefore treats the reference as
the authenticated source of truth instead of trusting installed RECORD files.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import marshal
import os
import py_compile
import re
import struct
import sys
import tempfile
from email.parser import BytesParser
from importlib.util import source_from_cache
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile


class IntegrityError(RuntimeError):
    """The installed environment differs from the verified reference."""


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _site_root(environment: Path) -> Path:
    records = sorted(environment.rglob("*.dist-info/RECORD"))
    roots = {record.parent.parent.resolve() for record in records}
    if not records or len(roots) != 1:
        raise IntegrityError("environment has an ambiguous package root")
    root = next(iter(roots))
    if not _inside(root, environment):
        raise IntegrityError("package root escapes the environment")
    return root


def _inventory(site_root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for metadata_path in sorted(site_root.glob("*.dist-info/METADATA")):
        try:
            metadata = BytesParser().parsebytes(
                metadata_path.read_bytes(),
                headersonly=True,
            )
        except OSError as error:
            raise IntegrityError("package metadata is unreadable") from error
        name = _canonical_name(metadata.get("Name", ""))
        version = metadata.get("Version", "")
        if not name or not version or name in result:
            raise IntegrityError("package inventory is invalid")
        result[name] = version
    if not result:
        raise IntegrityError("package inventory is empty")
    return result


def _normalized_direct_url(path: Path) -> bytes:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, TypeError) as error:
        raise IntegrityError("direct URL metadata is invalid") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("url"), str):
        raise IntegrityError("direct URL metadata is incomplete")
    payload["url"] = Path(payload["url"].replace("file://", "")).name
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _normalized_uv_cache(path: Path) -> bytes:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, TypeError) as error:
        raise IntegrityError("uv cache metadata is invalid") from error
    if not isinstance(payload, dict) or "timestamp" not in payload:
        raise IntegrityError("uv cache metadata is incomplete")
    payload["timestamp"] = "artifact-timestamp"
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _file_map(site_root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(site_root.rglob("*")):
        if path.is_symlink():
            target = os.readlink(path)
            result[path.relative_to(site_root).as_posix()] = "symlink:" + target
            continue
        if not path.is_file():
            continue
        relative = path.relative_to(site_root).as_posix()
        if path.suffix == ".pyc" or path.name == "RECORD":
            continue
        if path.name == "direct_url.json":
            data = _normalized_direct_url(path)
        elif path.name == "uv_cache.json":
            data = _normalized_uv_cache(path)
        else:
            data = path.read_bytes()
        result[relative] = hashlib.sha256(data).hexdigest()
    return result


def _record_map(site_root: Path, environment: Path) -> dict[str, list[tuple[str, str, str]]]:
    result: dict[str, list[tuple[str, str, str]]] = {}
    for record in sorted(site_root.glob("*.dist-info/RECORD")):
        rows: list[tuple[str, str, str]] = []
        try:
            stream = record.open(encoding="utf-8", newline="")
        except OSError as error:
            raise IntegrityError("RECORD is unreadable") from error
        with stream:
            for row in csv.reader(stream):
                if len(row) != 3:
                    raise IntegrityError("RECORD row is invalid")
                relative, hash_spec, size = row
                candidate = (
                    site_root.joinpath(*PurePosixPath(relative).parts).resolve()
                )
                if not _inside(candidate, environment):
                    raise IntegrityError("RECORD path escapes the environment")
                if _inside(candidate, site_root):
                    if candidate.name == "direct_url.json":
                        normalized = _normalized_direct_url(candidate)
                    elif candidate.name == "uv_cache.json":
                        normalized = _normalized_uv_cache(candidate)
                    else:
                        normalized = None
                    if normalized is None:
                        rows.append((relative, hash_spec, size))
                    else:
                        encoded = base64.urlsafe_b64encode(
                            hashlib.sha256(normalized).digest()
                        ).decode().rstrip("=")
                        rows.append(
                            (relative, "sha256=" + encoded, str(len(normalized)))
                        )
                else:
                    # Generated launchers embed the environment path. Their
                    # content is authenticated independently below.
                    rows.append((relative, "generated-outside-site-root", ""))
        result[record.relative_to(site_root).as_posix()] = rows
    return result


def _pyc_mode(flags: int) -> py_compile.PycInvalidationMode:
    if not flags & 0x01:
        return py_compile.PycInvalidationMode.TIMESTAMP
    if flags & 0x02:
        return py_compile.PycInvalidationMode.CHECKED_HASH
    return py_compile.PycInvalidationMode.UNCHECKED_HASH


def _validate_bytecode(site_root: Path, trusted_files: set[str]) -> None:
    for cache in sorted(site_root.rglob("*.pyc")):
        if cache.parent.name != "__pycache__":
            raise IntegrityError("sourceless bytecode is not allowed")
        try:
            source = Path(source_from_cache(str(cache))).resolve()
        except (NotImplementedError, ValueError) as error:
            raise IntegrityError("bytecode cache name is invalid") from error
        if not _inside(source, site_root):
            raise IntegrityError("bytecode source escapes package root")
        source_relative = source.relative_to(site_root).as_posix()
        if source_relative not in trusted_files or not source.is_file():
            raise IntegrityError("bytecode has no authenticated source")
        data = cache.read_bytes()
        if len(data) < 16:
            raise IntegrityError("bytecode header is incomplete")
        flags = struct.unpack("<I", data[4:8])[0]
        optimization_match = re.search(r"\.opt-([12])\.pyc$", cache.name)
        optimization = int(optimization_match.group(1)) if optimization_match else 0
        with tempfile.TemporaryDirectory(prefix="occult-pyc-") as temporary:
            expected = Path(temporary) / cache.name
            try:
                py_compile.compile(
                    str(source),
                    cfile=str(expected),
                    dfile=str(source),
                    doraise=True,
                    optimize=optimization,
                    invalidation_mode=_pyc_mode(flags),
                )
            except (OSError, py_compile.PyCompileError) as error:
                raise IntegrityError("bytecode could not be reproduced") from error
            expected_data = expected.read_bytes()
            try:
                code_matches = marshal.loads(data[16:]) == marshal.loads(
                    expected_data[16:]
                )
            except (EOFError, TypeError, ValueError):
                code_matches = False
            if data[:16] != expected_data[:16] or not code_matches:
                relative_cache = cache.relative_to(site_root).as_posix()
                raise IntegrityError(
                    "bytecode differs from authenticated source: " + relative_cache
                )


def _launcher(environment: Path) -> Path:
    candidates = (
        environment / "Scripts" / "hermes.exe",
        environment / "bin" / "hermes",
    )
    matches = [candidate for candidate in candidates if candidate.is_file()]
    if len(matches) != 1:
        raise IntegrityError("Hermes launcher is missing or ambiguous")
    if os.name != "nt" and not os.access(matches[0], os.X_OK):
        raise IntegrityError("Hermes launcher is not executable")
    return matches[0]


def _normalized_launcher(environment: Path) -> bytes:
    launcher = _launcher(environment)
    data = launcher.read_bytes()
    variants: set[str] = set()
    for value in (str(environment), str(environment.resolve())):
        variants.update({value, value.replace("\\", "/"), value.replace("/", "\\")})
    for value in sorted(variants, key=len, reverse=True):
        encoded = value.encode()
        encoded_wide = value.encode("utf-16-le")
        data = data.replace(encoded, b"\0" * len(encoded))
        data = data.replace(encoded_wide, b"\0" * len(encoded_wide))
    archive_offset = data.find(b"PK\x03\x04")
    if archive_offset < 0:
        return data
    try:
        with ZipFile(launcher) as archive:
            members = []
            for name in sorted(archive.namelist()):
                member = archive.read(name)
                for value in sorted(variants, key=len, reverse=True):
                    encoded = value.encode()
                    encoded_wide = value.encode("utf-16-le")
                    member = member.replace(encoded, b"\0" * len(encoded))
                    member = member.replace(
                        encoded_wide,
                        b"\0" * len(encoded_wide),
                    )
                members.append((name, hashlib.sha256(member).hexdigest()))
    except (BadZipFile, KeyError, OSError) as error:
        raise IntegrityError("Hermes launcher archive is invalid") from error
    encoded_members = json.dumps(members, separators=(",", ":")).encode()
    return data[:archive_offset] + b"\n<OCCULT_ZIP>\n" + encoded_members


def verify_environment(existing: Path, reference: Path) -> None:
    existing = existing.resolve()
    reference = reference.resolve()
    if existing == reference or not existing.is_dir() or not reference.is_dir():
        raise IntegrityError("environment paths are invalid")
    existing_site = _site_root(existing)
    reference_site = _site_root(reference)
    if _inventory(existing_site) != _inventory(reference_site):
        raise IntegrityError("installed package inventory differs")
    existing_files = _file_map(existing_site)
    reference_files = _file_map(reference_site)
    if existing_files != reference_files:
        raise IntegrityError("installed package files differ")
    if _record_map(existing_site, existing) != _record_map(reference_site, reference):
        raise IntegrityError("installed RECORD metadata differs")
    _validate_bytecode(existing_site, set(existing_files))
    if _normalized_launcher(existing) != _normalized_launcher(reference):
        raise IntegrityError("Hermes launcher differs")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--existing", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    args = parser.parse_args()
    try:
        verify_environment(args.existing, args.reference)
    except IntegrityError as error:
        print(f"Occult environment integrity check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
