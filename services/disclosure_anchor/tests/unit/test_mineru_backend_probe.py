"""Remote VLM backend readiness checks."""

from __future__ import annotations

import unittest
from unittest import mock

from disclosure_anchor.adapters.parsers.mineru import mineru_process
from disclosure_anchor.adapters.parsers.mineru.mineru_process import MinerUProcess
from disclosure_anchor.adapters.parsers.mineru.parser import MinerUDocumentParser
from disclosure_anchor.domain.errors import ParserVersionProbeError


class BackendProbeTests(unittest.TestCase):
    def test_identity_is_stable_without_remote_probe(self) -> None:
        process = mock.Mock()
        process.version.return_value = "2.9.9"
        parser = MinerUDocumentParser(
            process=process, server_url="http://gpu:30000"
        )

        first = parser.identity()
        second = parser.identity()

        self.assertEqual(first, second)
        self.assertEqual(first.version, "2.9.9")
        process.version.assert_called_once_with()
        process.probe_server.assert_not_called()

    def test_readiness_probes_remote_and_surfaces_failure(self) -> None:
        process = mock.Mock()
        process.version.return_value = "2.9.9"
        parser = MinerUDocumentParser(
            process=process, server_url="http://gpu:30000"
        )

        parser.readiness()

        process.probe_server.assert_called_once_with("http://gpu:30000")
        process.probe_server.side_effect = ParserVersionProbeError(
            "MinerU backend server unreachable: http://gpu:30000"
        )
        with self.assertRaises(ParserVersionProbeError):
            parser.readiness()

    def test_readiness_skips_probe_without_server_url(self) -> None:
        process = mock.Mock()
        process.version.return_value = "2.9.9"
        parser = MinerUDocumentParser(process=process)

        parser.readiness()

        process.probe_server.assert_not_called()


class ProbeSuccessCacheTests(unittest.TestCase):
    """Repeated readiness checks cache success briefly, never failure."""

    def setUp(self) -> None:
        mineru_process._PROBE_SUCCESS_AT.clear()
        self.process = MinerUProcess(executable="mineru")

    def tearDown(self) -> None:
        mineru_process._PROBE_SUCCESS_AT.clear()

    def test_probe_success_is_cached_within_ttl(self) -> None:
        opener = mock.Mock()
        opener.open.return_value.__enter__ = mock.Mock(return_value=None)
        opener.open.return_value.__exit__ = mock.Mock(return_value=False)
        clock = iter([100.0, 100.5, 130.0, 100.0 + 61.0, 200.0])
        with (
            mock.patch.object(
                mineru_process.urllib.request,
                "build_opener",
                return_value=opener,
            ),
            mock.patch.object(
                mineru_process.time, "monotonic", side_effect=clock
            ),
        ):
            self.process.probe_server("http://gpu:30000")
            self.process.probe_server("http://gpu:30000")
            self.process.probe_server("http://gpu:30000")
        self.assertEqual(opener.open.call_count, 1)

    def test_probe_failure_is_never_cached(self) -> None:
        opener = mock.Mock()
        opener.open.side_effect = OSError("connection refused")
        with (
            mock.patch.object(
                mineru_process.urllib.request,
                "build_opener",
                return_value=opener,
            ),
            mock.patch.object(
                mineru_process.time, "monotonic", return_value=100.0
            ),
        ):
            for _ in range(2):
                with self.assertRaises(ParserVersionProbeError):
                    self.process.probe_server("http://gpu:30000")
        self.assertEqual(opener.open.call_count, 2)


if __name__ == "__main__":
    unittest.main()
