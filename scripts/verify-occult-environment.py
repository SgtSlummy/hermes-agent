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
import os
import py_compile
import re
import stat
import struct
import subprocess
import sys
import tempfile
from email.parser import BytesParser
from importlib.util import source_from_cache
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit
from zipfile import BadZipFile, ZipFile


class IntegrityError(RuntimeError):
    """The installed environment differs from the verified reference."""


def _is_reparse(status: os.stat_result) -> bool:
    return bool(
        getattr(status, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


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
    parsed = urlsplit(payload["url"])
    if parsed.scheme.lower() == "file":
        filename = PurePosixPath(parsed.path).name
        if not filename:
            raise IntegrityError("direct URL metadata has no artifact name")
        # uv records the temporary directory containing the authenticated
        # local wheel. Normalize only that varying directory while retaining
        # the file scheme, authority, artifact name, query, and fragment.
        payload["url"] = urlunsplit(
            ("file", parsed.netloc, "/" + filename, parsed.query, parsed.fragment)
        )
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
    root_status = site_root.lstat()
    if not stat.S_ISDIR(root_status.st_mode) or _is_reparse(root_status):
        raise IntegrityError("package root must be a real directory")
    root_mode = root_status.st_mode & 0o777
    result: dict[str, str] = {".": f"directory:{root_mode:o}"}
    for path in sorted(site_root.rglob("*")):
        relative_path = path.relative_to(site_root)
        if "__pycache__" in relative_path.parts:
            # Import probes can create reproducible caches after installation.
            # Their structure, permissions, links, and content are validated
            # independently instead of requiring reference inventory equality.
            continue
        status = path.lstat()
        if _is_reparse(status):
            raise IntegrityError("package root contains a reparse point")
        if stat.S_ISLNK(status.st_mode):
            target = os.readlink(path)
            result[relative_path.as_posix()] = "symlink:" + target
            continue
        if stat.S_ISDIR(status.st_mode):
            mode = status.st_mode & 0o777
            result[relative_path.as_posix()] = f"directory:{mode:o}"
            continue
        if not stat.S_ISREG(status.st_mode):
            raise IntegrityError("package root contains an unsupported node")
        if status.st_nlink != 1:
            raise IntegrityError("package root contains a hard-linked file")
        relative = relative_path.as_posix()
        mode = status.st_mode & 0o777
        if path.name == "RECORD":
            result[relative] = f"record:{mode:o}"
            continue
        if path.name == "direct_url.json":
            data = _normalized_direct_url(path)
        elif path.name == "uv_cache.json":
            data = _normalized_uv_cache(path)
        else:
            data = path.read_bytes()
        result[relative] = f"file:{mode:o}:" + hashlib.sha256(data).hexdigest()
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
                    # Generated launchers embed the environment path. Every
                    # file outside site-packages is authenticated below.
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
    with tempfile.TemporaryDirectory(prefix="occult-pyc-") as temporary:
        comparisons: list[dict[str, str]] = []
        cache_files: list[Path] = []
        for cache_root in sorted(site_root.rglob("__pycache__")):
            status = cache_root.lstat()
            if (
                not stat.S_ISDIR(status.st_mode)
                or _is_reparse(status)
                or (os.name != "nt" and stat.S_IMODE(status.st_mode) & 0o022)
            ):
                raise IntegrityError("bytecode cache directory is unsafe")
            for cache in sorted(cache_root.iterdir()):
                cache_status = cache.lstat()
                if (
                    cache.suffix != ".pyc"
                    or not stat.S_ISREG(cache_status.st_mode)
                    or _is_reparse(cache_status)
                    or cache_status.st_nlink != 1
                    or (
                        os.name != "nt"
                        and stat.S_IMODE(cache_status.st_mode) & 0o022
                    )
                ):
                    raise IntegrityError("bytecode cache contains an unsafe node")
                cache_files.append(cache)
        for cache in sorted(site_root.rglob("*.pyc")):
            if cache.parent.name != "__pycache__":
                raise IntegrityError("sourceless bytecode is not allowed")
        for cache in cache_files:
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
            optimization = (
                int(optimization_match.group(1)) if optimization_match else 0
            )
            expected_name = hashlib.sha256(
                cache.relative_to(site_root).as_posix().encode()
            ).hexdigest()
            expected = Path(temporary) / expected_name
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
            if data[:16] != expected_data[:16]:
                relative_cache = cache.relative_to(site_root).as_posix()
                raise IntegrityError(
                    "bytecode differs from authenticated source: " + relative_cache
                )
            if data[16:] != expected_data[16:]:
                comparisons.append(
                    {
                        "actual": str(cache),
                        "expected": str(expected),
                    }
                )
        if comparisons:
            manifest = Path(temporary) / "bytecode-comparisons.json"
            manifest.write_text(
                json.dumps(comparisons, separators=(",", ":")),
                encoding="utf-8",
            )
            try:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        str(Path(__file__).resolve()),
                        "--compare-bytecode-manifest",
                        str(manifest),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise IntegrityError("bytecode comparison failed safely") from error
            if completed.returncode != 0:
                raise IntegrityError("bytecode differs from authenticated source")


def _compare_bytecode_manifest(manifest: Path) -> int:
    """Compare marshal payloads in a disposable isolated child process."""
    import marshal

    try:
        comparisons = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(comparisons, list):
            return 1
        for comparison in comparisons:
            if not isinstance(comparison, dict):
                return 1
            actual_path = Path(comparison["actual"])
            expected_path = Path(comparison["expected"])
            actual = actual_path.read_bytes()
            expected = expected_path.read_bytes()
            size_limit = max(len(expected) * 2, len(expected) + 1_000_000)
            if len(actual) > size_limit or len(actual) < 16 or len(expected) < 16:
                return 1
            if marshal.loads(actual[16:]) != marshal.loads(expected[16:]):
                return 1
    except (EOFError, KeyError, OSError, TypeError, ValueError):
        return 1
    return 0


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


def _path_variants(environment: Path) -> set[str]:
    variants: set[str] = set()
    for value in (str(environment), str(environment.resolve())):
        variants.add(value)
        if os.name == "nt":
            variants.update({value.replace("\\", "/"), value.replace("/", "\\")})
    return variants


def _replace_environment_paths(data: bytes, environment: Path) -> bytes:
    variants = _path_variants(environment)
    for value in sorted(variants, key=len, reverse=True):
        encoded = value.encode()
        encoded_wide = value.encode("utf-16-le")
        data = data.replace(encoded, b"\0" * len(encoded))
        data = data.replace(encoded_wide, b"\0" * len(encoded_wide))
    return data


def _end_of_central_directory(
    data: bytes, start: int, member_count: int
) -> tuple[int, int]:
    """Locate a non-ZIP64, single-disk EOCD and its trailing-comment boundary."""
    offset = data.find(b"PK\x05\x06", start)
    while offset >= 0:
        if offset + 22 <= len(data):
            (
                disk_number,
                directory_disk,
                disk_members,
                total_members,
                directory_size,
                directory_offset,
                comment_length,
            ) = struct.unpack_from("<4H2LH", data, offset + 4)
            archive_end = offset + 22 + comment_length
            if (
                disk_number == 0
                and directory_disk == 0
                and disk_members == member_count
                and total_members == member_count
                and directory_size != 0xFFFFFFFF
                and directory_offset != 0xFFFFFFFF
                and archive_end <= len(data)
            ):
                return offset, archive_end
        offset = data.find(b"PK\x05\x06", offset + 1)
    raise IntegrityError("environment launcher archive has no valid end record")


def _normalized_zip_executable(
    path: Path, environment: Path, original: bytes
) -> bytes:
    """Authenticate a ZIP launcher while normalizing only generated path bytes.

    Embedded member data is represented by normalized content hashes. The
    executable prefix, local and central framing, archive comment, padding, and
    any trailing overlay remain authenticated. Dynamic CRC, size, and offset
    fields are canonicalized because normalized member bytes can change them.
    """
    normalized = _replace_environment_paths(original, environment)
    if len(normalized) != len(original):
        raise IntegrityError(
            "environment launcher path normalization changed archive length"
        )
    try:
        with ZipFile(path) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > 4096:
                raise IntegrityError("environment launcher archive inventory is invalid")
            if sum(info.file_size for info in infos) > 64 * 1024 * 1024:
                raise IntegrityError("environment launcher archive is too large")
            if any(
                info.file_size >= 0xFFFFFFFF
                or info.compress_size >= 0xFFFFFFFF
                or info.header_offset >= 0xFFFFFFFF
                for info in infos
            ):
                raise IntegrityError("ZIP64 environment launchers are unsupported")

            local_infos = sorted(infos, key=lambda info: info.header_offset)
            directory_start = archive.start_dir
            if directory_start <= local_infos[-1].header_offset:
                raise IntegrityError("environment launcher archive layout is invalid")

            framing: list[bytes] = [normalized[: local_infos[0].header_offset]]
            members: list[tuple[str, str]] = []
            for index, info in enumerate(local_infos):
                offset = info.header_offset
                if original[offset : offset + 4] != b"PK\x03\x04" or offset + 30 > len(
                    original
                ):
                    raise IntegrityError("environment launcher local header is invalid")
                flags = struct.unpack_from("<H", original, offset + 6)[0]
                name_length, extra_length = struct.unpack_from("<HH", original, offset + 26)
                data_start = offset + 30 + name_length + extra_length
                data_end = data_start + info.compress_size
                boundary = (
                    local_infos[index + 1].header_offset
                    if index + 1 < len(local_infos)
                    else directory_start
                )
                if data_start > data_end or data_end > boundary:
                    raise IntegrityError("environment launcher member bounds are invalid")

                header = bytearray(normalized[offset:data_start])
                header[14:26] = b"\0" * 12
                framing.append(bytes(header))
                framing.append(b"<OCCULT_ZIP_MEMBER_DATA>")

                descriptor_end = data_end
                if flags & 0x08:
                    signed_descriptor = original[data_end : data_end + 4] == b"PK\x07\x08"
                    descriptor_size = 16 if signed_descriptor else 12
                    descriptor_end += descriptor_size
                    if descriptor_end > boundary:
                        raise IntegrityError(
                            "environment launcher data descriptor is invalid"
                        )
                    framing.append(
                        b"<OCCULT_ZIP_DESCRIPTOR_SIGNED>"
                        if signed_descriptor
                        else b"<OCCULT_ZIP_DESCRIPTOR>"
                    )
                framing.append(normalized[descriptor_end:boundary])

                with archive.open(info) as stream:
                    member = _replace_environment_paths(stream.read(), environment)
                members.append((info.filename, hashlib.sha256(member).hexdigest()))

            cursor = directory_start
            for info in infos:
                if original[cursor : cursor + 4] != b"PK\x01\x02" or cursor + 46 > len(
                    original
                ):
                    raise IntegrityError("environment launcher central header is invalid")
                name_length, extra_length, comment_length = struct.unpack_from(
                    "<HHH", original, cursor + 28
                )
                entry_end = cursor + 46 + name_length + extra_length + comment_length
                if entry_end > len(original):
                    raise IntegrityError("environment launcher central entry is invalid")
                entry = bytearray(normalized[cursor:entry_end])
                entry[16:28] = b"\0" * 12
                entry[42:46] = b"\0" * 4
                framing.append(bytes(entry))
                cursor = entry_end

            eocd_offset, archive_end = _end_of_central_directory(
                original, cursor, len(infos)
            )
            framing.append(normalized[cursor:eocd_offset])
            eocd = bytearray(normalized[eocd_offset:archive_end])
            eocd[12:20] = b"\0" * 8
            framing.append(bytes(eocd))
            framing.append(normalized[archive_end:])
    except IntegrityError:
        raise
    except (BadZipFile, KeyError, OSError, RuntimeError, struct.error) as error:
        raise IntegrityError("environment launcher archive is invalid") from error

    encoded_members = json.dumps(sorted(members), separators=(",", ":")).encode()
    return b"".join(framing) + b"\n<OCCULT_ZIP_V2>\n" + encoded_members


def _normalized_environment_file(path: Path, environment: Path) -> bytes:
    original = path.read_bytes()
    data = _replace_environment_paths(original, environment)
    archive_offset = original.find(b"PK\x03\x04")
    if archive_offset < 0 or path.suffix.lower() != ".exe":
        return data
    return _normalized_zip_executable(path, environment, original)


def _outside_environment_map(environment: Path, site_root: Path) -> dict[str, str]:
    root_status = environment.lstat()
    if not stat.S_ISDIR(root_status.st_mode) or _is_reparse(root_status):
        raise IntegrityError("environment root must be a real directory")
    root_mode = root_status.st_mode & 0o777
    result: dict[str, str] = {".": f"directory:{root_mode:o}"}
    for path in sorted(environment.rglob("*")):
        if _inside(path, site_root):
            continue
        relative = path.relative_to(environment).as_posix()
        status = path.lstat()
        if _is_reparse(status):
            raise IntegrityError("environment contains a reparse point")
        mode = status.st_mode & 0o777
        if stat.S_ISLNK(status.st_mode):
            # Bind the environment to the same host runtime path as the fresh
            # reference. Integrity of that shared host prerequisite is outside
            # what either virtual environment can independently establish.
            target = os.readlink(path).encode()
            normalized = _replace_environment_paths(target, environment).decode(
                errors="surrogateescape"
            )
            result[relative] = f"symlink:{mode:o}:{normalized}"
        elif stat.S_ISDIR(status.st_mode):
            result[relative] = f"directory:{mode:o}"
            continue
        elif stat.S_ISREG(status.st_mode):
            if status.st_nlink != 1:
                raise IntegrityError("environment contains a hard-linked file")
            normalized = _normalized_environment_file(path, environment)
            result[relative] = (
                f"file:{mode:o}:" + hashlib.sha256(normalized).hexdigest()
            )
        else:
            raise IntegrityError("environment contains an unsupported node")
    return result


def _normalized_launcher(environment: Path) -> bytes:
    return _normalized_environment_file(_launcher(environment), environment)


def verify_environment(existing: Path, reference: Path) -> None:
    try:
        existing_status = existing.lstat()
        reference_status = reference.lstat()
    except OSError as error:
        raise IntegrityError("environment paths are unreadable") from error
    if (
        not stat.S_ISDIR(existing_status.st_mode)
        or not stat.S_ISDIR(reference_status.st_mode)
        or _is_reparse(existing_status)
        or _is_reparse(reference_status)
    ):
        raise IntegrityError("environment roots must be real directories")
    existing = existing.resolve(strict=True)
    reference = reference.resolve(strict=True)
    if existing == reference:
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
    if _outside_environment_map(existing, existing_site) != _outside_environment_map(
        reference, reference_site
    ):
        raise IntegrityError("installed environment runtime differs")
    _validate_bytecode(existing_site, set(existing_files))
    if _normalized_launcher(existing) != _normalized_launcher(reference):
        raise IntegrityError("Hermes launcher differs")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--compare-bytecode-manifest":
        return _compare_bytecode_manifest(Path(sys.argv[2]))
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
