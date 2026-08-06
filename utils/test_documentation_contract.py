#!/usr/bin/env python3
"""Keep documented local commands and assets aligned with the repository."""
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import tartarus as ll


ROOT = Path(__file__).resolve().parents[1]


class DocumentationContractTest(unittest.TestCase):
    def test_readme_references_current_local_assets(self):
        readme = (ROOT / 'README.md').read_text(encoding='utf-8')
        for path in (
            'utils/tartarus.py', 'utils/tartarus_web.py',
            'utils/e2e_backend_test.py', 'web/', 'data/word_lists/',
        ):
            self.assertIn(path, readme)
        self.assertNotIn('web/index.css', readme)
        self.assertNotIn('--no-audio', readme)
        self.assertNotIn('100% coverage', readme)
        self.assertNotIn('Chart.js', (ROOT / 'web' / 'index.html').read_text(encoding='utf-8'))

    def test_cli_and_make_help_match_documented_surface(self):
        cli_help = subprocess.run(
            [sys.executable, 'utils/tartarus.py', 'practice', '--help'],
            cwd=ROOT, check=True, text=True, capture_output=True,
        ).stdout
        self.assertNotIn('--no-audio', cli_help)
        self.assertIn('--fast', cli_help)
        result = subprocess.run(['make', 'help'], cwd=ROOT, check=True, text=True, capture_output=True)
        self.assertIn('make web', result.stdout)
        self.assertIn('make init user=<name> [list=<name>]', result.stdout)


if __name__ == '__main__':
    unittest.main()
