import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from courselink_mcp.config import Config


async def test_real_stdio_bridge_reuses_http_connection(tmp_path, monkeypatch):
    monkeypatch.setenv('COURSELINK_STATE_DIR', str(tmp_path))
    config = Config.load()
    connections = []
    headers_seen = []
    handlers = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        connections.append(writer)
        try:
            while True:
                header = await reader.readuntil(b'\r\n\r\n')
                headers = dict(line.split(b': ', 1) for line in header.split(b'\r\n')[1:] if b': ' in line)
                headers = {key.lower(): value for key, value in headers.items()}
                headers_seen.append(headers)
                await reader.readexactly(int(headers.get(b'content-length', b'0')))
                body = json.dumps({'session': {'state': 'authenticated'}, 'monitored_courses': {}}).encode()
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '
                             + str(len(body)).encode() + b'\r\n\r\n' + body)
                await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(handle, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    params = StdioServerParameters(command=sys.executable, args=['-m', 'courselink_mcp.cli', 'stdio'],
        env={'COURSELINK_STATE_DIR': str(tmp_path), 'COURSELINK_SERVER_URL': f'http://127.0.0.1:{port}'})
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for _ in range(2):
                    result = await session.call_tool('courselink_status')
                    assert not result.isError
        assert len(connections) == 1 and len(headers_seen) == 2
        assert all(h[b'authorization'].decode() == 'Bearer ' + config.token() for h in headers_seen)
    finally:
        server.close()
        await server.wait_closed()
        for writer in connections:
            writer.close()
        if handlers:
            await asyncio.gather(*handlers)
