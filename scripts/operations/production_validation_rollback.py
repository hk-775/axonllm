#!/usr/bin/env python3
"""Tamper-evident local journal for production-validation rollbacks."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit


SCHEMA = "axonllm.production-validation-rollback-journal/v1"
RECONCILIATION_SCHEMA = (
    "axonllm.production-validation-rollback-reconciliation/v1"
)
MAX_JOURNAL_BYTES = 256 * 1024
MAX_ENTRIES = 100
ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
ENTRY_ID = re.compile(r"^[0-9a-f]{64}$")
JOURNAL_ID = re.compile(r"^[0-9a-f]{32}$")
MAC = re.compile(r"^[0-9a-f]{64}$")
PROJECT_PATH = re.compile(r"^/admin/projects/[^/?#]{1,256}$")
COOKIE_CREDENTIAL_TYPES = frozenset(
    {
        "alb-session-cookie",
        "browser-session-cookie",
    }
)
REVERSIBLE_FIELDS = frozenset(
    {
        "cache_enabled",
        "cache_ttl_seconds",
        "log_level",
        "name",
        "prompt_caching_enabled",
        "semantic_cache_enabled",
        "semantic_cache_threshold",
    }
)


class RollbackJournalError(RuntimeError):
    """Raised when rollback state cannot be trusted or persisted."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RollbackJournalError("rollback journal contains duplicate fields")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise RollbackJournalError("rollback journal contains a non-finite number")


def _canonical(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RollbackJournalError(
            "rollback journal contains unsupported JSON"
        ) from exc


def _safe_scalar(value: Any) -> bool:
    if value is None or type(value) in {bool, int}:
        return True
    if type(value) is float:
        return math.isfinite(value)
    return (
        isinstance(value, str)
        and value == value.strip()
        and 1 <= len(value) <= 512
        and not any(ord(character) < 32 for character in value)
    )


def _values(value: Any, location: str) -> dict[str, Any]:
    if (
        type(value) is not dict
        or not value
        or not set(value).issubset(REVERSIBLE_FIELDS)
        or any(not _safe_scalar(item) for item in value.values())
    ):
        raise RollbackJournalError(
            f"rollback journal {location} is invalid"
        )
    return dict(value)


def _endpoint(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 2048
        or any(ord(character) < 32 for character in value)
    ):
        raise RollbackJournalError("rollback journal endpoint is invalid")
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError as exc:
        raise RollbackJournalError(
            "rollback journal endpoint is invalid"
        ) from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise RollbackJournalError("rollback journal endpoint is invalid")
    return value


def _entry(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) != {
        "id",
        "endpoint",
        "path",
        "credentialEnv",
        "credentialType",
        "csrfTokenEnv",
        "timeoutSeconds",
        "priorRevision",
        "priorValues",
        "mutationValues",
        "state",
        "mutationRevision",
        "restoredRevision",
        "preparedAt",
        "updatedAt",
    }:
        raise RollbackJournalError(
            "rollback journal entry fields do not match schema"
        )
    entry_id = value["id"]
    endpoint = _endpoint(value["endpoint"])
    path = value["path"]
    credential_env = value["credentialEnv"]
    csrf_env = value["csrfTokenEnv"]
    timeout_seconds = value["timeoutSeconds"]
    prior_revision = value["priorRevision"]
    prior_values = _values(value["priorValues"], "prior values")
    mutation_values = _values(value["mutationValues"], "mutation values")
    mutation_revision = value["mutationRevision"]
    restored_revision = value["restoredRevision"]
    state_value = value["state"]
    if (
        not isinstance(entry_id, str)
        or ENTRY_ID.fullmatch(entry_id) is None
        or not isinstance(path, str)
        or PROJECT_PATH.fullmatch(path) is None
        or not isinstance(credential_env, str)
        or ENVIRONMENT_NAME.fullmatch(credential_env) is None
        or value["credentialType"] not in COOKIE_CREDENTIAL_TYPES
        or not isinstance(csrf_env, str)
        or ENVIRONMENT_NAME.fullmatch(csrf_env) is None
        or credential_env == csrf_env
        or isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or not 0.1 <= float(timeout_seconds) <= 300
        or type(prior_revision) is not int
        or prior_revision < 0
        or set(prior_values) != set(mutation_values)
        or prior_values == mutation_values
        or state_value not in {"PENDING", "COMPLETE"}
        or (
            mutation_revision is not None
            and (
                type(mutation_revision) is not int
                or mutation_revision <= prior_revision
            )
        )
        or (
            restored_revision is not None
            and (
                type(restored_revision) is not int
                or restored_revision < prior_revision
            )
        )
        or (state_value == "PENDING" and restored_revision is not None)
        or (state_value == "COMPLETE" and restored_revision is None)
        or not isinstance(value["preparedAt"], str)
        or not value["preparedAt"]
        or not isinstance(value["updatedAt"], str)
        or not value["updatedAt"]
    ):
        raise RollbackJournalError("rollback journal entry is invalid")
    identity = {
        "endpoint": endpoint,
        "path": path,
        "credentialEnv": credential_env,
        "credentialType": value["credentialType"],
        "csrfTokenEnv": csrf_env,
        "timeoutSeconds": float(timeout_seconds),
        "priorRevision": prior_revision,
        "priorValues": prior_values,
        "mutationValues": mutation_values,
    }
    if hashlib.sha256(_canonical(identity)).hexdigest() != entry_id:
        raise RollbackJournalError(
            "rollback journal entry identity is invalid"
        )
    return {
        **identity,
        "id": entry_id,
        "state": state_value,
        "mutationRevision": mutation_revision,
        "restoredRevision": restored_revision,
        "preparedAt": value["preparedAt"],
        "updatedAt": value["updatedAt"],
    }


class RollbackJournal:
    """Atomic rollback journal whose contents are authenticated by a sidecar key."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], str],
    ) -> None:
        self.path = path
        self.key_path = path.with_name(f".{path.name}.key")
        self.lock_path = path.with_name(f".{path.name}.lock")
        self._clock = clock

    @classmethod
    def create(
        cls,
        path: Path,
        *,
        clock: Callable[[], str],
    ) -> RollbackJournal:
        journal = cls(path, clock=clock)
        with journal._lock():
            if journal.path.exists():
                raise RollbackJournalError(
                    "rollback journal already exists"
                )
            key = journal._load_or_create_key()
            document = {
                "schema": SCHEMA,
                "journalId": secrets.token_hex(16),
                "revision": 0,
                "entries": {},
            }
            journal._write(document, key)
        return journal

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        clock: Callable[[], str],
    ) -> RollbackJournal:
        journal = cls(path, clock=clock)
        with journal._lock():
            journal._load()
        return journal

    def _lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                self.lock_path,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            lock_file = os.fdopen(descriptor, "r+b")
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise RollbackJournalError(
                "cannot lock rollback journal"
            ) from exc

        class _Lock:
            def __enter__(self_nonlocal):
                return lock_file

            def __exit__(self_nonlocal, *_args: object) -> None:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                finally:
                    lock_file.close()

        return _Lock()

    def _load_or_create_key(self) -> bytes:
        try:
            descriptor = os.open(
                self.key_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
        except FileExistsError:
            return self._load_key()
        except OSError as exc:
            raise RollbackJournalError(
                "cannot create rollback journal key"
            ) from exc
        key = secrets.token_bytes(32)
        try:
            with os.fdopen(descriptor, "wb") as key_file:
                key_file.write(key)
                key_file.flush()
                os.fsync(key_file.fileno())
        except OSError as exc:
            raise RollbackJournalError(
                "cannot persist rollback journal key"
            ) from exc
        return key

    def _load_key(self) -> bytes:
        try:
            descriptor = os.open(
                self.key_path,
                os.O_RDONLY | os.O_NOFOLLOW,
            )
            with os.fdopen(descriptor, "rb") as key_file:
                key_stat = os.fstat(key_file.fileno())
                key = key_file.read(33)
        except OSError as exc:
            raise RollbackJournalError(
                "cannot read rollback journal key"
            ) from exc
        if (
            not stat.S_ISREG(key_stat.st_mode)
            or key_stat.st_mode & 0o077
            or len(key) != 32
        ):
            raise RollbackJournalError("rollback journal key is unsafe")
        return key

    @staticmethod
    def _signed(document: Mapping[str, Any], key: bytes) -> dict[str, Any]:
        body = dict(document)
        body.pop("mac", None)
        return {
            **body,
            "mac": hmac.new(key, _canonical(body), hashlib.sha256).hexdigest(),
        }

    def _load(self) -> tuple[dict[str, Any], bytes]:
        key = self._load_key()
        try:
            path_stat = self.path.lstat()
            if (
                stat.S_ISLNK(path_stat.st_mode)
                or not stat.S_ISREG(path_stat.st_mode)
                or path_stat.st_mode & 0o077
                or path_stat.st_size > MAX_JOURNAL_BYTES
            ):
                raise RollbackJournalError("rollback journal file is unsafe")
            raw = self.path.read_bytes()
            final_stat = self.path.stat()
        except OSError as exc:
            raise RollbackJournalError(
                "cannot read rollback journal"
            ) from exc
        if (
            path_stat.st_dev != final_stat.st_dev
            or path_stat.st_ino != final_stat.st_ino
            or path_stat.st_size != final_stat.st_size
            or len(raw) != final_stat.st_size
        ):
            raise RollbackJournalError(
                "rollback journal changed while reading"
            )
        try:
            value = json.loads(
                raw,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RollbackJournalError(
                "rollback journal is not strict UTF-8 JSON"
            ) from exc
        if type(value) is not dict or set(value) != {
            "schema",
            "journalId",
            "revision",
            "entries",
            "mac",
        }:
            raise RollbackJournalError(
                "rollback journal fields do not match schema"
            )
        supplied_mac = value.pop("mac")
        expected_mac = hmac.new(
            key,
            _canonical(value),
            hashlib.sha256,
        ).hexdigest()
        if (
            not isinstance(supplied_mac, str)
            or MAC.fullmatch(supplied_mac) is None
            or not hmac.compare_digest(supplied_mac, expected_mac)
        ):
            raise RollbackJournalError(
                "rollback journal authentication failed"
            )
        entries = value["entries"]
        if (
            value["schema"] != SCHEMA
            or not isinstance(value["journalId"], str)
            or JOURNAL_ID.fullmatch(value["journalId"]) is None
            or type(value["revision"]) is not int
            or value["revision"] < 0
            or type(entries) is not dict
            or len(entries) > MAX_ENTRIES
        ):
            raise RollbackJournalError("rollback journal is invalid")
        normalized_entries: dict[str, Any] = {}
        for entry_id, raw_entry in entries.items():
            entry = _entry(raw_entry)
            if entry_id != entry["id"]:
                raise RollbackJournalError(
                    "rollback journal entry key is invalid"
                )
            normalized_entries[entry_id] = entry
        return {
            "schema": SCHEMA,
            "journalId": value["journalId"],
            "revision": value["revision"],
            "entries": normalized_entries,
        }, key

    def _write(self, document: Mapping[str, Any], key: bytes) -> None:
        encoded = _canonical(self._signed(document, key)) + b"\n"
        if len(encoded) > MAX_JOURNAL_BYTES:
            raise RollbackJournalError("rollback journal exceeds size limit")
        temporary: str | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            temporary = None
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise RollbackJournalError(
                "cannot persist rollback journal"
            ) from exc
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def prepare(
        self,
        *,
        endpoint: str,
        path: str,
        credential_env: str,
        credential_type: str,
        csrf_token_env: str | None,
        timeout_seconds: float,
        prior_revision: int,
        prior_values: Mapping[str, Any],
        mutation_values: Mapping[str, Any],
    ) -> str:
        identity = {
            "endpoint": endpoint,
            "path": path,
            "credentialEnv": credential_env,
            "credentialType": credential_type,
            "csrfTokenEnv": csrf_token_env,
            "timeoutSeconds": float(timeout_seconds),
            "priorRevision": prior_revision,
            "priorValues": dict(prior_values),
            "mutationValues": dict(mutation_values),
        }
        entry_id = hashlib.sha256(_canonical(identity)).hexdigest()
        prepared_at = self._clock()
        raw_entry = {
            **identity,
            "id": entry_id,
            "state": "PENDING",
            "mutationRevision": None,
            "restoredRevision": None,
            "preparedAt": prepared_at,
            "updatedAt": prepared_at,
        }
        entry = _entry(raw_entry)
        with self._lock():
            document, key = self._load()
            if entry_id in document["entries"]:
                raise RollbackJournalError(
                    "rollback journal entry already exists"
                )
            if any(
                item["endpoint"] == endpoint and item["path"] == path
                for item in document["entries"].values()
            ):
                raise RollbackJournalError(
                    "rollback journal already owns this project endpoint"
                )
            document["entries"][entry_id] = entry
            document["revision"] += 1
            self._write(document, key)
        return entry_id

    def mark_mutation_revision(
        self,
        entry_id: str,
        mutation_revision: int,
    ) -> None:
        with self._lock():
            document, key = self._load()
            entry = document["entries"].get(entry_id)
            if entry is None or entry["state"] != "PENDING":
                raise RollbackJournalError(
                    "rollback journal entry is not pending"
                )
            if (
                type(mutation_revision) is not int
                or mutation_revision <= entry["priorRevision"]
                or (
                    entry["mutationRevision"] is not None
                    and entry["mutationRevision"] != mutation_revision
                )
            ):
                raise RollbackJournalError(
                    "rollback mutation revision is invalid"
                )
            entry["mutationRevision"] = mutation_revision
            entry["updatedAt"] = self._clock()
            document["revision"] += 1
            self._write(document, key)

    def mark_complete(
        self,
        entry_id: str,
        restored_revision: int,
    ) -> None:
        with self._lock():
            document, key = self._load()
            entry = document["entries"].get(entry_id)
            if entry is None:
                raise RollbackJournalError(
                    "rollback journal entry is missing"
                )
            if entry["state"] == "COMPLETE":
                return
            if (
                type(restored_revision) is not int
                or restored_revision < entry["priorRevision"]
            ):
                raise RollbackJournalError(
                    "rollback restored revision is invalid"
                )
            entry["state"] = "COMPLETE"
            entry["restoredRevision"] = restored_revision
            entry["updatedAt"] = self._clock()
            document["revision"] += 1
            self._write(document, key)

    def entries(self, *, pending_only: bool = False) -> tuple[dict[str, Any], ...]:
        with self._lock():
            document, _key = self._load()
        return tuple(
            dict(entry)
            for entry in document["entries"].values()
            if not pending_only or entry["state"] == "PENDING"
        )

    def summary(self) -> dict[str, Any]:
        with self._lock():
            document, _key = self._load()
        entries = tuple(document["entries"].values())
        pending = sum(entry["state"] == "PENDING" for entry in entries)
        return {
            "schema": RECONCILIATION_SCHEMA,
            "journalId": document["journalId"],
            "journalRevision": document["revision"],
            "status": "COMPLETE" if pending == 0 else "PENDING",
            "entryCount": len(entries),
            "completedEntries": len(entries) - pending,
            "pendingEntries": pending,
        }
