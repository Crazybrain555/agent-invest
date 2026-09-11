"""Independent tests for the bounded v2 diagnostic journal (WP-E evidence container).

The journal is only a single-writer, bounded evidence container with an original
budget binding.  Nothing here treats a record as a provider ACK, a PDF parse, a
publication or any completed HTTP lifecycle step.  Every case uses a disposable
temporary root; the expected on-disk encoding (canonical JSON, SHA-256 chain) is
recomputed independently because the container's whole purpose is that an
outside reader can verify it.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import (
    DiagnosticJournal,
    DiagnosticJournalError,
    DiagnosticJournalRecord,
)

JOURNAL_MODULE = "disclosure_anchor.adapters.runtime.mineru_diagnostic_journal"
CONFIG = "sha256:" + "1" * 64
CLOCK_ID = "sha256:" + "2" * 64
ATTEMPT = "attempt-synthetic-1"
SECOND = 1_000_000_000
START = 5 * SECOND
DEADLINE = START + 3600 * SECOND
MAX_RECORDS = 96
MAX_RECORD_BYTES = 2 * 1024 * 1024 + 8192
MAX_TOTAL_BYTES = 16 * 1024 * 1024
HEADER = "00-journal.json"
PENDING = "append-pending.json"
LOCK = "owner.lock"


class _Clock:
    def __init__(self, now: int) -> None:
        self.now: Any = now

    def __call__(self) -> Any:
        return self.now


class _FailOnCall:
    """Delegate until the ``ordinal``-th call, which raises (optionally after delegating)."""

    def __init__(self, ordinal: int, delegate: Callable[..., Any], *, delegate_first: bool = False) -> None:
        self.ordinal = ordinal
        self.delegate = delegate
        self.delegate_first = delegate_first
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.calls == self.ordinal:
            if self.delegate_first:
                self.delegate(*args, **kwargs)
            raise OSError(f"synthetic storage failure on call {self.ordinal}")
        return self.delegate(*args, **kwargs)


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def files(root: Path) -> dict[str, bytes]:
    return {
        name: (root / name).read_bytes()
        for name in sorted(os.listdir(root))
        if stat.S_ISREG(os.lstat(root / name).st_mode)
    }


def fd_count() -> int | None:
    try:
        return len(os.listdir("/dev/fd"))
    except OSError:
        return None


def exception_leaves(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [leaf for member in error.exceptions for leaf in exception_leaves(member)]
    return [error]


def rewrite(path: Path, raw: bytes) -> None:
    """Replace file content in place; the mode of the existing file is kept."""
    path.write_bytes(raw)


class _JournalCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "journal"
        self.clock = _Clock(START)

    def open(
        self,
        *,
        create: bool,
        root: Path | None = None,
        clock: Callable[[], int] | None = None,
        attempt: str = ATTEMPT,
        config: str = CONFIG,
        clock_id: str = CLOCK_ID,
        deadline: int = DEADLINE,
    ) -> DiagnosticJournal:
        return DiagnosticJournal(
            root if root is not None else self.root,
            create=create,
            attempt_id=attempt,
            configuration_sha256=config,
            clock_identity_sha256=clock_id,
            deadline_ns=deadline,
            continuous_ns=clock if clock is not None else self.clock,
        )

    def populated(self, root: Path, steps: tuple[str, ...] = ("submit_intent", "poll", "terminal")) -> dict[str, bytes]:
        # A new root is an independent clock scenario, never a rewind of an active owner.
        self.clock = _Clock(START)
        journal = self.open(create=True, root=root)
        try:
            for index, step in enumerate(steps, 1):
                self.clock.now = START + index * SECOND
                journal.append(step, {"k": index, "step": step})
        finally:
            journal.close()
        return files(root)


class JournalOwnershipAndRecoveryTests(_JournalCase):
    def test_new_journal_is_private_owned_and_records_recover_exactly_after_reopen(self) -> None:
        journal = self.open(create=True)
        root_stat = os.lstat(self.root)
        self.assertTrue(stat.S_ISDIR(root_stat.st_mode))
        self.assertEqual(stat.S_IMODE(root_stat.st_mode), 0o700)
        self.assertEqual(root_stat.st_uid, os.getuid())
        lock_stat = os.lstat(self.root / LOCK)
        self.assertTrue(stat.S_ISREG(lock_stat.st_mode))
        self.assertEqual((stat.S_IMODE(lock_stat.st_mode), lock_stat.st_nlink), (0o600, 1))
        header_raw = (self.root / HEADER).read_bytes()
        header = json.loads(header_raw)
        self.assertEqual(canonical(header), header_raw)
        self.assertEqual(
            header,
            {
                "contract_version": "mineru-diagnostic-journal.v2",
                "attempt_id": ATTEMPT,
                "configuration_sha256": CONFIG,
                "clock_identity_sha256": CLOCK_ID,
                "deadline_ns": DEADLINE,
                "root_identity": [root_stat.st_dev, root_stat.st_ino, root_stat.st_mode, root_stat.st_uid],
                "started_ns": START,
            },
        )
        self.assertEqual(journal.records, ())
        self.assertEqual(journal.remaining_seconds(), 30.0)

        self.clock.now = START + 2 * SECOND
        value = {"b": 1, "a": {"nested": [1, 2, {"z": "ü"}]}}
        original = json.loads(json.dumps(value))
        first = journal.append("submit_intent", value)
        self.assertEqual(
            (first.sequence, first.step, first.value, first.observed_ns),
            (1, "submit_intent", original, START + 2 * SECOND),
        )
        raw1 = (self.root / "0001-submit_intent.json").read_bytes()
        self.assertEqual(stat.S_IMODE(os.lstat(self.root / "0001-submit_intent.json").st_mode), 0o600)
        self.assertEqual(first.sha256, digest(raw1))
        data1 = json.loads(raw1)
        self.assertEqual(canonical(data1), raw1)
        self.assertEqual(
            data1,
            {
                "contract_version": "mineru-diagnostic-record.v2",
                "sequence": 1,
                "step": "submit_intent",
                "previous_sha256": digest(header_raw),
                "value_sha256": digest(canonical(original)),
                "value": original,
                "observed_ns": START + 2 * SECOND,
            },
        )

        value["a"]["nested"].append("late mutation")
        first.value["b"] = 2
        self.assertEqual(journal.records[0].value, original)
        exposed = journal.records
        exposed[0].value["a"]["nested"].clear()
        self.assertEqual(journal.records[0].value, original)

        self.clock.now = START + 3 * SECOND
        second = journal.append("poll", {"status": "pending"})
        raw2 = (self.root / "0002-poll.json").read_bytes()
        self.assertEqual(json.loads(raw2)["previous_sha256"], first.sha256)
        self.assertEqual(second.sha256, digest(raw2))
        self.assertNotIn(PENDING, os.listdir(self.root))
        before = files(self.root)
        journal.close()

        self.clock.now = START + 10 * SECOND
        resumed = self.open(create=False)
        try:
            self.assertEqual(
                resumed.records,
                (
                    DiagnosticJournalRecord(1, "submit_intent", original, first.sha256, START + 2 * SECOND),
                    DiagnosticJournalRecord(2, "poll", {"status": "pending"}, second.sha256, START + 3 * SECOND),
                ),
            )
            self.assertEqual(files(self.root), before)
            self.assertEqual(resumed.remaining_seconds(), 30.0)
            self.clock.now = DEADLINE - 4 * SECOND
            self.assertEqual(resumed.remaining_seconds(), 4.0)
        finally:
            resumed.close()

    def test_payload_mutation_during_append_cannot_diverge_disk_cache_or_return(self) -> None:
        journal = self.open(create=True)
        self.addCleanup(journal.close)
        value = {"nested": {"items": ["original"]}, "count": 1}
        expected = {"nested": {"items": ["original"]}, "count": 1}
        write_new = journal._write_new
        writes: list[str] = []

        def mutate_at_io(name: str, raw: bytes) -> None:
            writes.append(name)
            if name == PENDING:
                value["nested"]["items"].append("caller mutation during IO")
                value["count"] = 2
            write_new(name, raw)

        with patch.object(journal, "_write_new", side_effect=mutate_at_io):
            returned = journal.append("submit_intent", value)
        self.assertIn(PENDING, writes)
        self.assertNotEqual(value, expected, "the owned caller mutation must actually happen")
        raw = (self.root / "0001-submit_intent.json").read_bytes()
        persisted = json.loads(raw)
        self.assertEqual(persisted["value"], expected)
        self.assertEqual(persisted["value_sha256"], digest(canonical(expected)))
        self.assertEqual(returned.sha256, digest(raw))
        self.assertEqual(returned.value, expected)
        self.assertEqual(journal.records[0].value, expected)
        journal.close()
        resumed = self.open(create=False)
        try:
            self.assertEqual(resumed.records, (returned,))
        finally:
            resumed.close()

    def test_second_writer_is_rejected_until_the_exact_owner_closes(self) -> None:
        owner = self.open(create=True)
        before = fd_count()
        with self.assertRaises((DiagnosticJournalError, OSError)):
            self.open(create=False)
        if before is not None:
            self.assertEqual(fd_count(), before)
        with self.assertRaises(FileExistsError):
            self.open(create=True)
        self.clock.now = START + SECOND
        owner.append("still_owner", {"ok": True})
        self.assertEqual(sorted(os.listdir(self.root)), [HEADER, "0001-still_owner.json", LOCK])
        owner.close()
        with self.assertRaisesRegex(DiagnosticJournalError, "closed"):
            owner.append("late", {})
        successor = self.open(create=False)
        try:
            self.assertEqual([record.step for record in successor.records], ["still_owner"])
            self.clock.now = START + 2 * SECOND
            self.assertEqual(successor.append("poll", {}).sequence, 2)
        finally:
            successor.close()

    def test_resume_cannot_reset_deadline_clock_configuration_or_attempt(self) -> None:
        before = self.populated(self.root, ("submit_intent",))
        lock_inode = os.lstat(self.root / LOCK).st_ino
        for label, overrides in (
            ("attempt", {"attempt": "attempt-synthetic-2"}),
            ("configuration", {"config": "sha256:" + "3" * 64}),
            ("clock_identity", {"clock_id": "sha256:" + "4" * 64}),
            ("shorter_deadline", {"deadline": DEADLINE - SECOND}),
            ("longer_deadline", {"deadline": DEADLINE + SECOND}),
        ):
            with self.subTest(changed=label):
                with self.assertRaisesRegex(DiagnosticJournalError, "changed"):
                    self.open(create=False, **overrides)
                self.assertEqual(files(self.root), before)
                self.assertEqual(os.lstat(self.root / LOCK).st_ino, lock_inode)

        for label, overrides in (
            ("empty_attempt", {"attempt": ""}),
            ("control_character_attempt", {"attempt": "attempt\n1"}),
            ("bad_configuration_hash", {"config": "1" * 64}),
            ("bad_clock_hash", {"clock_id": "sha256:xyz"}),
            ("zero_deadline", {"deadline": 0}),
        ):
            with self.subTest(invalid=label):
                fresh = self.base / f"invalid-{label}"
                with self.assertRaisesRegex(DiagnosticJournalError, "explicit bounded"):
                    self.open(create=True, root=fresh, **overrides)
                self.assertFalse(fresh.exists())

        self.clock.now = START + 2 * SECOND
        resumed = self.open(create=False)
        try:
            self.assertEqual(resumed.append("poll", {"k": 2}).sequence, 2)
        finally:
            resumed.close()

    def test_regressed_clock_poison_is_not_cleared_by_a_later_good_clock(self) -> None:
        journal = self.open(create=True)
        self.clock.now = START + 2 * SECOND
        journal.append("submit_intent", {"k": 1})
        before = files(self.root)
        self.clock.now = START + SECOND
        with self.assertRaisesRegex(DiagnosticJournalError, "regressed"):
            journal.remaining_seconds()
        self.clock.now = START + 5 * SECOND
        with self.assertRaisesRegex(DiagnosticJournalError, "poisoned"):
            journal.remaining_seconds()
        with self.assertRaisesRegex(DiagnosticJournalError, "poisoned"):
            journal.append("poll", {"k": 2})
        self.assertEqual(files(self.root), before)
        journal.close()

        with self.subTest(resume="clock behind the last record"):
            self.clock.now = START + SECOND
            resumed = self.open(create=False)
            try:
                with self.assertRaisesRegex(DiagnosticJournalError, "regressed"):
                    resumed.append("poll", {"k": 2})
                self.clock.now = START + 9 * SECOND
                with self.assertRaisesRegex(DiagnosticJournalError, "poisoned"):
                    resumed.append("poll", {"k": 2})
                self.assertEqual(files(self.root), before)
            finally:
                resumed.close()

        with self.subTest(resume="non-integer or negative clock values"):
            for bad in (1.5, -1, True, "7"):
                self.clock.now = START + 9 * SECOND
                resumed = self.open(create=False)
                try:
                    self.clock.now = bad
                    with self.assertRaisesRegex(DiagnosticJournalError, "invalid or regressed"):
                        resumed.remaining_seconds()
                    self.clock.now = START + 9 * SECOND
                    with self.assertRaisesRegex(DiagnosticJournalError, "poisoned"):
                        resumed.append("poll", {"k": 2})
                finally:
                    resumed.close()
            self.assertEqual(files(self.root), before)

    def test_deadline_expiry_is_a_timeout_that_writes_nothing(self) -> None:
        journal = self.open(create=True)
        before = files(self.root)
        self.clock.now = DEADLINE - 1
        self.assertGreater(journal.remaining_seconds(), 0.0)
        self.assertLess(journal.remaining_seconds(), 1e-6)
        self.clock.now = DEADLINE
        with self.assertRaisesRegex(TimeoutError, "original diagnostic deadline"):
            journal.remaining_seconds()
        with self.assertRaises(TimeoutError):
            journal.append("late", {"k": 1})
        self.assertEqual(files(self.root), before)
        journal.close()

        for label, deadline, expected in (
            ("too-long", START + 7201 * SECOND, DiagnosticJournalError),
            ("already-expired", START, TimeoutError),
        ):
            with self.subTest(initial_budget=label):
                root = self.base / label
                with self.assertRaises(expected):
                    self.open(create=True, root=root, deadline=deadline, clock=_Clock(START))
                self.assertFalse(root.exists())
        within = self.open(
            create=True, root=self.base / "max-lifetime",
            deadline=START + 7200 * SECOND, clock=_Clock(START),
        )
        within.close()


class JournalPendingAppendTests(_JournalCase):
    def test_failed_record_flush_leaves_pending_and_never_retries(self) -> None:
        for ordinal, expectation in ((1, "pending"), (3, "pending"), (5, "recorded")):
            with self.subTest(fsync_call=ordinal):
                root = self.base / f"flush-{ordinal}"
                self.clock = _Clock(START)
                journal = self.open(create=True, root=root)
                self.clock.now = START + SECOND
                journal.append("submit_intent", {"k": 1})
                before = files(root)
                self.clock.now = START + 2 * SECOND
                fault = _FailOnCall(ordinal, os.fsync)
                with patch(f"{JOURNAL_MODULE}.os.fsync", fault), self.assertRaises(OSError) as raised:
                    journal.append("poll", {"k": 2})
                self.assertNotIsInstance(raised.exception, DiagnosticJournalError)
                self.assertEqual(fault.calls, ordinal)
                after = files(root)
                for name, raw in before.items():
                    self.assertEqual(after[name], raw, name)
                self.assertEqual([record.step for record in journal.records], ["submit_intent"])
                with self.assertRaisesRegex(DiagnosticJournalError, "poisoned"):
                    journal.append("poll", {"k": 2})
                with self.assertRaisesRegex(DiagnosticJournalError, "poisoned"):
                    journal.remaining_seconds()
                self.assertEqual(files(root), after)
                journal.close()
                if expectation == "pending":
                    self.assertIn(PENDING, after)
                    pending = json.loads(after[PENDING])
                    self.assertEqual(canonical(pending), after[PENDING])
                    self.assertEqual(set(pending), {"record", "sha256"})
                    self.assertEqual(pending["record"], "0002-poll.json")
                    with self.assertRaisesRegex(DiagnosticJournalError, "uncertain"):
                        self.open(create=False, root=root)
                    self.assertEqual(files(root), after)
                else:
                    self.assertNotIn(PENDING, after)
                    resumed = self.open(create=False, root=root)
                    try:
                        self.assertEqual([record.step for record in resumed.records], ["submit_intent", "poll"])
                        self.assertEqual(resumed.records[1].sha256, digest(after["0002-poll.json"]))
                    finally:
                        resumed.close()

    def test_pending_append_from_a_previous_owner_blocks_resume_and_preserves_bytes(self) -> None:
        self.populated(self.root, ("submit_intent",))
        descriptor = os.open(self.root / PENDING, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, canonical({"record": "0002-poll.json", "sha256": "sha256:" + "0" * 64}))
        finally:
            os.close(descriptor)
        before = files(self.root)
        with self.assertRaisesRegex(DiagnosticJournalError, "uncertain"):
            self.open(create=False)
        self.assertEqual(files(self.root), before)

    def test_invalid_step_or_payload_is_rejected_before_any_write(self) -> None:
        journal = self.open(create=True)
        self.clock.now = START + SECOND
        before = files(self.root)
        for step, value in (
            ("Submit", {}),
            ("1abc", {}),
            ("", {}),
            ("a" * 49, {}),
            ("has-dash", {}),
            ("ok_step", ["not", "an", "object"]),
            ("ok_step", None),
        ):
            with self.subTest(step=step, value=value):
                with self.assertRaisesRegex(DiagnosticJournalError, "closed diagnostic step"):
                    journal.append(step, value)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            journal.append("nan_value", {"x": float("nan")})
        for label, value in (
            ("opaque_value", {"x": object()}),
            ("tuple_value", {"x": (1, 2)}),
            ("integer_key", {1: "one"}),
            ("nested_integer_key", {"nested": [{1: "one"}]}),
        ):
            with self.subTest(payload=label):
                with self.assertRaises(DiagnosticJournalError):
                    journal.append(label, value)  # type: ignore[arg-type]
                self.assertEqual(files(self.root), before)
        self.assertEqual(files(self.root), before)
        self.assertEqual(journal.append("ok_step", {"fine": True}).sequence, 1)
        self.assertEqual(journal.append("a" * 48, {}).sequence, 2)
        journal.close()


class JournalEvidenceIntegrityTests(_JournalCase):
    def test_damaged_hash_gap_and_foreign_entry_preserve_original_files(self) -> None:
        def value_tamper(root: Path) -> None:
            path = root / "0002-poll.json"
            data = json.loads(path.read_bytes())
            data["value"]["k"] = 999
            rewrite(path, canonical(data))

        def rehashed_tamper(root: Path) -> None:
            path = root / "0002-poll.json"
            data = json.loads(path.read_bytes())
            data["value"]["k"] = 999
            data["value_sha256"] = digest(canonical(data["value"]))
            rewrite(path, canonical(data))

        def non_canonical(root: Path) -> None:
            path = root / "0002-poll.json"
            rewrite(path, json.dumps(json.loads(path.read_bytes()), indent=2).encode())

        def truncated(root: Path) -> None:
            path = root / "0002-poll.json"
            raw = path.read_bytes()
            rewrite(path, raw[: len(raw) // 2])

        def emptied(root: Path) -> None:
            rewrite(root / "0002-poll.json", b"")

        def gap(root: Path) -> None:
            (root / "0002-poll.json").unlink()

        def duplicate_sequence(root: Path) -> None:
            raw = (root / "0002-poll.json").read_bytes()
            descriptor = os.open(root / "0002-other.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(descriptor, raw)
            finally:
                os.close(descriptor)

        def foreign_name(root: Path) -> None:
            descriptor = os.open(root / "notes.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)

        def renamed_step(root: Path) -> None:
            (root / "0002-poll.json").rename(root / "0002-other.json")

        def hard_linked(root: Path) -> None:
            os.link(root / "0002-poll.json", root.parent / f"{root.name}-alias")

        def public_mode(root: Path) -> None:
            os.chmod(root / "0002-poll.json", 0o644)

        def symlinked_record(root: Path) -> None:
            path = root / "0002-poll.json"
            copy = root.parent / f"{root.name}-copy"
            copy.write_bytes(path.read_bytes())
            path.unlink()
            path.symlink_to(copy)

        def observed_before_previous(root: Path) -> None:
            path = root / "0003-terminal.json"
            data = json.loads(path.read_bytes())
            data["observed_ns"] = json.loads((root / "0002-poll.json").read_bytes())["observed_ns"] - 1
            rewrite(path, canonical(data))

        def observed_at_deadline(root: Path) -> None:
            path = root / "0003-terminal.json"
            data = json.loads(path.read_bytes())
            data["observed_ns"] = DEADLINE
            rewrite(path, canonical(data))

        def header_started_moved(root: Path) -> None:
            path = root / HEADER
            data = json.loads(path.read_bytes())
            data["started_ns"] = START + 1
            rewrite(path, canonical(data))

        def header_extra_field(root: Path) -> None:
            path = root / HEADER
            data = json.loads(path.read_bytes())
            data["ack"] = "consumed"
            rewrite(path, canonical(data))

        def resources_symlink(root: Path) -> None:
            (root / "resources").symlink_to(root.parent)

        def resources_public(root: Path) -> None:
            (root / "resources").mkdir(mode=0o755)

        tampers: tuple[tuple[str, Callable[[Path], None], type[BaseException] | tuple[type[BaseException], ...]], ...] = (
            ("value_tamper", value_tamper, DiagnosticJournalError),
            ("rehashed_tamper_breaks_next_link", rehashed_tamper, DiagnosticJournalError),
            ("non_canonical_bytes", non_canonical, DiagnosticJournalError),
            ("truncated_record", truncated, ValueError),
            ("empty_record", emptied, DiagnosticJournalError),
            ("sequence_gap", gap, DiagnosticJournalError),
            ("duplicate_sequence", duplicate_sequence, DiagnosticJournalError),
            ("foreign_name", foreign_name, DiagnosticJournalError),
            ("renamed_step", renamed_step, DiagnosticJournalError),
            ("hard_linked_record", hard_linked, DiagnosticJournalError),
            ("public_mode_record", public_mode, DiagnosticJournalError),
            ("symlinked_record", symlinked_record, (DiagnosticJournalError, OSError)),
            ("observed_before_previous", observed_before_previous, DiagnosticJournalError),
            ("observed_at_deadline", observed_at_deadline, DiagnosticJournalError),
            ("header_started_moved", header_started_moved, DiagnosticJournalError),
            ("header_extra_field", header_extra_field, DiagnosticJournalError),
            ("resources_symlink", resources_symlink, DiagnosticJournalError),
            ("resources_public", resources_public, DiagnosticJournalError),
        )
        for label, tamper, expected in tampers:
            with self.subTest(tamper=label):
                root = self.base / label
                self.populated(root)
                tamper(root)
                damaged = files(root)
                names = sorted(os.listdir(root))
                self.clock.now = START + 10 * SECOND
                with self.assertRaises(expected):
                    self.open(create=False, root=root)
                self.assertEqual(files(root), damaged)
                self.assertEqual(sorted(os.listdir(root)), names)
                self.assertNotIn(PENDING, names)

        with self.subTest(tamper="private_resources_directory_is_ignored"):
            root = self.base / "resources-private"
            pristine = self.populated(root)
            (root / "resources").mkdir(mode=0o700)
            self.clock.now = START + 10 * SECOND
            journal = self.open(create=False, root=root)
            try:
                self.assertEqual([record.sequence for record in journal.records], [1, 2, 3])
                self.assertEqual(files(root), pristine)
            finally:
                journal.close()

    def test_header_numeric_aliases_are_rejected_without_a_record_chain(self) -> None:
        for field in ("deadline_ns", "root_identity"):
            with self.subTest(header_field=field):
                root = self.base / f"numeric-alias-{field}"
                self.populated(root, ())
                header = json.loads((root / HEADER).read_bytes())
                if field == "deadline_ns":
                    header[field] = float(header[field])
                else:
                    header[field] = [float(number) for number in header[field]]
                # Python numerical equality accepts the alias; canonical identity must not.
                self.assertEqual(header, json.loads((root / HEADER).read_bytes()))
                changed = canonical(header)
                self.assertNotEqual(changed, (root / HEADER).read_bytes())
                rewrite(root / HEADER, changed)
                before = files(root)
                self.assertEqual(set(before), {HEADER, LOCK})
                with self.assertRaisesRegex(DiagnosticJournalError, "changed"):
                    self.open(create=False, root=root)
                self.assertEqual(files(root), before)

    def test_record_count_ceiling_is_96_and_the_97th_append_writes_nothing(self) -> None:
        journal = self.open(create=True)
        for index in range(MAX_RECORDS):
            self.clock.now = START + (index + 1) * SECOND
            journal.append(f"step_{index:02d}", {"i": index})
        self.assertEqual(len(journal.records), MAX_RECORDS)
        before = files(self.root)
        self.clock.now = START + 200 * SECOND
        with self.assertRaisesRegex(DiagnosticJournalError, "envelope"):
            journal.append("overflow", {"i": MAX_RECORDS})
        self.assertEqual(files(self.root), before)
        self.assertNotIn(PENDING, before)
        self.assertEqual(journal.remaining_seconds(), 30.0)
        journal.close()

        resumed = self.open(create=False)
        try:
            self.assertEqual(len(resumed.records), MAX_RECORDS)
            self.assertEqual(resumed.records[-1].sequence, MAX_RECORDS)
            with self.assertRaisesRegex(DiagnosticJournalError, "envelope"):
                resumed.append("overflow", {})
        finally:
            resumed.close()
        self.assertEqual(files(self.root), before)

        descriptor = os.open(self.root / "0097-late.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        with self.assertRaisesRegex(DiagnosticJournalError, "record count exceeded"):
            self.open(create=False)

    def test_record_and_total_byte_ceilings_reject_before_any_write(self) -> None:
        journal = self.open(create=True)
        self.clock.now = START + SECOND
        before = files(self.root)
        with self.assertRaisesRegex(DiagnosticJournalError, "envelope"):
            journal.append("huge", {"blob": "x" * MAX_RECORD_BYTES})
        self.assertEqual(files(self.root), before)

        blob = "x" * (2 * 1024 * 1024 - 8192)
        successes = 0
        for index in range(12):
            self.clock.now = START + (index + 2) * SECOND
            try:
                journal.append(f"chunk_{index:02d}", {"blob": blob})
            except DiagnosticJournalError as error:
                self.assertIn("envelope", str(error))
                break
            successes += 1
        else:
            self.fail("total byte envelope was never enforced")
        self.assertEqual(successes, 8)
        on_disk = files(self.root)
        total = sum(len(raw) for name, raw in on_disk.items() if name != LOCK)
        self.assertLessEqual(total, MAX_TOTAL_BYTES)
        self.assertGreater(total + len(blob), MAX_TOTAL_BYTES)
        self.assertEqual(len([name for name in on_disk if name.startswith("00") and name != HEADER]), successes)
        self.assertNotIn(PENDING, on_disk)
        self.clock.now = START + 30 * SECOND
        self.assertEqual(journal.append("small_still_fits", {"ok": True}).sequence, successes + 1)
        journal.close()
        resumed = self.open(create=False)
        try:
            self.assertEqual(len(resumed.records), successes + 1)
        finally:
            resumed.close()


class JournalWriteAuthorityTests(_JournalCase):
    def test_legacy_symlink_and_replaced_root_do_not_grant_write_authority(self) -> None:
        pristine = self.populated(self.root, ("submit_intent",))
        self.clock.now = START + 10 * SECOND

        with self.subTest(case="symlink_root"):
            link = self.base / "link"
            link.symlink_to(self.root)
            for create in (False, True):
                with self.assertRaisesRegex(DiagnosticJournalError, "explicit bounded"):
                    self.open(create=create, root=link)
            self.assertEqual(files(self.root), pristine)

        with self.subTest(case="relative_root"):
            with self.assertRaisesRegex(DiagnosticJournalError, "explicit bounded"):
                self.open(create=True, root=Path("relative/journal"))

        with self.subTest(case="public_root_mode"):
            os.chmod(self.root, 0o755)
            try:
                with self.assertRaisesRegex(DiagnosticJournalError, "private owned directory"):
                    self.open(create=False)
            finally:
                os.chmod(self.root, 0o700)
            self.assertEqual(files(self.root), pristine)

        with self.subTest(case="live_root_replacement"):
            journal = self.open(create=False)
            moved = self.base / "moved-away"
            try:
                journal.append("poll", {"k": 2})
                owned = files(self.root)
                os.rename(self.root, moved)
                self.root.mkdir(mode=0o700)
                with self.assertRaisesRegex(DiagnosticJournalError, "replaced"):
                    journal.append("late", {"k": 3})
                self.assertEqual(os.listdir(self.root), [])
                self.assertEqual(files(moved), owned)
                with self.assertRaises((DiagnosticJournalError, OSError)):
                    self.open(create=False)
            finally:
                journal.close()
            resumed = self.open(create=False, root=moved)
            try:
                self.assertEqual([record.step for record in resumed.records], ["submit_intent", "poll"])
            finally:
                resumed.close()
            self.root.rmdir()
            os.rename(moved, self.root)

        with self.subTest(case="lock_replaced_by_symlink"):
            lock = self.root / LOCK
            target = self.base / "lock-target"
            target.write_bytes(b"")
            os.chmod(target, 0o600)
            lock.unlink()
            lock.symlink_to(target)
            try:
                with self.assertRaises((DiagnosticJournalError, OSError)):
                    self.open(create=False)
            finally:
                lock.unlink()
                descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(descriptor)

        with self.subTest(case="lock_hard_linked"):
            alias = self.base / "lock-alias"
            os.link(self.root / LOCK, alias)
            try:
                with self.assertRaisesRegex(DiagnosticJournalError, "ownership or type"):
                    self.open(create=False)
            finally:
                alias.unlink()
            journal = self.open(create=False)
            journal.close()

    def test_replaced_owner_lock_cannot_yield_two_accepted_appends_for_one_sequence(self) -> None:
        owner = self.open(create=True)
        self.clock.now = START + SECOND
        owner.append("submit_intent", {"k": 1})
        lock = self.root / LOCK
        lock.unlink()
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        before = files(self.root)
        self.clock.now = START + 2 * SECOND
        for operation in (owner.remaining_seconds, lambda: owner.append("poll", {"k": 2})):
            with self.assertRaises(DiagnosticJournalError):
                operation()
            self.assertEqual(files(self.root), before)
            self.assertNotIn(PENDING, files(self.root))
        self.assertEqual([record.sequence for record in owner.records], [1])

        # Same-user replacement is detectable loss of the old writer's authority,
        # not a claim that a malicious owner can never obtain a fresh lock.
        intruder = self.open(create=False)
        try:
            second = intruder.append("poll", {"k": 2})
            accepted = files(self.root)
            self.assertEqual(second.sha256, digest(accepted["0002-poll.json"]))
            with self.assertRaises(DiagnosticJournalError):
                owner.append("poll", {"k": 3})
            self.assertEqual(files(self.root), accepted)
            self.assertNotIn(PENDING, accepted)
        finally:
            owner.close()
            intruder.close()
        resumed = self.open(create=False)
        try:
            self.assertEqual([record.sequence for record in resumed.records], [1, 2])
        finally:
            resumed.close()
        self.assertEqual(files(self.root), accepted)


class JournalHandleLifecycleTests(_JournalCase):
    def test_constructor_failure_closes_handles_and_preserves_the_primary_error(self) -> None:
        self.populated(self.root, ("submit_intent",))
        before = fd_count()
        with self.assertRaisesRegex(DiagnosticJournalError, "changed"):
            self.open(create=False, attempt="attempt-synthetic-2")
        if before is not None:
            self.assertEqual(fd_count(), before)
        resumed = self.open(create=False)
        resumed.close()

        close_fault = _FailOnCall(1, os.close, delegate_first=True)
        with patch(f"{JOURNAL_MODULE}.os.close", close_fault), self.assertRaises(BaseExceptionGroup) as raised:
            self.open(create=False, attempt="attempt-synthetic-2")
        errors = exception_leaves(raised.exception)
        self.assertEqual(len(errors), 2)
        self.assertTrue(any(isinstance(error, DiagnosticJournalError) and "changed" in str(error) for error in errors))
        self.assertTrue(any(type(error) is OSError and "synthetic storage failure" in str(error) for error in errors))
        if before is not None:
            self.assertEqual(fd_count(), before)

    def test_close_after_release_and_error_never_closes_a_reused_descriptor(self) -> None:
        real_close = os.close
        for attribute in ("_lock_fd", "_root_fd"):
            with self.subTest(owned_handle=attribute):
                journal = self.open(create=True, root=self.base / attribute)
                self.addCleanup(journal.close)
                target = getattr(journal, attribute)
                replacement = None
                attempted: list[int] = []
                close_error = OSError("synthetic close released the owned descriptor then failed")

                def release_reuse_then_fail(fd: int) -> None:
                    nonlocal replacement
                    attempted.append(fd)
                    real_close(fd)
                    if fd == target:
                        replacement = tempfile.TemporaryFile(dir=self.base)
                        self.assertEqual(replacement.fileno(), target, "fixture must exercise exact integer reuse")
                        raise close_error

                try:
                    with patch(f"{JOURNAL_MODULE}.os.close", release_reuse_then_fail):
                        with self.assertRaises(BaseExceptionGroup) as raised:
                            journal.close()
                        self.assertIn(close_error, exception_leaves(raised.exception))
                        self.assertIsNotNone(replacement)
                        before_second_close = tuple(attempted)
                        journal.close()
                        self.assertEqual(tuple(attempted), before_second_close)
                        replacement.write(b"still independently owned")
                        replacement.flush()
                        replacement.seek(0)
                        self.assertEqual(replacement.read(), b"still independently owned")
                    self.assertEqual(attempted.count(target), 1)
                finally:
                    if replacement is not None:
                        replacement.close()

    def test_body_and_close_failures_remain_explicitly_visible(self) -> None:
        journal = self.open(create=True)
        primary = LookupError("synthetic operation failure")
        close_fault = _FailOnCall(1, os.close, delegate_first=True)
        with patch(f"{JOURNAL_MODULE}.os.close", close_fault), self.assertRaises(BaseExceptionGroup) as raised:
            with journal:
                raise primary
        errors = exception_leaves(raised.exception)
        self.assertIn(primary, errors)
        self.assertTrue(any(type(error) is OSError and "synthetic storage failure" in str(error) for error in errors))
        journal.close()

    def test_parent_creation_failure_and_close_failure_preserve_both_errors(self) -> None:
        for phase in ("mkdir", "fsync"):
            with self.subTest(primary_phase=phase):
                primary = OSError(f"synthetic parent {phase} failure")
                close_fault = _FailOnCall(1, os.close, delegate_first=True)
                root = self.base / f"parent-{phase}"
                before = fd_count()
                with (
                    patch(f"{JOURNAL_MODULE}.os.{phase}", side_effect=primary),
                    patch(f"{JOURNAL_MODULE}.os.close", close_fault),
                    self.assertRaises(BaseExceptionGroup) as raised,
                ):
                    self.open(create=True, root=root)
                errors = exception_leaves(raised.exception)
                self.assertIn(primary, errors)
                self.assertTrue(any(type(error) is OSError and "synthetic storage failure" in str(error) for error in errors))
                self.assertEqual(close_fault.calls, 1)
                if before is not None:
                    self.assertEqual(fd_count(), before)
                if root.exists():
                    self.assertEqual(os.listdir(root), [])

    def test_close_is_idempotent_and_a_closed_journal_refuses_new_evidence(self) -> None:
        with self.open(create=True) as journal:
            self.clock.now = START + SECOND
            journal.append("submit_intent", {"k": 1})
        before = files(self.root)
        journal.close()
        with self.assertRaisesRegex(DiagnosticJournalError, "closed"):
            journal.remaining_seconds()
        with self.assertRaisesRegex(DiagnosticJournalError, "closed"):
            journal.append("late", {"k": 2})
        self.assertEqual(files(self.root), before)
        self.assertEqual([record.step for record in journal.records], ["submit_intent"])
        successor = self.open(create=False)
        successor.close()


if __name__ == "__main__":
    unittest.main()
