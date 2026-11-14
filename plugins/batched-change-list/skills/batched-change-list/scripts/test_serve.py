import importlib.util
import json
from http.server import HTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

spec = importlib.util.spec_from_file_location('report_server', Path(__file__).with_name('serve.py'))
server_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server_module)


class ServerTests(unittest.TestCase):
    def test_live_source_resolves_capture_from_registered_report_base(self):
        example = Path(__file__).parents[1] / 'examples/findings.json'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state'
            live = state / 'example-project' / '110.json'
            live.parent.mkdir(parents=True)
            home = root / 'project'
            base = home / 'docs/triage'
            capture = base / 'captures/proof.png'
            capture.parent.mkdir(parents=True)
            capture.write_bytes(b'proof')
            document = json.loads(example.read_text())
            document['findings'][0]['evidence'] = [{'src':'captures/proof.png','alt':'proof','caption':'proof'}]
            live.write_text(json.dumps(document))
            record = {'live':str(live),'home':str(home),'snapshot':'docs/triage/110-findings.json',
                      'html':'docs/triage/110.html','repo':'example/project','id':'110'}
            state.mkdir(exist_ok=True)
            (state / 'registry.json').write_text(json.dumps({'example/project/110':record}))
            page = base / '110.html'
            with patch.object(server_module.live_state, 'STATE', state):
                with HTTPServer(('127.0.0.1', 0), server_module.handler_for(page, live)) as server:
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        with urlopen(f'http://127.0.0.1:{server.server_port}/') as response:
                            content = response.read().decode()
                        self.assertIn('data:image/png;base64,cHJvb2Y=', content)
                    finally:
                        server.shutdown()
                        thread.join()

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

    def test_data_fingerprint_is_stable_and_tracks_tracker_and_inventory_without_rendering_head(self):
        example = Path(__file__).parents[1] / 'examples/findings.json'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page, data, inventory = root / 'report.html', root / 'findings.json', root / 'inventory.json'
            page.write_text('fallback')
            document = json.loads(example.read_text())
            data.write_text(json.dumps(document))
            inventory.write_text(json.dumps({'versions': {'batched-change-list':'0.1.16'},
                                             'observed_at':'2026-11-14T07:55:00Z','source':'test inventory'}))
            with patch.object(server_module.RENDERER, 'render', wraps=server_module.RENDERER.render) as render:
                with HTTPServer(('127.0.0.1', 0), server_module.handler_for(page, data, inventory)) as server:
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    url = f'http://127.0.0.1:{server.server_port}/'
                    def etag():
                        with urlopen(Request(url, method='HEAD')) as response:
                            return response.headers['ETag']
                    try:
                        first = etag()
                        self.assertEqual(etag(), first)
                        render.assert_not_called()
                        document['title'] += ' updated'
                        data.write_text(json.dumps(document))
                        tracker_changed = etag()
                        self.assertNotEqual(tracker_changed, first)
                        inventory.write_text(json.dumps({'versions': {'batched-change-list':'0.1.16'},
                                                         'observed_at':'2026-11-14T07:56:00Z','source':'test inventory'}))
                        inventory_changed = etag()
                        self.assertNotEqual(inventory_changed, tracker_changed)
                        render.assert_not_called()
                        with urlopen(url) as response:
                            body = response.read().decode()
                            self.assertEqual(response.headers['ETag'], inventory_changed)
                        self.assertIn('batched-change-list 0.1.16', body)
                        render.assert_called_once()
                    finally:
                        server.shutdown()
                        thread.join()

    def test_live_source_discovers_installed_version_without_inventory_file(self):
        example = Path(__file__).parents[1] / 'examples/findings.json'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page, data = root / 'report.html', root / 'findings.json'
            page.write_text('fallback')
            data.write_text(example.read_text())
            inventory = {'versions': {'batched-change-list':'0.1.16'},
                         'sources': {'batched-change-list':'agents plugins'},
                         'observed_at':'2026-11-14T07:55:00+00:00','source':'installed copies'}
            with patch.object(server_module.RENDERER, 'discover_installed_plugins', return_value=inventory) as discover:
                with HTTPServer(('127.0.0.1', 0), server_module.handler_for(page, data)) as server:
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        with urlopen(f'http://127.0.0.1:{server.server_port}/') as response:
                            content = response.read().decode()
                        self.assertIn('Inventory checked', content)
                        self.assertIn('agents plugins', content)
                        self.assertIn('batched-change-list 0.1.16', content)
                        discover.assert_called_once()
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
                    self.assertIn('Inventory checked', content)
                    self.assertIn('batched-change-list 0.1.3', content)
                finally:
                    server.shutdown()
                    thread.join()


if __name__ == '__main__':
    unittest.main()
