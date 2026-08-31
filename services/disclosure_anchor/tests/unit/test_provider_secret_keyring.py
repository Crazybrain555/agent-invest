from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from disclosure_anchor.adapters.security.provider_secret_keyring import (
    PROVIDER_SECRET_KEYRING_FORMAT,
    StaticProviderSecretKeyring,
    load_provider_secret_keyring_file,
    load_provider_secret_keyring_from_settings,
)
from disclosure_anchor.application.ports.provider_secret_cipher_v4 import (
    ProviderSecretKekUnknown,
    ProviderSecretKeyringInvalid,
    ProviderSecretKeyringUnavailable,
)
from disclosure_anchor.settings import Settings

_PRIMARY_HEX = "0f" * 32
_LEGACY_HEX = "a1" * 32


def _payload() -> dict[str, object]:
    return {
        "format": PROVIDER_SECRET_KEYRING_FORMAT,
        "primary_kek_id": "kek-2026-08",
        "keks": {"kek-2026-08": _PRIMARY_HEX, "kek-2026-01": _LEGACY_HEX},
    }


def _settings(root: Path, **overrides: object) -> Settings:
    data_root = root / "services" / "disclosure_anchor"
    shared_root = root / "shared"
    return Settings(
        disclosure_data_root=data_root,
        disclosure_shared_root=shared_root,
        disclosure_runtime_root=data_root / "runtime",
        mineru_model_cache=shared_root / "model_cache" / "mineru",
        hf_home=shared_root / "model_cache" / "huggingface",
        modelscope_cache=shared_root / "model_cache" / "modelscope",
        **overrides,  # type: ignore[arg-type]
    )


class StaticKeyringTests(unittest.TestCase):
    def test_lookup_and_unknown_kek(self) -> None:
        keyring = StaticProviderSecretKeyring(
            primary_kek_id="kek-a",
            keks={"kek-a": b"\x01" * 32, "kek-b": b"\x02" * 32},
        )
        self.assertEqual(keyring.primary_kek_id(), "kek-a")
        self.assertEqual(keyring.kek_bytes("kek-b"), b"\x02" * 32)
        with self.assertRaises(ProviderSecretKekUnknown):
            keyring.kek_bytes("kek-c")

    def test_invalid_construction(self) -> None:
        rejected = (
            {"primary_kek_id": "kek-a", "keks": {}},
            {"primary_kek_id": "kek-a", "keks": {"KEK-A": b"\x01" * 32}},
            {"primary_kek_id": "kek-a", "keks": {"kek-a": b"\x01" * 31}},
            {"primary_kek_id": "kek-a", "keks": {"kek-a": bytearray(32)}},
            {"primary_kek_id": "kek-a", "keks": {"kek-a": "0f" * 32}},
            {"primary_kek_id": "kek-x", "keks": {"kek-a": b"\x01" * 32}},
        )
        for kwargs in rejected:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ProviderSecretKeyringInvalid):
                    StaticProviderSecretKeyring(**kwargs)  # type: ignore[arg-type]


class KeyringFileLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write(
        self,
        *,
        name: str = "keyring.json",
        text: str | None = None,
        raw: bytes | None = None,
        mode: int = 0o600,
    ) -> Path:
        path = self.root / name
        if raw is not None:
            path.write_bytes(raw)
        else:
            path.write_text(
                json.dumps(_payload()) if text is None else text,
                encoding="utf-8",
            )
        os.chmod(path, mode)
        return path

    def test_load_valid_file(self) -> None:
        keyring = load_provider_secret_keyring_file(self._write())
        self.assertEqual(keyring.primary_kek_id(), "kek-2026-08")
        self.assertEqual(
            keyring.kek_bytes("kek-2026-08"), bytes.fromhex(_PRIMARY_HEX)
        )
        self.assertEqual(
            keyring.kek_bytes("kek-2026-01"), bytes.fromhex(_LEGACY_HEX)
        )

    def test_relative_path_rejected(self) -> None:
        with self.assertRaises(ProviderSecretKeyringInvalid):
            load_provider_secret_keyring_file(Path("keyring.json"))

    def test_close_failure_does_not_replace_valid_result(self) -> None:
        path = self._write()
        real_close = os.close

        def close_then_fail(fd: int) -> None:
            real_close(fd)
            raise InterruptedError("simulated close interruption")

        with mock.patch.object(os, "close", side_effect=close_then_fail):
            keyring = load_provider_secret_keyring_file(path)
        self.assertEqual(keyring.primary_kek_id(), "kek-2026-08")

    def test_read_only_owner_mode_is_accepted(self) -> None:
        path = self._write(mode=0o400)
        self.assertEqual(
            load_provider_secret_keyring_file(path).primary_kek_id(),
            "kek-2026-08",
        )

    def test_broad_permissions_rejected(self) -> None:
        for mode in (0o644, 0o640, 0o604, 0o700, 0o660):
            with self.subTest(mode=oct(mode)):
                path = self._write(name=f"ring-{mode:o}.json", mode=mode)
                with self.assertRaises(ProviderSecretKeyringInvalid):
                    load_provider_secret_keyring_file(path)

    def test_symlink_rejected(self) -> None:
        target = self._write()
        link = self.root / "link.json"
        os.symlink(target, link)
        with self.assertRaises(ProviderSecretKeyringInvalid) as caught:
            load_provider_secret_keyring_file(link)
        self.assertNotIn(str(self.root), str(caught.exception))

    def test_fifo_rejected(self) -> None:
        path = self.root / "ring.fifo"
        os.mkfifo(path, 0o600)
        with self.assertRaises(ProviderSecretKeyringInvalid):
            load_provider_secret_keyring_file(path)

    def test_duplicate_json_keys_rejected(self) -> None:
        duplicate_kek = (
            '{"format":"%s","primary_kek_id":"kek-a",'
            '"keks":{"kek-a":"%s","kek-a":"%s"}}'
            % (PROVIDER_SECRET_KEYRING_FORMAT, _PRIMARY_HEX, _LEGACY_HEX)
        )
        duplicate_top = (
            '{"format":"%s","primary_kek_id":"kek-a",'
            '"keks":{"kek-a":"%s"},"keks":{"kek-a":"%s"}}'
            % (PROVIDER_SECRET_KEYRING_FORMAT, _PRIMARY_HEX, _LEGACY_HEX)
        )
        for label, text in (("kek", duplicate_kek), ("top", duplicate_top)):
            with self.subTest(duplicate=label):
                path = self._write(name=f"dup-{label}.json", text=text)
                with self.assertRaises(ProviderSecretKeyringInvalid):
                    load_provider_secret_keyring_file(path)

    def test_oversize_rejected(self) -> None:
        path = self._write(raw=b"x" * (64 * 1024 + 1))
        with self.assertRaises(ProviderSecretKeyringInvalid):
            load_provider_secret_keyring_file(path)

    def test_bad_hex_rejected(self) -> None:
        bad_values = ("0F" * 32, "0f" * 31, "0f" * 33, "zz" * 32, 123, None)
        for bad in bad_values:
            with self.subTest(bad=bad):
                payload = _payload()
                payload["keks"] = {"kek-2026-08": bad}
                path = self._write(text=json.dumps(payload))
                with self.assertRaises(ProviderSecretKeyringInvalid):
                    load_provider_secret_keyring_file(path)

    def test_closed_structure_rejected(self) -> None:
        wrong_format = _payload()
        wrong_format["format"] = "disclosure-v4-secret-keyring.v2"
        extra_field = {**_payload(), "comment": "nope"}
        missing_field = {
            "format": PROVIDER_SECRET_KEYRING_FORMAT,
            "keks": {"kek-a": _PRIMARY_HEX},
        }
        primary_absent = {
            "format": PROVIDER_SECRET_KEYRING_FORMAT,
            "primary_kek_id": "kek-x",
            "keks": {"kek-a": _PRIMARY_HEX},
        }
        primary_not_string = {
            "format": PROVIDER_SECRET_KEYRING_FORMAT,
            "primary_kek_id": 7,
            "keks": {"kek-a": _PRIMARY_HEX},
        }
        empty_keks = {
            "format": PROVIDER_SECRET_KEYRING_FORMAT,
            "primary_kek_id": "kek-a",
            "keks": {},
        }
        keks_not_object = {
            "format": PROVIDER_SECRET_KEYRING_FORMAT,
            "primary_kek_id": "kek-a",
            "keks": [_PRIMARY_HEX],
        }
        bad_kek_id = {
            "format": PROVIDER_SECRET_KEYRING_FORMAT,
            "primary_kek_id": "KEK-A",
            "keks": {"KEK-A": _PRIMARY_HEX},
        }
        cases = (
            ("wrong_format", json.dumps(wrong_format)),
            ("extra_field", json.dumps(extra_field)),
            ("missing_field", json.dumps(missing_field)),
            ("primary_absent", json.dumps(primary_absent)),
            ("primary_not_string", json.dumps(primary_not_string)),
            ("empty_keks", json.dumps(empty_keks)),
            ("keks_not_object", json.dumps(keks_not_object)),
            ("bad_kek_id", json.dumps(bad_kek_id)),
            ("top_level_array", "[]"),
            ("not_json", "{nope"),
            ("not_utf8", None),
        )
        for label, text in cases:
            with self.subTest(case=label):
                if text is None:
                    path = self._write(name=f"bad-{label}.json", raw=b"\xff\xfe{}")
                else:
                    path = self._write(name=f"bad-{label}.json", text=text)
                with self.assertRaises(ProviderSecretKeyringInvalid):
                    load_provider_secret_keyring_file(path)

    def test_missing_file_error_excludes_path(self) -> None:
        missing = self.root / "absent.json"
        with self.assertRaises(ProviderSecretKeyringInvalid) as caught:
            load_provider_secret_keyring_file(missing)
        message = str(caught.exception)
        self.assertNotIn(str(missing), message)
        self.assertNotIn(self._tmp.name, message)
        self.assertIsNone(caught.exception.__cause__)

    def test_error_messages_exclude_key_material(self) -> None:
        path = self._write(mode=0o644)
        with self.assertRaises(ProviderSecretKeyringInvalid) as caught:
            load_provider_secret_keyring_file(path)
        message = str(caught.exception)
        self.assertNotIn(_PRIMARY_HEX, message)
        self.assertNotIn("kek-2026-08", message)
        self.assertNotIn(str(path), message)


class KeyringSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_setting_defaults_to_none(self) -> None:
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("DISCLOSURE_V4_SECRET_KEYRING_FILE", None)
            settings = _settings(self.root)
        self.assertIsNone(settings.disclosure_v4_secret_keyring_file)

    def test_setting_reads_environment_alias(self) -> None:
        keyring_path = self.root / "keyring.json"
        with mock.patch.dict(
            os.environ,
            {"DISCLOSURE_V4_SECRET_KEYRING_FILE": str(keyring_path)},
        ):
            settings = _settings(self.root)
        self.assertEqual(
            settings.disclosure_v4_secret_keyring_file, keyring_path
        )

    def test_composition_fails_closed_without_path(self) -> None:
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("DISCLOSURE_V4_SECRET_KEYRING_FILE", None)
            settings = _settings(self.root)
        with self.assertRaises(ProviderSecretKeyringUnavailable):
            load_provider_secret_keyring_from_settings(settings)

    def test_composition_loads_configured_file(self) -> None:
        keyring_path = self.root / "keyring.json"
        keyring_path.write_text(json.dumps(_payload()), encoding="utf-8")
        os.chmod(keyring_path, 0o600)
        settings = _settings(
            self.root, disclosure_v4_secret_keyring_file=keyring_path
        )
        keyring = load_provider_secret_keyring_from_settings(settings)
        self.assertEqual(keyring.primary_kek_id(), "kek-2026-08")


if __name__ == "__main__":
    unittest.main()
