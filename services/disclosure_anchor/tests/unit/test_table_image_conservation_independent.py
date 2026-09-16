"""Independent source/token conservation regressions for the demonstrated lost crop."""
from __future__ import annotations

import ast
import base64
from copy import deepcopy
import hashlib
from pathlib import Path
import re
from types import SimpleNamespace
import unittest

from scripts.windows.mineru_heap_trim_compat.patch_mineru_344 import patch_source


SOURCE = 'mineru_vl_utils/post_process/__init__.py'
CROP = b'\xff\xd8independent-crop-evidence\xff\xd9'
URI = 'data:image/jpeg;base64,' + base64.b64encode(CROP).decode('ascii')
SHA = 'sha256:' + hashlib.sha256(CROP).hexdigest()
IMG = '<img src="' + URI + '"/>'


class _Block(dict):
    def __getattr__(self, key):
        return self[key]

    def __setattr__(self, key, value):
        self[key] = value


def patched_functions():
    source = (Path(__file__).parents[1] / 'fixtures/mineru_344_preimages' / SOURCE).read_text()
    patched = patch_source(SOURCE, source)
    compile(patched, SOURCE, 'exec')
    tree = ast.parse(patched)
    body = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in ('table_image_conservation', 'simple_process')]
    if {node.name for node in body} != {'table_image_conservation', 'simple_process'}:
        raise AssertionError('both exact patched production functions must be exercised')
    namespace = {'base64': base64, 'hashlib': hashlib, 're': re, 'ContentBlock': _Block,
                 'logger': SimpleNamespace(warning=lambda *args: None),
                 'TABLE_IMAGE_TOKEN_MAP_KEY': '_table_image_token_map',
                 'convert_otsl_to_html': lambda content: content,
                 'replace_table_formula_delimiters': lambda content, **kwargs: content}
    exec(compile(ast.Module(body=body, type_ignores=[]), SOURCE, 'exec'), namespace)
    return source, patched, namespace


class TableImageConservationIndependentTests(unittest.TestCase):
    def test_matched_tokens_preserve_bytes_and_empty_map_adds_nothing(self):
        source, patched, ns = patched_functions()
        self.assertEqual(patch_source(SOURCE, patched), patched, 'patch must be idempotent')
        self.assertNotEqual(source, patched)
        for original in ('<td>[A1]</td>', '<td>[  A1\t]</td>'):
            mapping = {'[A1]': URI}
            before = deepcopy(mapping)
            self.assertEqual(ns['table_image_conservation'](original, '<td>'+IMG+'</td>', mapping), [])
            self.assertEqual(mapping, before)
        self.assertEqual(ns['table_image_conservation']('text', 'text', None), [])

    def test_missing_duplicate_and_uri_collision_keep_each_original_token(self):
        _, _, ns = patched_functions()
        check = ns['table_image_conservation']
        cases = (
            ('<td>[6×46]</td>', '<td>[6×46]</td>', {'[6X46]': URI}, {'[6X46]': ('missing', 0)}),
            ('[Al]', '[Al]', {'[A1]': URI}, {'[A1]': ('missing', 0)}),
            ('[A1] [A1]', IMG+' '+IMG, {'[A1]': URI}, {'[A1]': ('duplicate', 2)}),
            ('[A1]', IMG, {'[A1]': URI, '[B1]': URI}, {'[B1]': ('missing', 0)}),
            ('[A1] [A1]', IMG+' '+IMG, {'[A1]': URI, '[B1]': URI},
             {'[A1]': ('duplicate', 2), '[B1]': ('missing', 0)}),
            ('', '', {'[A1]': URI}, {'[A1]': ('missing', 0)}),
        )
        for original, replaced, mapping, expected in cases:
            with self.subTest(original=original, mapping=tuple(mapping)):
                issues = check(original, replaced, mapping)
                self.assertEqual({v['token']: (v['kind'], v['actual']) for v in issues}, expected)
                for issue in issues:
                    self.assertEqual(issue['expected'], 1)
                    self.assertEqual(issue['image_data_uri'], URI)
                    self.assertEqual(issue['image_sha256'], SHA)
                    self.assertEqual(issue['image_byte_count'], len(CROP))

    def test_broken_restoration_is_visible_without_rewriting_output(self):
        _, _, ns = patched_functions()
        for output in ('[A1]', '', IMG+IMG):
            with self.subTest(output=output):
                issues = ns['table_image_conservation']('[A1]', output, {'[A1]': URI})
                self.assertEqual(len(issues), 1)
                self.assertEqual((issues[0]['kind'], issues[0]['actual']), ('unrestored', 1))
                self.assertEqual(issues[0]['image_data_uri'], URI)

    def test_simple_process_checks_empty_table_and_keeps_matched_content_unchanged(self):
        _, _, ns = patched_functions()
        # Replacement itself is an unchanged dependency, not the test target.
        # Its exact replacement result is supplied so this checks the integration seam.
        ns['replace_table_image_tokens'] = lambda content, _mapping: content.replace('[A1]', IMG)
        for content in ('[A1]', '', None):
            with self.subTest(content=content):
                block = _Block(type='table', content=content, _table_image_token_map={'[A1]': URI})
                out = ns['simple_process']([block])
                self.assertIs(out[0], block)
                if content:
                    self.assertEqual(block.content, IMG)
                    self.assertNotIn('table_image_unmatched', block)
                else:
                    self.assertEqual(block.content, content)
                    self.assertEqual(block['table_image_unmatched'][0]['kind'], 'missing')
                    self.assertEqual(block['table_image_unmatched'][0]['image_data_uri'], URI)


if __name__ == '__main__':
    unittest.main()
