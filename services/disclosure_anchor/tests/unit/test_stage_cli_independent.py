"""Real CLI and files, commissioning IO replaced; no database."""
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock
from disclosure_anchor.cli import staged_commission as cli
from tests.unit.test_stage_observation_independent import guard


class CommissionObservationCliTests(unittest.TestCase):
    def test_default_cli_has_no_observation_and_keeps_result_exit_semantics(self):
        for result, code in (('PASS',0),('NOT_PASS',1)):
            with self.subTest(result=result), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp).resolve()
                with mock.patch.object(cli,'load_settings',return_value=SimpleNamespace(disclosure_runtime_root=root)), \
                     mock.patch.object(cli,'run_commissioning',return_value={'result':result}) as run, \
                     contextlib.redirect_stdout(io.StringIO()):
                    actual=cli.main(['--document-id','doc-test','--max-seconds','1',
                                     '--receipt-out',str(root/'receipt.json')])
                self.assertEqual(actual,code)
                self.assertIsNone(run.call_args.kwargs['hooks'])
                self.assertEqual(json.loads((root/'receipt.json').read_text()),{'result':result})
                self.assertFalse((root/'stage-events.jsonl').exists())

    def test_enabled_cli_closes_real_tail_even_when_commission_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            def run(*args,**kwargs):
                hooks=kwargs['hooks']
                guard(hooks.stage_observer).note('units_built',units=3)
                hooks.publication_committed(False)
                raise RuntimeError('original commission error')
            with mock.patch.object(cli,'load_settings',return_value=SimpleNamespace(disclosure_runtime_root=root)), \
                 mock.patch.object(cli,'run_commissioning',side_effect=run), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError,'original commission error'):
                    cli.main(['--document-id','doc-test','--max-seconds','1','--receipt-out',str(root/'receipt.json'),
                              '--observation-out',str(root/'observe')])
            rows=[json.loads(line) for line in (root/'observe/stage-events.jsonl').read_text().splitlines()]
            self.assertEqual([r['kind'] for r in rows],['units_built','observation_closed'])
            summary=json.loads((root/'observe/observation-summary.json').read_text())
            self.assertEqual(summary['join_timeout'],0)
            signal=json.loads((root/'observe/progress.jsonl').read_text())
            self.assertEqual(signal['kind'],'prune_signal')
            self.assertFalse(signal['replaced'])

    def test_existing_or_outside_observation_directory_rejected_before_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            existing=root/'existing'
            existing.mkdir()
            for path in (existing,root.parent/'outside-r17'):
                with self.subTest(path=path), \
                     mock.patch.object(cli,'load_settings',return_value=SimpleNamespace(disclosure_runtime_root=root)), \
                     mock.patch.object(cli,'run_commissioning') as run, contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as exc:
                        cli.main(['--document-id','doc-test','--max-seconds','1','--receipt-out',str(root/'receipt.json'),
                                  '--observation-out',str(path)])
                    self.assertEqual(exc.exception.code,2)
                    run.assert_not_called()

    def test_progress_close_failure_still_closes_observer_and_restores_signals(self):
        import os
        import signal
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            captured={}
            original_close=cli.ProgressRecorder.close
            def broken_close(recorder):
                captured['progress']=recorder
                # Real recorder flush fails, not a mocked main/observer lifecycle.
                real_fsync=os.fsync
                def fail_progress(fd):
                    if fd==recorder._fd:
                        raise OSError('progress fsync failed')
                    return real_fsync(fd)
                with mock.patch('disclosure_anchor.adapters.runtime.stage_observation.os.fsync',side_effect=fail_progress):
                    return original_close(recorder)
            def run(*args,**kwargs):
                captured['observer']=kwargs['hooks'].stage_observer
                guard(captured['observer']).note('units_built',units=1)
                return {'result':'PASS'}
            original_handlers={s:signal.getsignal(s) for s in (signal.SIGINT,signal.SIGTERM)}
            try:
                with mock.patch.object(cli,'load_settings',return_value=SimpleNamespace(disclosure_runtime_root=root)), \
                     mock.patch.object(cli,'run_commissioning',side_effect=run), \
                     mock.patch.object(cli.ProgressRecorder,'close',broken_close), contextlib.redirect_stdout(io.StringIO()):
                    try:
                        cli.main(['--document-id','doc-test','--max-seconds','1','--receipt-out',str(root/'receipt.json'),
                                  '--observation-out',str(root/'observe')])
                    except OSError:
                        pass  # Failure may remain visible; independent cleanup must still happen.
                self.assertFalse(captured['observer']._thread.is_alive(), 'progress failure abandoned real observer thread')
                self.assertEqual({s:signal.getsignal(s) for s in original_handlers},original_handlers)
            finally:
                observer=captured.get('observer')
                if observer is not None and not observer._closed:
                    observer.close()
                progress=captured.get('progress')
                if progress is not None:
                    try:
                        os.close(progress._fd)
                    except OSError:
                        pass
                for sig,handler in original_handlers.items():
                    signal.signal(sig,handler)
