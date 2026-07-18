"""Remote VLM backend probe in the parser identity contract."""

from __future__ import annotations

import unittest
from unittest import mock

from disclosure_anchor.adapters.parsers.mineru.parser import MinerUDocumentParser
from disclosure_anchor.domain.errors import ParserVersionProbeError


class BackendProbeTests(unittest.TestCase):
    def test_identity_probes_remote_backend_when_configured(self) -> None:
        process = mock.Mock()
        process.version.return_value = "2.9.9"
        parser = MinerUDocumentParser(
            process=process, server_url="http://gpu:30000"
        )

        identity = parser.identity()

        self.assertEqual(identity.version, "2.9.9")
        process.probe_server.assert_called_once_with("http://gpu:30000")

    def test_unreachable_backend_fails_before_any_document_dequeues(
        self,
    ) -> None:
        # The worker's pre-dequeue identity probe must surface a dead GPU
        # server as an infrastructure failure instead of letting a batch
        # burn one parse retry per document.
        process = mock.Mock()
        process.version.return_value = "2.9.9"
        process.probe_server.side_effect = ParserVersionProbeError(
            "MinerU backend server unreachable: http://gpu:30000"
        )
        parser = MinerUDocumentParser(
            process=process, server_url="http://gpu:30000"
        )

        with self.assertRaises(ParserVersionProbeError):
            parser.identity()

    def test_identity_skips_probe_without_server_url(self) -> None:
        process = mock.Mock()
        process.version.return_value = "2.9.9"
        parser = MinerUDocumentParser(process=process)

        parser.identity()

        process.probe_server.assert_not_called()


if __name__ == "__main__":
    unittest.main()
