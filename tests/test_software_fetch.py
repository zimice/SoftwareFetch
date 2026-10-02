import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import SoftwareFetch as f

CONFIG = Path(__file__).resolve().parents[1] / 'config.json'
WANTED = {
    '7zip', 'Brave', 'Bitwarden', 'Firefox', 'MEGAsync', 'Python', 'Notion',
    'MobaXterm', 'Office', 'VS Code', 'Git Bash', 'KeePass', 'Rufus Portable',
    'Open Hardware Monitor', 'Tor Browser', 'Discord', 'Steam',
    'DB Browser for SQLite', 'VirtualBox', 'VLC', 'Wireshark', 'WinSCP',
}


class Response:
    def __init__(self, body=b'', content_type='application/octet-stream', url='https://example.org/setup.exe', status=200, extra=None):
        self.body = body
        self.url = url
        self.headers = {'Content-Type': content_type, 'Content-Length': str(len(body))}
        self.headers.update(extra or {})
        self.status_code = status
        self.text = body.decode('utf-8') if 'html' in content_type else ''

    def json(self):
        return json.loads(self.body.decode('utf-8'))

    def raise_for_status(self):
        if self.status_code >= 400:
            raise f.requests.HTTPError(f'HTTP {self.status_code}')

    def iter_content(self, chunk_size=None):
        yield self.body[:3]
        yield self.body[3:]

    def __enter__(self): return self
    def __exit__(self, *args): return None


class Tests(unittest.TestCase):
    def test_all_requested_programs_configured(self):
        apps, dest, workers = f.load_config(CONFIG)
        names = {app.name for app in apps}
        self.assertEqual(WANTED, names - {'Visual C++ Redistributable x64'})
        self.assertEqual(len(apps), 23)
        self.assertEqual(sum(a.source['type'] == 'manual' for a in apps), 1)
        self.assertTrue(1 <= workers <= 32)

    def test_dependency_is_included(self):
        apps, _, _ = f.load_config(CONFIG)
        selected = f.select_apps(apps, ['VirtualBox'])
        self.assertEqual({a.name for a in selected}, {'VirtualBox', 'Visual C++ Redistributable x64'})
        self.assertEqual(selected[0].name, 'Visual C++ Redistributable x64')

    def test_dependencies_cycles_rejected(self):
        a = f.App('a', '.exe', {'type':'direct', 'url':'https://example.org/a'}, requires=('b',))
        b = f.App('b', '.exe', {'type':'direct', 'url':'https://example.org/b'}, requires=('a',))
        with self.assertRaisesRegex(ValueError, 'circular'):
            f.select_apps([a, b], ['a'])

    def test_unknown_app_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'unknown'):
            f.select_apps([], ['missing'])

    def test_no_bad_output_filename(self):
        for value in ['CON', 'NUL.txt', 'LPT1']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                f.safe_name(value)
        self.assertEqual(f.safe_name('Example: App'), 'Example_ App')

    def test_only_https_urls_allowed(self):
        self.assertFalse(f.https_url('http://example.org/setup.exe'))
        self.assertFalse(f.https_url('file:///evil.exe'))
        self.assertFalse(f.https_url('https://user:password@example.org'))
        self.assertTrue(f.https_url('https://example.org/setup.exe'))

    def test_page_scraping_handles_download_suffix_and_skips_arm64(self):
        html = '''<a href="/download/WinSCP-6.5.7-Setup.exe/download">get</a>
                  <a href="/WinSCP-6.5.6-Portable.zip">other</a>'''
        self.assertEqual(
            f.matching_links(html, 'https://winscp.net/eng/download.php',
                             f.re.compile(r'WinSCP-[0-9.]+-Setup\.exe')),
            ['https://winscp.net/download/WinSCP-6.5.7-Setup.exe/download'])
        page = '<a href="https://example.org/aarch64.exe">arm</a><a href="https://example.org/python-3.14.8-amd64.exe">x64</a>'
        matches = f.matching_links(page, 'https://example.org/', f.re.compile(r'python-3\.[0-9.]+-amd64\.exe'))
        self.assertEqual(matches, ['https://example.org/python-3.14.8-amd64.exe'])

    def test_github_release_selects_stable_matching_asset(self):
        app = f.App('DB Browser for SQLite', '.msi', {
            'type':'github_release', 'repo':'sqlitebrowser/sqlitebrowser',
            'filename_regex':r'DB\.Browser\.for\.SQLite-v[0-9.]+-win64\.msi'})
        data = {'assets': [
            {'name':'DB.Browser.for.SQLite-v3.13.1-win32.msi','browser_download_url':'https://example.org/win32.msi'},
            {'name':'DB.Browser.for.SQLite-v3.13.1-win64.msi','browser_download_url':'https://github.com/example/win64.msi'},
        ]}
        response = Response(json.dumps(data).encode())
        session = Mock()
        session.get.return_value = response
        self.assertEqual(f.resolve_url(session, app), 'https://github.com/example/win64.msi')
        self.assertIn('releases/latest', session.get.call_args.args[0])

    def test_binary_download_is_atomic_and_sha256_correct(self):
        payload = b'MZ' + b'\x00' * 1254
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'setup.exe'
            result = f.copy_response(Response(payload), output, '.exe', False)
            self.assertEqual(result, (len(payload), True, hashlib.sha256(payload).hexdigest()))
            self.assertEqual(output.read_bytes(), payload)
            self.assertFalse(Path(str(output) + '.part').exists())
            self.assertFalse(f.copy_response(Response(payload), output, '.exe', False)[1])

    def test_reject_html_and_wrong_magic_without_traces(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'setup.exe'
            with self.assertRaisesRegex(ValueError, 'instead of an installer'):
                f.copy_response(Response(b'<html>' * 300, 'text/html'), output, '.exe', False)
            with self.assertRaisesRegex(ValueError, 'file signature'):
                f.copy_response(Response(b'not executable' * 100), output, '.exe', False)
            self.assertFalse(output.exists())
            self.assertFalse((Path(folder) / 'setup.exe.part').exists())

    def test_reject_incomplete_response(self):
        with tempfile.TemporaryDirectory() as folder:
            response = Response(b'MZ' + b'0' * 1200, extra={'Content-Length': '4000'})
            with self.assertRaisesRegex(f.IncompleteDownload, 'incomplete'):
                f.copy_response(response, Path(folder) / 'setup.exe', '.exe', False)

    def test_manual_office_creates_shortcut_and_no_network(self):
        apps, _, _ = f.load_config(CONFIG)
        office = next(app for app in apps if app.name == 'Office')
        with tempfile.TemporaryDirectory() as folder, patch.object(f, 'session_for_app', side_effect=AssertionError('network called')):
            result = f.fetch_app(office, Path(folder), False)
            self.assertTrue(result['manual'])
            self.assertEqual(result['path'].suffix, '.url')
            self.assertIn('microsoft.com', result['path'].read_text())

    def test_checksums_and_verify(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / 'a.exe'
            target.write_bytes(b'MZ'+b'0'*1200)
            app = f.App('a', '.exe', {'type':'direct','url':'https://example.org/a.exe'})
            f.write_hashes(root, [{'name':'a','path':target,'sha256':f.file_hash(target),'manual':False}])
            self.assertEqual(f.verify_files([app], root), 0)
            target.write_bytes(b'MZ'+b'1'*1200)
            self.assertEqual(f.verify_files([app], root), 1)

    def test_config_rejects_invalid_schemes(self):
        with tempfile.TemporaryDirectory() as folder:
            c = Path(folder) / 'config.json'
            c.write_text(json.dumps({'apps':[{'name':'test','extension':'.exe','source':{'type':'direct','url':'http://evil.example/x.exe'}}]}))
            with self.assertRaisesRegex(ValueError, 'HTTPS'):
                f.load_config(c)

    def test_cli_list(self):
        self.assertEqual(f.main(['--list', '--config', str(CONFIG)]), 0)


if __name__ == '__main__':
    unittest.main()
