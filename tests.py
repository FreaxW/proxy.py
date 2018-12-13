# -*- coding: utf-8 -*-
"""
    proxy.py
    ~~~~~~~~

    HTTP Proxy Server in Python.

    :copyright: (c) 2013-2018 by Abhinav Singh.
    :license: BSD, see LICENSE for more details.
"""
import sys
import base64
import socket
import logging
import unittest
from threading import Thread
from contextlib import closing
from proxy import Proxy, ChunkParser, HttpParser, Client
from proxy import ProxyAuthenticationFailed, ProxyConnectionFailed
from proxy import CRLF, text_, bytes_
from proxy import HTTP_PARSER_STATE_COMPLETE, CHUNK_PARSER_STATE_COMPLETE, \
    CHUNK_PARSER_STATE_WAITING_FOR_SIZE, CHUNK_PARSER_STATE_WAITING_FOR_DATA, \
    HTTP_PARSER_STATE_INITIALIZED, HTTP_PARSER_STATE_LINE_RCVD, HTTP_PARSER_STATE_RCVING_HEADERS, \
    HTTP_PARSER_STATE_HEADERS_COMPLETE, HTTP_PARSER_STATE_RCVING_BODY, HTTP_RESPONSE_PARSER, \
    PROXY_TUNNEL_ESTABLISHED_RESPONSE_PKT

# logging.basicConfig(level=logging.DEBUG,
#                     format='%(asctime)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s')

# True if we are running on Python 3.
if sys.version_info[0] == 3:
    from http.server import HTTPServer, BaseHTTPRequestHandler
else:
    from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler


class TestChunkParser(unittest.TestCase):

    def setUp(self):
        self.parser = ChunkParser()

    def test_chunk_parse(self):
        self.parser.parse(b''.join([
            b'4\r\n',
            b'Wiki\r\n',
            b'5\r\n',
            b'pedia\r\n',
            b'E\r\n',
            b' in\r\n\r\nchunks.\r\n',
            b'0\r\n',
            b'\r\n'
        ]))
        self.assertEqual(self.parser.chunk, b'')
        self.assertEqual(self.parser.size, None)
        self.assertEqual(self.parser.body, b'Wikipedia in\r\n\r\nchunks.')
        self.assertEqual(self.parser.state, CHUNK_PARSER_STATE_COMPLETE)

    def test_chunk_parse_issue_27(self):
        self.parser.parse(b'3')
        self.assertEqual(self.parser.chunk, b'3')
        self.assertEqual(self.parser.size, None)
        self.assertEqual(self.parser.body, b'')
        self.assertEqual(self.parser.state, CHUNK_PARSER_STATE_WAITING_FOR_SIZE)
        self.parser.parse(b'\r\n')
        self.assertEqual(self.parser.chunk, b'')
        self.assertEqual(self.parser.size, 3)
        self.assertEqual(self.parser.body, b'')
        self.assertEqual(self.parser.state, CHUNK_PARSER_STATE_WAITING_FOR_DATA)
        self.parser.parse(b'abc')
        self.assertEqual(self.parser.chunk, b'')
        self.assertEqual(self.parser.size, None)
        self.assertEqual(self.parser.body, b'abc')
        self.assertEqual(self.parser.state, CHUNK_PARSER_STATE_WAITING_FOR_SIZE)
        self.parser.parse(b'\r\n')
        self.assertEqual(self.parser.chunk, b'')
        self.assertEqual(self.parser.size, None)
        self.assertEqual(self.parser.body, b'abc')
        self.assertEqual(self.parser.state, CHUNK_PARSER_STATE_WAITING_FOR_SIZE)
        self.parser.parse(b'4\r\n')
        self.assertEqual(self.parser.chunk, b'')
        self.assertEqual(self.parser.size, 4)
        self.assertEqual(self.parser.body, b'abc')
        self.assertEqual(self.parser.state, CHUNK_PARSER_STATE_WAITING_FOR_DATA)
        self.parser.parse(b'defg\r\n0')
        self.assertEqual(self.parser.chunk, b'0')
        self.assertEqual(self.parser.size, None)
        self.assertEqual(self.parser.body, b'abcdefg')
        self.assertEqual(self.parser.state, CHUNK_PARSER_STATE_WAITING_FOR_SIZE)
        self.parser.parse(b'\r\n\r\n')
        self.assertEqual(self.parser.chunk, b'')
        self.assertEqual(self.parser.size, None)
        self.assertEqual(self.parser.body, b'abcdefg')
        self.assertEqual(self.parser.state, CHUNK_PARSER_STATE_COMPLETE)


class TestHttpParser(unittest.TestCase):

    def setUp(self):
        self.parser = HttpParser()

    def test_get_full_parse(self):
        raw = text_(CRLF, encoding='utf-8').join([
            'GET %s HTTP/1.1',
            'Host: %s',
            text_(CRLF, encoding='utf-8')
        ])
        self.parser.parse(bytes_(raw % ('https://example.com/path/dir/?a=b&c=d#p=q', 'example.com')))
        self.assertEqual(self.parser.build_url(), b'/path/dir/?a=b&c=d#p=q')
        self.assertEqual(self.parser.method, b'GET')
        self.assertEqual(self.parser.url.hostname, b'example.com')
        self.assertEqual(self.parser.url.port, None)
        self.assertEqual(self.parser.version, b'HTTP/1.1')
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_COMPLETE)
        self.assertDictContainsSubset({b'host': (b'Host', b'example.com')}, self.parser.headers)
        self.assertEqual(bytes_(raw % ('/path/dir/?a=b&c=d#p=q', 'example.com')),
                         self.parser.build(del_headers=[b'host'], add_headers=[(b'Host', b'example.com')]))

    def test_build_url_none(self):
        self.assertEqual(self.parser.build_url(), b'/None')

    def test_line_rcvd_to_rcving_headers_state_change(self):
        self.parser.parse(b'GET http://localhost HTTP/1.1')
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_INITIALIZED)
        self.parser.parse(CRLF)
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_LINE_RCVD)
        self.parser.parse(CRLF)
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_RCVING_HEADERS)

    def test_get_partial_parse1(self):
        self.parser.parse(CRLF.join([
            b'GET http://localhost:8080 HTTP/1.1'
        ]))
        self.assertEqual(self.parser.method, None)
        self.assertEqual(self.parser.url, None)
        self.assertEqual(self.parser.version, None)
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_INITIALIZED)

        self.parser.parse(CRLF)
        self.assertEqual(self.parser.method, b'GET')
        self.assertEqual(self.parser.url.hostname, b'localhost')
        self.assertEqual(self.parser.url.port, 8080)
        self.assertEqual(self.parser.version, b'HTTP/1.1')
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_LINE_RCVD)

        self.parser.parse(b'Host: localhost:8080')
        self.assertDictEqual(self.parser.headers, dict())
        self.assertEqual(self.parser.buffer, b'Host: localhost:8080')
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_LINE_RCVD)

        self.parser.parse(CRLF * 2)
        self.assertDictContainsSubset({b'host': (b'Host', b'localhost:8080')}, self.parser.headers)
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_COMPLETE)

    def test_get_partial_parse2(self):
        self.parser.parse(CRLF.join([
            b'GET http://localhost:8080 HTTP/1.1',
            b'Host: '
        ]))
        self.assertEqual(self.parser.method, b'GET')
        self.assertEqual(self.parser.url.hostname, b'localhost')
        self.assertEqual(self.parser.url.port, 8080)
        self.assertEqual(self.parser.version, b'HTTP/1.1')
        self.assertEqual(self.parser.buffer, b'Host: ')
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_LINE_RCVD)

        self.parser.parse(b'localhost:8080' + CRLF)
        self.assertDictContainsSubset({b'host': (b'Host', b'localhost:8080')}, self.parser.headers)
        self.assertEqual(self.parser.buffer, b'')
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_RCVING_HEADERS)

        self.parser.parse(b'Content-Type: text/plain' + CRLF)
        self.assertEqual(self.parser.buffer, b'')
        self.assertDictContainsSubset({b'content-type': (b'Content-Type', b'text/plain')}, self.parser.headers)
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_RCVING_HEADERS)

        self.parser.parse(CRLF)
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_COMPLETE)

    def test_post_full_parse(self):
        raw = text_(CRLF).join([
            'POST %s HTTP/1.1',
            'Host: localhost',
            'Content-Length: 7',
            'Content-Type: application/x-www-form-urlencoded' + text_(CRLF, encoding='utf-8'),
            'a=b&c=d'
        ])
        self.parser.parse(bytes_(raw % 'http://localhost'))
        self.assertEqual(self.parser.method, b'POST')
        self.assertEqual(self.parser.url.hostname, b'localhost')
        self.assertEqual(self.parser.url.port, None)
        self.assertEqual(self.parser.version, b'HTTP/1.1')
        self.assertDictContainsSubset({b'content-type': (b'Content-Type', b'application/x-www-form-urlencoded')},
                                      self.parser.headers)
        self.assertDictContainsSubset({b'content-length': (b'Content-Length', b'7')}, self.parser.headers)
        self.assertEqual(self.parser.body, b'a=b&c=d')
        self.assertEqual(self.parser.buffer, b'')
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_COMPLETE)
        self.assertEqual(len(self.parser.build()), len(raw % '/'))

    def test_post_partial_parse(self):
        self.parser.parse(CRLF.join([
            b'POST http://localhost HTTP/1.1',
            b'Host: localhost',
            b'Content-Length: 7',
            b'Content-Type: application/x-www-form-urlencoded'
        ]))
        self.assertEqual(self.parser.method, b'POST')
        self.assertEqual(self.parser.url.hostname, b'localhost')
        self.assertEqual(self.parser.url.port, None)
        self.assertEqual(self.parser.version, b'HTTP/1.1')
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_RCVING_HEADERS)

        self.parser.parse(CRLF)
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_RCVING_HEADERS)

        self.parser.parse(CRLF)
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_HEADERS_COMPLETE)

        self.parser.parse(b'a=b')
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_RCVING_BODY)
        self.assertEqual(self.parser.body, b'a=b')
        self.assertEqual(self.parser.buffer, b'')

        self.parser.parse(b'&c=d')
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_COMPLETE)
        self.assertEqual(self.parser.body, b'a=b&c=d')
        self.assertEqual(self.parser.buffer, b'')

    def test_connect_without_host_header_request_parse(self):
        """Case where clients can send CONNECT request without a Host header field.

        Example:
            1. pip3 --proxy http://localhost:8899 install <package name>
               Uses HTTP/1.0, Host header missing with CONNECT requests
            2. Android Emulator
               Uses HTTP/1.1, Host header missing with CONNECT requests
        See https://github.com/abhinavsingh/proxy.py/issues/5 for details
        """
        self.parser.parse(b'CONNECT pypi.org:443 HTTP/1.0\r\n\r\n')
        self.assertEqual(self.parser.method, b'CONNECT')
        self.assertEqual(self.parser.version, b'HTTP/1.0')
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_RCVING_HEADERS)

    def test_response_parse_without_content_length(self):
        """Case when server response doesn't contain a content-length header for non-chunk response types.

        HttpParser by itself has no way to know if more data should be expected.
        In example below, parser reaches state HTTP_PARSER_STATE_HEADERS_COMPLETE
        and it is responsibility of callee to change state to HTTP_PARSER_STATE_COMPLETE
        when server stream closes.
        """
        self.parser.type = HTTP_RESPONSE_PARSER
        self.parser.parse(b'HTTP/1.0 200 OK' + CRLF)
        self.assertEqual(self.parser.code, b'200')
        self.assertEqual(self.parser.version, b'HTTP/1.0')
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_LINE_RCVD)
        self.parser.parse(CRLF.join([
            b'Server: BaseHTTP/0.3 Python/2.7.10',
            b'Date: Thu, 13 Dec 2018 16:24:09 GMT',
            CRLF
        ]))
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_HEADERS_COMPLETE)

    def test_response_parse(self):
        self.parser.type = HTTP_RESPONSE_PARSER
        self.parser.parse(b''.join([
            b'HTTP/1.1 301 Moved Permanently\r\n',
            b'Location: http://www.google.com/\r\n',
            b'Content-Type: text/html; charset=UTF-8\r\n',
            b'Date: Wed, 22 May 2013 14:07:29 GMT\r\n',
            b'Expires: Fri, 21 Jun 2013 14:07:29 GMT\r\n',
            b'Cache-Control: public, max-age=2592000\r\n',
            b'Server: gws\r\n',
            b'Content-Length: 219\r\n',
            b'X-XSS-Protection: 1; mode=block\r\n',
            b'X-Frame-Options: SAMEORIGIN\r\n\r\n',
            b'<HTML><HEAD><meta http-equiv="content-type" content="text/html;charset=utf-8">\n' +
            b'<TITLE>301 Moved</TITLE></HEAD>',
            b'<BODY>\n<H1>301 Moved</H1>\nThe document has moved\n' +
            b'<A HREF="http://www.google.com/">here</A>.\r\n</BODY></HTML>\r\n'
        ]))
        self.assertEqual(self.parser.code, b'301')
        self.assertEqual(self.parser.reason, b'Moved Permanently')
        self.assertEqual(self.parser.version, b'HTTP/1.1')
        self.assertEqual(self.parser.body,
                         b'<HTML><HEAD><meta http-equiv="content-type" content="text/html;charset=utf-8">\n' +
                         b'<TITLE>301 Moved</TITLE></HEAD><BODY>\n<H1>301 Moved</H1>\nThe document has moved\n' +
                         b'<A HREF="http://www.google.com/">here</A>.\r\n</BODY></HTML>\r\n')
        self.assertDictContainsSubset({b'content-length': (b'Content-Length', b'219')}, self.parser.headers)
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_COMPLETE)

    def test_response_partial_parse(self):
        self.parser.type = HTTP_RESPONSE_PARSER
        self.parser.parse(b''.join([
            b'HTTP/1.1 301 Moved Permanently\r\n',
            b'Location: http://www.google.com/\r\n',
            b'Content-Type: text/html; charset=UTF-8\r\n',
            b'Date: Wed, 22 May 2013 14:07:29 GMT\r\n',
            b'Expires: Fri, 21 Jun 2013 14:07:29 GMT\r\n',
            b'Cache-Control: public, max-age=2592000\r\n',
            b'Server: gws\r\n',
            b'Content-Length: 219\r\n',
            b'X-XSS-Protection: 1; mode=block\r\n',
            b'X-Frame-Options: SAMEORIGIN\r\n'
        ]))
        self.assertDictContainsSubset({b'x-frame-options': (b'X-Frame-Options', b'SAMEORIGIN')}, self.parser.headers)
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_RCVING_HEADERS)
        self.parser.parse(b'\r\n')
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_HEADERS_COMPLETE)
        self.parser.parse(
            b'<HTML><HEAD><meta http-equiv="content-type" content="text/html;charset=utf-8">\n' +
            b'<TITLE>301 Moved</TITLE></HEAD>')
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_RCVING_BODY)
        self.parser.parse(
            b'<BODY>\n<H1>301 Moved</H1>\nThe document has moved\n' +
            b'<A HREF="http://www.google.com/">here</A>.\r\n</BODY></HTML>\r\n')
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_COMPLETE)

    def test_chunked_response_parse(self):
        self.parser.type = HTTP_RESPONSE_PARSER
        self.parser.parse(b''.join([
            b'HTTP/1.1 200 OK\r\n',
            b'Content-Type: application/json\r\n',
            b'Date: Wed, 22 May 2013 15:08:15 GMT\r\n',
            b'Server: gunicorn/0.16.1\r\n',
            b'transfer-encoding: chunked\r\n',
            b'Connection: keep-alive\r\n\r\n',
            b'4\r\n',
            b'Wiki\r\n',
            b'5\r\n',
            b'pedia\r\n',
            b'E\r\n',
            b' in\r\n\r\nchunks.\r\n',
            b'0\r\n',
            b'\r\n'
        ]))
        self.assertEqual(self.parser.body, b'Wikipedia in\r\n\r\nchunks.')
        self.assertEqual(self.parser.state, HTTP_PARSER_STATE_COMPLETE)


class MockConnection(object):

    def __init__(self, b=b''):
        self.buffer = b

    def recv(self, b=8192):
        data = self.buffer[:b]
        self.buffer = self.buffer[b:]
        return data

    def send(self, data):
        return len(data)

    def queue(self, data):
        self.buffer += data


class MockHTTPRequestHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        if self.path != '/no-content-length':
            self.send_header('content-length', 2)
        self.end_headers()
        self.wfile.write(b'OK')


class TestProxy(unittest.TestCase):

    mock_server = None
    mock_server_port = None
    mock_server_thread = None

    @staticmethod
    def get_available_port():
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            sock.bind(('', 0))
            _, port = sock.getsockname()
            return port

    @classmethod
    def setUpClass(cls):
        cls.mock_server_port = cls.get_available_port()
        cls.mock_server = HTTPServer(('127.0.0.1', cls.mock_server_port), MockHTTPRequestHandler)
        cls.mock_server_thread = Thread(target=cls.mock_server.serve_forever)
        cls.mock_server_thread.setDaemon(True)
        cls.mock_server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.mock_server.shutdown()
        cls.mock_server.server_close()
        cls.mock_server_thread.join()

    def setUp(self):
        self._conn = MockConnection()
        self._addr = ('127.0.0.1', 54382)
        self.proxy = Proxy(Client(self._conn, self._addr))

    def test_http_get(self):
        # Send request line
        self.proxy.client.conn.queue(bytes_('GET http://localhost:%d/get HTTP/1.1' % self.mock_server_port) + CRLF)
        self.proxy._process_request(self.proxy.client.recv())
        self.assertNotEqual(self.proxy.request.state, HTTP_PARSER_STATE_COMPLETE)
        # Send headers and blank line, thus completing HTTP request
        self.proxy.client.conn.queue(CRLF.join([
            b'User-Agent: curl/7.27.0',
            bytes_('Host: localhost:%d' % self.mock_server_port),
            b'Accept: */*',
            b'Proxy-Connection: Keep-Alive',
            CRLF
        ]))
        self.proxy._process_request(self.proxy.client.recv())
        self.assertEqual(self.proxy.request.state, HTTP_PARSER_STATE_COMPLETE)
        self.assertEqual(self.proxy.server.addr, (b'localhost', self.mock_server_port))
        # Flush data queued for server
        self.proxy.server.flush()
        self.assertEqual(self.proxy.server.buffer_size(), 0)
        # Receive full response from server
        data = self.proxy.server.recv()
        while data:
            self.proxy._process_response(data)
            logging.info(self.proxy.response.state)
            if self.proxy.response.state == HTTP_PARSER_STATE_COMPLETE:
                break
            data = self.proxy.server.recv()
        # Verify 200 success response code
        self.assertEqual(self.proxy.response.state, HTTP_PARSER_STATE_COMPLETE)
        self.assertEqual(int(self.proxy.response.code), 200)

    def test_https_get(self):
        self.proxy.client.conn.queue(CRLF.join([
            b'CONNECT httpbin.org:80 HTTP/1.1',
            b'Host: httpbin.org:80',
            b'User-Agent: curl/7.27.0',
            b'Proxy-Connection: Keep-Alive',
            CRLF
        ]))
        self.proxy._process_request(self.proxy.client.recv())
        self.assertFalse(self.proxy.server is None)
        self.assertEqual(self.proxy.client.buffer, PROXY_TUNNEL_ESTABLISHED_RESPONSE_PKT)

        parser = HttpParser(HTTP_RESPONSE_PARSER)
        parser.parse(self.proxy.client.buffer)
        self.assertEqual(parser.state, HTTP_PARSER_STATE_HEADERS_COMPLETE)
        self.assertEqual(int(parser.code), 200)

        self.proxy.client.flush()
        self.assertEqual(self.proxy.client.buffer_size(), 0)

        self.proxy.client.conn.queue(CRLF.join([
            b'GET /user-agent HTTP/1.1',
            b'Host: httpbin.org',
            b'User-Agent: curl/7.27.0',
            CRLF
        ]))
        self.proxy._process_request(self.proxy.client.recv())
        self.proxy.server.flush()
        self.assertEqual(self.proxy.server.buffer_size(), 0)

        parser = HttpParser(HTTP_RESPONSE_PARSER)
        data = self.proxy.server.recv()
        while data:
            parser.parse(data)
            if parser.state == HTTP_PARSER_STATE_COMPLETE:
                break
            data = self.proxy.server.recv()

        self.assertEqual(parser.state, HTTP_PARSER_STATE_COMPLETE)
        self.assertEqual(int(parser.code), 200)

    def test_proxy_connection_failed(self):
        with self.assertRaises(ProxyConnectionFailed):
            self.proxy._process_request(CRLF.join([
                b'GET http://unknown.domain HTTP/1.1',
                b'Host: unknown.domain',
                CRLF
            ]))

    def test_proxy_authentication_failed(self):
        self.proxy = Proxy(Client(self._conn, self._addr), b'Basic %s' % base64.b64encode(bytes_('user:pass')))

        with self.assertRaises(ProxyAuthenticationFailed):
            self.proxy._process_request(CRLF.join([
                b'GET http://abhinavsingh.com HTTP/1.1',
                b'Host: abhinavsingh.com',
                CRLF
            ]))

    def test_authenticated_proxy_http_get(self):
        self.proxy = Proxy(Client(self._conn, self._addr), b'Basic %s' % base64.b64encode(bytes_('user:pass')))

        self.proxy.client.conn.queue(bytes_('GET http://localhost:%d/get HTTP/1.1' % self.mock_server_port) + CRLF)
        self.proxy._process_request(self.proxy.client.recv())
        self.assertNotEqual(self.proxy.request.state, HTTP_PARSER_STATE_COMPLETE)

        self.proxy.client.conn.queue(CRLF.join([
            b'User-Agent: curl/7.27.0',
            bytes_('Host: localhost:%d' % self.mock_server_port),
            b'Accept: */*',
            b'Proxy-Connection: Keep-Alive',
            b'Proxy-Authorization: Basic dXNlcjpwYXNz',
            CRLF
        ]))

        self.proxy._process_request(self.proxy.client.recv())
        self.assertEqual(self.proxy.request.state, HTTP_PARSER_STATE_COMPLETE)
        self.assertEqual(self.proxy.server.addr, (b'localhost', self.mock_server_port))

        self.proxy.server.flush()
        self.assertEqual(self.proxy.server.buffer_size(), 0)

        data = self.proxy.server.recv()
        while data:
            self.proxy._process_response(data)
            if self.proxy.response.state == HTTP_PARSER_STATE_COMPLETE:
                break
            data = self.proxy.server.recv()

        self.assertEqual(self.proxy.response.state, HTTP_PARSER_STATE_COMPLETE)
        self.assertEqual(int(self.proxy.response.code), 200)


if __name__ == '__main__':
    unittest.main()
