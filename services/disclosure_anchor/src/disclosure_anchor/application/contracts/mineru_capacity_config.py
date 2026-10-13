"""Explicit startup capacity for one MinerU API process and event loop.

This module also ships, byte for byte, as a standalone MinerU module. Keep it
stdlib-only. The bounds describe supported configuration, not measured hardware
capacity or a throughput recommendation. No runtime observations belong here.

The projection of a config onto its startup consumers (environment variables
and the shared HTTP limit argument) lives here as well, so the release builder
and the API bootstrap derive it from one function instead of two mappings.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any


CAPACITY_CONFIG_CONTRACT = "mineru.capacity-config.v1"
_MAX_BYTES = 64 * 1024
_MAX_INT64 = (1 << 63) - 1
_FIELDS = frozenset(
    {
        "contract_version",
        "parse_active_limit",
        "total_nonterminal_limit",
        "finalizer_active_limit",
        "final_http_limit_per_loop",
        "api_process_limit",
        "api_event_loop_limit",
        "processing_window_size",
        "omp_num_threads",
        "mkl_num_threads",
        "openblas_num_threads",
        "pdf_render_processes_requested",
        "hybrid_batch_ratio_requested",
        "pipeline_inference_locks",
        "result_reservation_bytes",
        "max_unacked_result_bytes",
    }
)
# Requested value -> its original startup consumer. Order is the projection
# order; every numeric field with a consumer appears exactly once. The two
# process/loop limits have no consumer: they are validated to one above.
CAPACITY_ENVIRONMENT_FIELDS = (
    ("MINERU_API_MAX_CONCURRENT_REQUESTS", "parse_active_limit"),
    ("MINERU_API_MAX_PENDING_TASKS", "total_nonterminal_limit"),
    ("MINERU_API_FINALIZER_SLOTS", "finalizer_active_limit"),
    ("MINERU_PROCESSING_WINDOW_SIZE", "processing_window_size"),
    ("OMP_NUM_THREADS", "omp_num_threads"),
    ("MKL_NUM_THREADS", "mkl_num_threads"),
    ("OPENBLAS_NUM_THREADS", "openblas_num_threads"),
    ("MINERU_PDF_RENDER_THREADS", "pdf_render_processes_requested"),
    ("MINERU_HYBRID_BATCH_RATIO", "hybrid_batch_ratio_requested"),
    ("MINERU_TASK_PROTOCOL_V2_RESULT_RESERVATION_BYTES", "result_reservation_bytes"),
    ("MINERU_TASK_PROTOCOL_V2_MAX_UNACKED_BYTES", "max_unacked_result_bytes"),
)
CAPACITY_PIPELINE_LOCKS_VARIABLE = "MINERU_ENABLE_PIPELINE_INFERENCE_LOCKS"
CAPACITY_HTTP_OPTION = "--max-concurrency"


@dataclass(frozen=True, slots=True)
class MineruCapacityConfig:
    """Immutable requested limits; changing them requires a new process epoch.

    P includes all accepted nonterminal tasks, including those waiting for result
    capacity. N and F limit physically active parse and finalizer work. H is the
    shared final HTTP limit for the serving loop, not a per-document allowance.
    """

    contract_version: str
    parse_active_limit: int
    total_nonterminal_limit: int
    finalizer_active_limit: int
    final_http_limit_per_loop: int
    api_process_limit: int
    api_event_loop_limit: int
    processing_window_size: int
    omp_num_threads: int
    mkl_num_threads: int
    openblas_num_threads: int
    pdf_render_processes_requested: int
    hybrid_batch_ratio_requested: int
    pipeline_inference_locks: bool
    result_reservation_bytes: int
    max_unacked_result_bytes: int

    def __post_init__(self) -> None:
        if (
            type(self.contract_version) is not str
            or self.contract_version != CAPACITY_CONFIG_CONTRACT
        ):
            raise ValueError("MinerU capacity config contract is unsupported")
        for name in (
            "parse_active_limit",
            "total_nonterminal_limit",
            "finalizer_active_limit",
            "final_http_limit_per_loop",
        ):
            _positive_int(name, getattr(self, name), 128)
        for name in ("api_process_limit", "api_event_loop_limit"):
            _positive_int(name, getattr(self, name), 1)
        _positive_int("processing_window_size", self.processing_window_size, 1024)
        for name in (
            "omp_num_threads",
            "mkl_num_threads",
            "openblas_num_threads",
            "pdf_render_processes_requested",
        ):
            _positive_int(name, getattr(self, name), 256)
        if (
            type(self.hybrid_batch_ratio_requested) is not int
            or self.hybrid_batch_ratio_requested not in (1, 2, 4, 8)
        ):
            raise ValueError("MinerU requested hybrid batch ratio is unsupported")
        if self.pipeline_inference_locks is not True:
            raise ValueError("MinerU capacity config requires original inference locks")
        for name in ("result_reservation_bytes", "max_unacked_result_bytes"):
            _positive_int(name, getattr(self, name), _MAX_INT64)
        if self.parse_active_limit > self.total_nonterminal_limit:
            raise ValueError("MinerU parse limit exceeds total nonterminal limit")
        if self.finalizer_active_limit > self.total_nonterminal_limit:
            raise ValueError("MinerU finalizer limit exceeds total nonterminal limit")
        if self.result_reservation_bytes > self.max_unacked_result_bytes:
            raise ValueError("MinerU single result reservation exceeds result capacity")

    @property
    def exact_bytes(self) -> bytes:
        return encode_mineru_capacity_config(self)

    @property
    def sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.exact_bytes).hexdigest()


def encode_mineru_capacity_config(config: MineruCapacityConfig) -> bytes:
    """Encode the exact validated type without defaults or observed values."""

    if type(config) is not MineruCapacityConfig:
        raise ValueError("MinerU capacity config must use the exact contract type")
    # Revalidate at the byte boundary, including objects reconstructed by callers.
    config.__post_init__()
    return json.dumps(
        asdict(config),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def decode_mineru_capacity_config(payload: bytes) -> MineruCapacityConfig:
    """Accept only closed, canonical UTF-8 JSON bytes within the finite envelope."""

    if type(payload) is not bytes or not payload or len(payload) > _MAX_BYTES:
        raise ValueError("MinerU capacity config bytes are outside the closed envelope")
    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("MinerU capacity config is not strict UTF-8 JSON") from exc
    if type(decoded) is not dict or set(decoded) != _FIELDS:
        raise ValueError("MinerU capacity config fields are not closed")
    config = MineruCapacityConfig(**decoded)
    if config.exact_bytes != payload:
        raise ValueError("MinerU capacity config bytes are not canonical")
    return config


def capacity_environment(config: AnyMineruCapacityConfig) -> dict[str, str]:
    """Project the requested values onto their startup environment consumers.

    The result is a fresh mapping of the numeric projections plus the
    always-enabled pipeline lock flag. The config is revalidated at the byte
    boundary first, so a forged or foreign object never projects. A v2 config
    has no B/L environment consumers: its result storage is read from the
    decoded policy object, never from ambiguous legacy variables.
    """

    if type(config) is MineruCapacityConfigV2:
        encode_mineru_capacity_config_v2(config)
        fields = CAPACITY_ENVIRONMENT_FIELDS_V2
    else:
        encode_mineru_capacity_config(config)  # type: ignore[arg-type]
        fields = CAPACITY_ENVIRONMENT_FIELDS
    values = {
        variable: str(getattr(config, field))
        for variable, field in fields
    }
    values[CAPACITY_PIPELINE_LOCKS_VARIABLE] = "1"
    return values


def capacity_http_arguments(config: AnyMineruCapacityConfig) -> tuple[str, str]:
    """Project the shared serving-loop HTTP limit onto the API command line."""

    if type(config) is MineruCapacityConfigV2:
        encode_mineru_capacity_config_v2(config)
    else:
        encode_mineru_capacity_config(config)  # type: ignore[arg-type]
    return (CAPACITY_HTTP_OPTION, str(config.final_http_limit_per_loop))


# ---------------------------------------------------------------------------
# Result storage policy and capacity config v2.
#
# D is the service's working-space quota on the native output volume and H the
# free space that must stay available to the OS. D is partitioned into the
# source pool P (uploads, growing and sealed parse trees, growth promises), the
# completion escrow C (retained ZIP writing and sealed ZIPs; producers never
# borrow it) and a metadata reserve M. A result may borrow unused P; a source
# never borrows C. Growth is granted per producer before any byte is written.
# The values are deployment choices bound by identity, never measurements.
# ---------------------------------------------------------------------------

RESULT_STORAGE_POLICY_CONTRACT = "mineru.result-storage-policy.v1"
CAPACITY_CONFIG_CONTRACT_V2 = "mineru.capacity-config.v2"
_MAX_IDENTITY_CHARS = 256
_MAX_POLICY_SECONDS = 7 * 24 * 60 * 60
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_ARCHIVE_NAME_BYTES = 4096
_MIN_INVENTORY_BYTES = 64 * 1024
_MAX_INVENTORY_BYTES = 256 * 1024 * 1024
_MAX_DECODE_EXPANSION = 1024
# The Mac work volume's allocation block (APFS). The worker's volume binding
# refuses a volume that allocates in larger units than the work quota assumes.
MAC_ALLOCATION_UNIT_BYTES = 4096
# Files one document owns there besides its members: source snapshot, spool,
# spool part and owner, provider envelope, manifest and staging marker, plus one.
_MAC_DOCUMENT_EXTRA_FILES = 8

# CPython 3.12 ``zipfile`` with force_zip64 on a seekable writer: local header
# 30 + zip64 extra 20, central directory 46 + zip64 extra of at most 28 (sizes
# and offset), each carrying the UTF-8 name once. End records are at most a
# zip64 end record 56 + locator 20 + classic end record 22. No data descriptor
# is written because the header is rewritten in place.
RETAINED_ZIP_MEMBER_OVERHEAD_BYTES = 124
RETAINED_ZIP_END_RECORDS_MAX_BYTES = 98
_STORAGE_INT_FIELDS = (
    "native_volume_total_bytes",
    "native_work_disk_limit_bytes",
    "native_free_floor_bytes",
    "native_source_pool_bytes",
    "native_completion_escrow_bytes",
    "native_metadata_reserve_bytes",
    "native_source_single_limit_bytes",
    "native_growing_producer_limit",
    "native_result_hard_limit_bytes",
    "native_normal_unacked_target_bytes",
    "initial_result_estimate_bytes",
    "native_allocation_unit_bytes",
    "native_file_overhead_bytes",
    "source_pdf_bytes_limit",
    "mac_volume_total_bytes",
    "mac_work_disk_limit_bytes",
    "mac_free_floor_bytes",
    "mac_normal_output_target_bytes",
    "mac_decode_input_limit_bytes",
    "mac_decode_working_set_budget_bytes",
    "mac_decode_expansion_factor",
    "mac_decode_stage_seconds",
    "max_members",
    "max_name_bytes",
    "max_inventory_bytes",
    "transfer_logical_deadline_seconds",
    "progress_window_seconds",
    "minimum_progress_bytes",
)
_STORAGE_FIELDS = frozenset(
    {"contract_version", "native_volume_identity", "mac_volume_identity", *_STORAGE_INT_FIELDS}
)


def raw_deflate_upper_bound(source_bytes: int) -> int:
    """Conservative raw-DEFLATE output bound of the pinned zlib 1.2.11 codec.

    zlib 1.2.11 ``deflateBound`` returns ``s + ((s+7)>>3) + ((s+63)>>6) + 5``
    for arbitrary parameters and a tighter ``s + (s>>12) + (s>>14) + (s>>25) +
    7`` for the default window/memory level used by ``zipfile`` (raw, wrap 0).
    The larger of the two is used, which also covers the empty stream. It plans
    grants only; the writer still enforces the granted extent.
    """

    _nonnegative_int("DEFLATE source bytes", source_bytes)
    conservative = source_bytes + ((source_bytes + 7) >> 3) + ((source_bytes + 63) >> 6) + 5
    tight = source_bytes + (source_bytes >> 12) + (source_bytes >> 14) + (source_bytes >> 25) + 7
    return max(conservative, tight)


def retained_zip_upper_bound(members: tuple[tuple[int, int], ...]) -> int:
    """Bound one retained ZIP from its exact ``(size, utf8_name_bytes)`` members."""

    if type(members) is not tuple:
        raise ValueError("retained ZIP members must be an exact tuple")
    total = RETAINED_ZIP_END_RECORDS_MAX_BYTES
    for member in members:
        if type(member) is not tuple or len(member) != 2:
            raise ValueError("retained ZIP member must be (size, name bytes)")
        size, name_bytes = member
        _nonnegative_int("retained ZIP member size", size)
        _positive_int("retained ZIP member name bytes", name_bytes, _MAX_ARCHIVE_NAME_BYTES)
        total += raw_deflate_upper_bound(size) + RETAINED_ZIP_MEMBER_OVERHEAD_BYTES + 2 * name_bytes
    return total


def retained_zip_envelope_upper_bound(source_bytes: int, member_count: int, name_bytes: int) -> int:
    """Bound any retained ZIP whose members total ``source_bytes``.

    Each member's per-stream rounding adds at most 2 bytes beyond the aggregate
    ceilings and the empty/short-stream constant is at most 7, so the envelope
    is ``S + ceil(S/8) + ceil(S/64) + M*(133 + 2N) + 98`` for at most ``M``
    members with names of at most ``N`` bytes.
    """

    _nonnegative_int("retained ZIP envelope source bytes", source_bytes)
    _positive_int("retained ZIP envelope member count", member_count, _MAX_ARCHIVE_MEMBERS)
    _positive_int("retained ZIP envelope name bytes", name_bytes, _MAX_ARCHIVE_NAME_BYTES)
    return (
        source_bytes + ((source_bytes + 7) >> 3) + ((source_bytes + 63) >> 6)
        + member_count * (9 + RETAINED_ZIP_MEMBER_OVERHEAD_BYTES + 2 * name_bytes)
        + RETAINED_ZIP_END_RECORDS_MAX_BYTES
    )


@dataclass(frozen=True, slots=True)
class MineruResultStoragePolicy:
    """Versioned physical budgets for retained results on both ends.

    Native: D/H on the output volume, the P/C/M partition, one producer's
    growth permit, the single-result hard envelope, the soft unacknowledged
    target and the initial per-task estimate, plus physical allocation
    rounding. Mac: work disk D/H, the soft output target, the decode input and
    working-set budgets and the frozen finite decode stage. Shared: archive
    member/name/inventory bounds and the bounded transfer progress rule.
    """

    contract_version: str
    native_volume_identity: str
    native_volume_total_bytes: int
    native_work_disk_limit_bytes: int
    native_free_floor_bytes: int
    native_source_pool_bytes: int
    native_completion_escrow_bytes: int
    native_metadata_reserve_bytes: int
    native_source_single_limit_bytes: int
    native_growing_producer_limit: int
    native_result_hard_limit_bytes: int
    native_normal_unacked_target_bytes: int
    initial_result_estimate_bytes: int
    native_allocation_unit_bytes: int
    native_file_overhead_bytes: int
    source_pdf_bytes_limit: int
    mac_volume_identity: str
    mac_volume_total_bytes: int
    mac_work_disk_limit_bytes: int
    mac_free_floor_bytes: int
    mac_normal_output_target_bytes: int
    mac_decode_input_limit_bytes: int
    mac_decode_working_set_budget_bytes: int
    mac_decode_expansion_factor: int
    mac_decode_stage_seconds: int
    max_members: int
    max_name_bytes: int
    max_inventory_bytes: int
    transfer_logical_deadline_seconds: int
    progress_window_seconds: int
    minimum_progress_bytes: int

    def __post_init__(self) -> None:
        if (
            type(self.contract_version) is not str
            or self.contract_version != RESULT_STORAGE_POLICY_CONTRACT
        ):
            raise ValueError("MinerU result storage policy contract is unsupported")
        for name in ("native_volume_identity", "mac_volume_identity"):
            _identity_text(name, getattr(self, name))
        for name in _STORAGE_INT_FIELDS:
            if name == "native_file_overhead_bytes":
                _nonnegative_int(name, getattr(self, name))
            else:
                _positive_int(name, getattr(self, name), _MAX_INT64)
        _positive_int("native_growing_producer_limit", self.native_growing_producer_limit, 128)
        _positive_int("mac_decode_expansion_factor", self.mac_decode_expansion_factor, _MAX_DECODE_EXPANSION)
        for name in ("mac_decode_stage_seconds", "transfer_logical_deadline_seconds", "progress_window_seconds"):
            _positive_int(name, getattr(self, name), _MAX_POLICY_SECONDS)
        _positive_int("max_members", self.max_members, _MAX_ARCHIVE_MEMBERS)
        _positive_int("max_name_bytes", self.max_name_bytes, _MAX_ARCHIVE_NAME_BYTES)
        if not _MIN_INVENTORY_BYTES <= self.max_inventory_bytes <= _MAX_INVENTORY_BYTES:
            raise ValueError("MinerU storage inventory bound is outside its supported range")
        unit = self.native_allocation_unit_bytes
        if unit < 512 or unit > 1024 * 1024 or unit & (unit - 1):
            raise ValueError("MinerU native allocation unit must be a power of two in 512..1MiB")
        if self.native_file_overhead_bytes > 1024 * 1024:
            raise ValueError("MinerU native per-file overhead is outside its supported range")
        # Native partition and volume: D hosts P + C + M and leaves H free.
        if (
            _checked_sum(
                self.native_source_pool_bytes,
                self.native_completion_escrow_bytes,
                self.native_metadata_reserve_bytes,
            )
            > self.native_work_disk_limit_bytes
        ):
            raise ValueError("MinerU source pool, completion escrow and metadata exceed the work quota")
        if _checked_sum(self.native_work_disk_limit_bytes, self.native_free_floor_bytes) > self.native_volume_total_bytes:
            raise ValueError("MinerU native work quota plus free floor exceeds the volume")
        if self.native_source_single_limit_bytes > self.native_source_pool_bytes:
            raise ValueError("MinerU single source growth permit exceeds the source pool")
        # Every tree inside the growth permit must be packable inside the hard
        # result envelope, and that envelope must fit the unborrowable escrow.
        # The ledger charges a completion physically (allocation rounding plus
        # per-file overhead) against P + C, and sources may fill P exactly, so
        # the escrow must hold the hard result's physical charge.
        envelope = retained_zip_envelope_upper_bound(
            self.native_source_single_limit_bytes, self.max_members, self.max_name_bytes,
        )
        if envelope > self.native_result_hard_limit_bytes:
            raise ValueError("MinerU hard result limit cannot hold the guaranteed source envelope")
        if self.physical_charge(self.native_result_hard_limit_bytes) > self.native_completion_escrow_bytes:
            raise ValueError("MinerU hard result's physical completion charge exceeds the completion escrow")
        if self.native_normal_unacked_target_bytes > self.native_completion_escrow_bytes:
            raise ValueError("MinerU normal unacked target exceeds the completion escrow")
        if self.initial_result_estimate_bytes > self.native_normal_unacked_target_bytes:
            raise ValueError("MinerU initial result estimate exceeds the normal unacked target")
        # Mac work disk and memory.
        if _checked_sum(self.mac_work_disk_limit_bytes, self.mac_free_floor_bytes) > self.mac_volume_total_bytes:
            raise ValueError("MinerU Mac work quota plus free floor exceeds the volume")
        if self.mac_normal_output_target_bytes > self.mac_work_disk_limit_bytes:
            raise ValueError("MinerU Mac normal output target exceeds the Mac work quota")
        maximal_grant = mac_document_disk_upper_bound(
            self, self.native_result_hard_limit_bytes, self.native_source_single_limit_bytes,
        )
        if maximal_grant > self.mac_work_disk_limit_bytes:
            raise ValueError("MinerU Mac work quota cannot hold one maximal result grant")
        # The worker charges D with every distinct work-volume extent plus its
        # allocation rounding, and a document's source snapshot lives there
        # beside its grant until cleanup: one maximal document must fit whole,
        # or it could never run.
        if _checked_sum(
            maximal_grant, self.source_pdf_bytes_limit, mac_work_file_margin_bytes(self),
        ) > self.mac_work_disk_limit_bytes:
            raise ValueError(
                "MinerU Mac work quota cannot hold one maximal result grant with its source snapshot"
            )
        if _checked_product(self.mac_decode_input_limit_bytes, self.mac_decode_expansion_factor) > (
            self.mac_decode_working_set_budget_bytes
        ):
            raise ValueError("MinerU decode working-set budget cannot hold one maximal decode input")
        # Transfer: the minimum rate must finish the largest result in time.
        windows = -(-self.native_result_hard_limit_bytes // self.minimum_progress_bytes)
        if self.progress_window_seconds > self.transfer_logical_deadline_seconds or (
            _checked_product(windows, self.progress_window_seconds) > self.transfer_logical_deadline_seconds
        ):
            raise ValueError("MinerU transfer minimum progress cannot deliver the hard result in its deadline")

    @property
    def exact_bytes(self) -> bytes:
        return encode_mineru_result_storage_policy(self)

    @property
    def sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.exact_bytes).hexdigest()

    def physical_charge(self, logical_bytes: int) -> int:
        """Allocation-rounded native bytes for one file, including its metadata."""

        _nonnegative_int("physical charge logical bytes", logical_bytes)
        unit = self.native_allocation_unit_bytes
        return -(-logical_bytes // unit) * unit + self.native_file_overhead_bytes


def mac_work_file_margin_bytes(policy: MineruResultStoragePolicy) -> int:
    """Allocation rounding for every file one document may own on the Mac work volume."""

    return _checked_product(policy.max_members + _MAC_DOCUMENT_EXTRA_FILES, MAC_ALLOCATION_UNIT_BYTES)


def mac_document_disk_upper_bound(
    policy: MineruResultStoragePolicy, artifact_bytes: int, uncompressed_bytes: int,
) -> int:
    """Mac scratch for one document: spool Z, unpacked S and serialized outputs.

    Promotion renames the parser subtree, so parser files never exist twice.
    The provider envelope, manifest and staging marker are not bounded by S:
    the envelope adds identities, typed projections and escaped raw fragments.
    They are built as in-memory bytes inside the decode stage before any write,
    so they cannot exceed the decode working-set budget; the materializer
    enforces that sum exactly before writing. This is the per-document grant
    ceiling, not an estimate.
    """

    _nonnegative_int("Mac artifact bytes", artifact_bytes)
    _nonnegative_int("Mac uncompressed bytes", uncompressed_bytes)
    return _checked_sum(
        artifact_bytes, uncompressed_bytes, policy.mac_decode_working_set_budget_bytes,
    )


def encode_mineru_result_storage_policy(policy: MineruResultStoragePolicy) -> bytes:
    if type(policy) is not MineruResultStoragePolicy:
        raise ValueError("MinerU result storage policy must use the exact contract type")
    policy.__post_init__()
    return json.dumps(
        asdict(policy), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def decode_mineru_result_storage_policy(payload: bytes) -> MineruResultStoragePolicy:
    decoded = _strict_object(payload, label="MinerU result storage policy")
    return _storage_policy_from_object(decoded, canonical=payload)


def _storage_policy_from_object(value: object, *, canonical: bytes | None) -> MineruResultStoragePolicy:
    if type(value) is not dict or set(value) != _STORAGE_FIELDS:
        raise ValueError("MinerU result storage policy fields are not closed")
    policy = MineruResultStoragePolicy(**value)
    if canonical is not None and policy.exact_bytes != canonical:
        raise ValueError("MinerU result storage policy bytes are not canonical")
    return policy


_FIELDS_V2 = (_FIELDS - {"result_reservation_bytes", "max_unacked_result_bytes"}) | {"result_storage"}
CAPACITY_ENVIRONMENT_FIELDS_V2 = tuple(
    (variable, field) for variable, field in CAPACITY_ENVIRONMENT_FIELDS
    if field not in {"result_reservation_bytes", "max_unacked_result_bytes"}
)


@dataclass(frozen=True, slots=True)
class MineruCapacityConfigV2:
    """v1 compute limits plus one explicit, nested result storage policy.

    The v1 ``result_reservation_bytes``/``max_unacked_result_bytes`` do not
    exist here: their soft roles are the policy's explicit initial estimate and
    normal unacked target, and the hard budgets are the policy's own fields.
    """

    contract_version: str
    parse_active_limit: int
    total_nonterminal_limit: int
    finalizer_active_limit: int
    final_http_limit_per_loop: int
    api_process_limit: int
    api_event_loop_limit: int
    processing_window_size: int
    omp_num_threads: int
    mkl_num_threads: int
    openblas_num_threads: int
    pdf_render_processes_requested: int
    hybrid_batch_ratio_requested: int
    pipeline_inference_locks: bool
    result_storage: MineruResultStoragePolicy

    def __post_init__(self) -> None:
        if (
            type(self.contract_version) is not str
            or self.contract_version != CAPACITY_CONFIG_CONTRACT_V2
        ):
            raise ValueError("MinerU capacity config contract is unsupported")
        for name in (
            "parse_active_limit",
            "total_nonterminal_limit",
            "finalizer_active_limit",
            "final_http_limit_per_loop",
        ):
            _positive_int(name, getattr(self, name), 128)
        for name in ("api_process_limit", "api_event_loop_limit"):
            _positive_int(name, getattr(self, name), 1)
        _positive_int("processing_window_size", self.processing_window_size, 1024)
        for name in (
            "omp_num_threads",
            "mkl_num_threads",
            "openblas_num_threads",
            "pdf_render_processes_requested",
        ):
            _positive_int(name, getattr(self, name), 256)
        if (
            type(self.hybrid_batch_ratio_requested) is not int
            or self.hybrid_batch_ratio_requested not in (1, 2, 4, 8)
        ):
            raise ValueError("MinerU requested hybrid batch ratio is unsupported")
        if self.pipeline_inference_locks is not True:
            raise ValueError("MinerU capacity config requires original inference locks")
        if self.parse_active_limit > self.total_nonterminal_limit:
            raise ValueError("MinerU parse limit exceeds total nonterminal limit")
        if self.finalizer_active_limit > self.total_nonterminal_limit:
            raise ValueError("MinerU finalizer limit exceeds total nonterminal limit")
        if type(self.result_storage) is not MineruResultStoragePolicy:
            raise ValueError("MinerU capacity config v2 requires an exact result storage policy")
        policy = self.result_storage
        policy.__post_init__()
        if policy.native_growing_producer_limit > self.parse_active_limit:
            raise ValueError("MinerU growing producer limit exceeds the parse slots")
        # Uploads of every admitted task and every concurrent growth permit fit
        # the source pool, so an admitted upload never races a producer.
        uploads = _checked_product(
            self.total_nonterminal_limit, policy.physical_charge(policy.source_pdf_bytes_limit),
        )
        permits = _checked_product(policy.native_growing_producer_limit, policy.native_source_single_limit_bytes)
        if _checked_sum(uploads, permits) > policy.native_source_pool_bytes:
            raise ValueError("MinerU source pool cannot hold admitted uploads plus concurrent growth permits")

    @property
    def exact_bytes(self) -> bytes:
        return encode_mineru_capacity_config_v2(self)

    @property
    def sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.exact_bytes).hexdigest()


AnyMineruCapacityConfig = MineruCapacityConfig | MineruCapacityConfigV2


def encode_mineru_capacity_config_v2(config: MineruCapacityConfigV2) -> bytes:
    if type(config) is not MineruCapacityConfigV2:
        raise ValueError("MinerU capacity config v2 must use the exact contract type")
    config.__post_init__()
    return json.dumps(
        asdict(config), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def decode_mineru_capacity_config_v2(payload: bytes) -> MineruCapacityConfigV2:
    decoded = _strict_object(payload, label="MinerU capacity config")
    if set(decoded) != _FIELDS_V2:
        raise ValueError("MinerU capacity config fields are not closed")
    storage = _storage_policy_from_object(decoded["result_storage"], canonical=None)
    config = MineruCapacityConfigV2(**{**decoded, "result_storage": storage})
    if config.exact_bytes != payload:
        raise ValueError("MinerU capacity config bytes are not canonical")
    return config


def encode_any_mineru_capacity_config(config: AnyMineruCapacityConfig) -> bytes:
    """Canonical bytes of either contract version, revalidated at the boundary."""

    if type(config) is MineruCapacityConfigV2:
        return encode_mineru_capacity_config_v2(config)
    return encode_mineru_capacity_config(config)  # type: ignore[arg-type]


def decode_any_mineru_capacity_config(payload: bytes) -> AnyMineruCapacityConfig:
    """Select the decoder by the closed contract version; v1 stays unchanged."""

    decoded = _strict_object(payload, label="MinerU capacity config")
    if decoded.get("contract_version") == CAPACITY_CONFIG_CONTRACT_V2:
        return decode_mineru_capacity_config_v2(payload)
    return decode_mineru_capacity_config(payload)


def _strict_object(payload: bytes, *, label: str) -> dict[str, Any]:
    if type(payload) is not bytes or not payload or len(payload) > _MAX_BYTES:
        raise ValueError(f"{label} bytes are outside the closed envelope")
    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{label} is not strict UTF-8 JSON") from exc
    if type(decoded) is not dict:
        raise ValueError(f"{label} fields are not closed")
    return decoded


def _identity_text(name: str, value: object) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_IDENTITY_CHARS
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"MinerU storage {name} must be printable non-empty text")


def _nonnegative_int(name: str, value: object) -> None:
    if type(value) is not int or not 0 <= value <= _MAX_INT64:
        raise ValueError(f"{name} must be a non-negative bounded integer")


def _checked_sum(*values: int) -> int:
    total = 0
    for value in values:
        total += value
        if total > _MAX_INT64:
            raise ValueError("MinerU storage policy arithmetic overflowed")
    return total


def _checked_product(left: int, right: int) -> int:
    value = left * right
    if value > _MAX_INT64:
        raise ValueError("MinerU storage policy arithmetic overflowed")
    return value


def _positive_int(name: str, value: object, maximum: int) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"MinerU capacity config {name} must be within 1..{maximum}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("MinerU capacity config has a duplicate field")
        value[name] = item
    return value


def _reject_constant(value: str) -> Any:
    raise ValueError(f"MinerU capacity config has a non-finite value: {value}")


__all__ = [
    "AnyMineruCapacityConfig",
    "CAPACITY_CONFIG_CONTRACT",
    "CAPACITY_CONFIG_CONTRACT_V2",
    "CAPACITY_ENVIRONMENT_FIELDS",
    "CAPACITY_ENVIRONMENT_FIELDS_V2",
    "CAPACITY_HTTP_OPTION",
    "CAPACITY_PIPELINE_LOCKS_VARIABLE",
    "MineruCapacityConfig",
    "MineruCapacityConfigV2",
    "MineruResultStoragePolicy",
    "RESULT_STORAGE_POLICY_CONTRACT",
    "RETAINED_ZIP_END_RECORDS_MAX_BYTES",
    "RETAINED_ZIP_MEMBER_OVERHEAD_BYTES",
    "capacity_environment",
    "capacity_http_arguments",
    "decode_any_mineru_capacity_config",
    "encode_any_mineru_capacity_config",
    "decode_mineru_capacity_config_v2",
    "decode_mineru_result_storage_policy",
    "encode_mineru_capacity_config_v2",
    "encode_mineru_result_storage_policy",
    "MAC_ALLOCATION_UNIT_BYTES",
    "mac_document_disk_upper_bound",
    "mac_work_file_margin_bytes",
    "raw_deflate_upper_bound",
    "retained_zip_envelope_upper_bound",
    "retained_zip_upper_bound",
    "decode_mineru_capacity_config",
    "encode_mineru_capacity_config",
]
