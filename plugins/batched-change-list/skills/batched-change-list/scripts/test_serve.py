import importlib.util
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


if __name__ == '__main__':
    unittest.main()
