import importlib.util
import json
from http.server import HTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

spec = importlib.util.spec_from_file_location('report_server', Path(__file__).with_name('serve.py'))
server_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server_module)


class ServerTests(unittest.TestCase):
    def test_refresh_and_reject_other_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            page = Path(directory) / 'report.html'
            page.write_text('first')
            with HTTPServer(('127.0.0.1', 0), server_module.handler_for(page)) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                url = f'http://127.0.0.1:{server.server_port}/'
                try:
                    with urlopen(Request(url, method='HEAD')) as response:
                        before = response.headers['ETag']
                    page.write_text('second')
                    with urlopen(url) as response:
                        self.assertEqual(response.read(), b'second')
                        self.assertNotEqual(before, response.headers['ETag'])
                    with self.assertRaises(HTTPError) as error:
                        urlopen(url + 'private')
                    self.assertEqual(error.exception.code, 404)
                finally:
                    server.shutdown()
                    thread.join()

    def test_live_source_renders_inventory_override(self):
        example = Path(__file__).parents[1] / 'examples/findings.json'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = root / 'report.html'
            page.write_text('fallback')
            data = root / 'findings.json'
            data.write_text(example.read_text())
            inventory = root / 'inventory.json'
            inventory.write_text(json.dumps({'versions':{'batched-change-list':'0.1.3'},
                                             'observed_at':'2026-11-10T00:00:00Z','source':'live'}))
            with HTTPServer(('127.0.0.1', 0), server_module.handler_for(page, data, inventory)) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with urlopen(f'http://127.0.0.1:{server.server_port}/') as response:
                        content = response.read().decode()
                    self.assertIn('Inventory supplied at render time', content)
                    self.assertIn('<strong>0.1.3</strong>', content)
                finally:
                    server.shutdown()
                    thread.join()


if __name__ == '__main__':
    unittest.main()
