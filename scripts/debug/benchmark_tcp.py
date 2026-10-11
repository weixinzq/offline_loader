"""Local-only GameSocket/MessageRouter TCP soak test; requires psutil.

Run: python -m scripts.debug.benchmark_tcp --connections 200 --hold-seconds 3600
No account configuration is read. The mock server always binds to 127.0.0.1.
"""
from __future__ import annotations

import argparse
import asyncio
from array import array
import contextvars
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import struct
import subprocess
import sys
import time

import psutil

from src.accounts.login import LoginResult, ZoneData
from src.config import UpCmd
from src.network.context import AppContext, MessageRouter
from src.network.socket_client import GameSocket
from src.protocol.amf3 import Amf3Writer
from src.protocol.encrypt import outer_xor_encrypt

LOCAL_HOST = '127.0.0.1'
PAYLOAD_BYTES = 256
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def save_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def statistics(values) -> dict:
    if not values:
        return {'count': 0}
    ordered = sorted(values)
    result = {'count': len(values), 'min_ms': ordered[0], 'max_ms': ordered[-1],
              'mean_ms': sum(values) / len(values)}
    for label, fraction in [('p50_ms', .50), ('p95_ms', .95), ('p99_ms', .99)]:
        result[label] = ordered[max(0, int(len(ordered) * fraction + .999999) - 1)]
    return {key: round(value, 3) if isinstance(value, float) else value
            for key, value in result.items()}


async def mock_server() -> None:
    next_user = 0

    async def handle(reader, writer):
        nonlocal next_user
        next_user += 1
        user_id = next_user
        try:
            writer.write(b'<cross-domain-policy/>\0')
            await writer.drain()
            while True:
                length = struct.unpack('>I', await reader.readexactly(4))[0]
                packet = await reader.readexactly(length)
                sequence = struct.unpack('>I', packet[:4])[0]
                body = outer_xor_encrypt(packet[4:], sequence)
                action = struct.unpack('>H', body[1:3])[0]
                if action == UpCmd.Login:
                    response = {'_cmd': 'logOK', 'id': user_id}
                elif action == UpCmd.XtReq:
                    command_length = struct.unpack('>H', body[9:11])[0]
                    command = body[11:11 + command_length].decode('utf-8')
                    if not command.startswith('benchmark_probe:'):
                        continue  # Receive the real SessionClock's 55_2 without replying.
                    response = {'_cmd': 'benchmark_ack', '_ext_id': 0,
                                'token': command.split(':', 1)[1]}
                else:
                    continue
                encoder = Amf3Writer()
                encoder.write_object(response)
                reply = bytes([1]) + encoder.to_bytes()
                writer.write(struct.pack('>I', len(reply)) + reply)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, LOCAL_HOST, 0, backlog=512)
    print(json.dumps({'port': server.sockets[0].getsockname()[1]}), flush=True)
    async with server:
        await server.serve_forever()


async def benchmark(args) -> dict:
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    output = PROJECT_ROOT / 'evidence' / 'tcp-benchmark' / stamp
    output.mkdir(parents=True)
    connections = []
    connection_records = []
    rtt_values = array('d')
    failures = []
    samples = []
    probe_tasks = []
    client = psutil.Process()
    server = None
    server_log = (output / 'server.log').open('w', encoding='utf-8')
    hold_start = None
    completed = False
    state = {'status': 'starting', 'started_at': now(), 'client_pid': os.getpid(),
             'target_connections': args.connections, 'target_hold_seconds': args.hold_seconds,
             'connect_concurrency': args.connect_concurrency,
             'probe_interval_seconds': args.probe_interval, 'probe_payload_bytes': PAYLOAD_BYTES,
             'event_loop': type(asyncio.get_running_loop()).__name__,
             'python': sys.version.split()[0], 'output_directory': str(output),
             'scope': 'Loopback TCP, mock login, GameSocket, MessageRouter, AppContext, SessionClock; no HTTP/game authentication or battle/UI workload.'}
    save_json(output / 'status.json', state)
    print(json.dumps(state), flush=True)
    original_open = asyncio.open_connection
    timing_record = contextvars.ContextVar('connection_timing', default=None)

    async def timed_open(*a, **kw):
        started = time.perf_counter()
        try:
            return await original_open(*a, **kw)
        finally:
            record = timing_record.get()
            if record is not None:
                record['tcp_connect_ms'] = (time.perf_counter() - started) * 1000

    async def connect_one(index, slots, port):
        record = {'connection': index}
        socket = GameSocket('local-benchmark-session')
        connection_records.append(record)
        context = None
        try:
            async with slots:
                timing_record.set(record)
                started = time.perf_counter()
                if not await socket.connect_tcp(LOCAL_HOST, port, timeout=10):
                    raise ConnectionError(socket.disconnect_reason)
                record['tcp_with_policy_ms'] = (time.perf_counter() - started) * 1000
                started = time.perf_counter()
                if not await socket.login('0 local', str(index), 'local-benchmark-session'):
                    raise ConnectionError('Mock login failed')
                record['mock_login_ms'] = (time.perf_counter() - started) * 1000
                router = MessageRouter(socket)
                result = LoginResult(True, user_id=str(socket.my_user_id))
                zone = ZoneData(0, 'local', LOCAL_HOST, port)
                context = AppContext({}, result, zone, socket, router)
                context.player_initialized = True  # HTTP/player initialization is outside this test.
                router.start()
                context.session_clock.start(120)
                connections.append({'context': context, 'index': index,
                                    'last_reply': None, 'probes_ok': 0, 'probes_failed': 0})
                record['connected'] = True
        except Exception as exc:
            record.update(connected=False, error=f'{type(exc).__name__}: {exc}')
            if context is not None:
                await context.close()
            else:
                await socket.close()

    async def probe(connection):
        context = connection['context']
        subscription = context.messages.subscribe(max_queue_size=10)
        deadline = hold_start + (connection['index'] - 1) * args.probe_interval / args.connections
        token = 0
        try:
            while time.perf_counter() < hold_start + args.hold_seconds:
                await asyncio.sleep(max(0, deadline - time.perf_counter()))
                token += 1
                started = time.perf_counter()
                try:
                    await context.socket.send_xt_message(
                        0, f'benchmark_probe:{token}', {'payload': 'x' * PAYLOAD_BYTES})
                    await subscription.wait_for(
                        lambda message: message.get('_cmd') == 'benchmark_ack'
                        and message.get('token') == str(token), timeout=5)
                    rtt_values.append((time.perf_counter() - started) * 1000)
                    connection['last_reply'] = time.perf_counter()
                    connection['probes_ok'] += 1
                except Exception as exc:
                    connection['probes_failed'] += 1
                    if len(failures) < 200:
                        failures.append({'connection': connection['index'], 'time': now(),
                                         'error': f'{type(exc).__name__}: {exc}'})
                    if not context.socket.connected:
                        return
                deadline = max(deadline + args.probe_interval, time.perf_counter())
        finally:
            subscription.close()

    def sample(server_process):
        elapsed = time.perf_counter() - hold_start
        alive = sum(c['context'].socket.connected and not c['context'].messages.disconnected.is_set()
                    for c in connections)
        responding = sum(c['last_reply'] is not None
                         and time.perf_counter() - c['last_reply'] <= args.probe_interval + 5
                         for c in connections)
        record = {'time': now(), 'elapsed_seconds': round(elapsed, 3), 'active_connections': alive,
                  'recently_responding': responding,
                  'probes_ok': sum(c['probes_ok'] for c in connections),
                  'probes_failed': sum(c['probes_failed'] for c in connections),
                  'client_rss_mib': round(client.memory_info().rss / 1048576, 3),
                  'client_cpu_percent_one_core': client.cpu_percent(),
                  'client_handles': client.num_handles() if os.name == 'nt' else client.num_fds()}
        if server_process.is_running():
            record['server_rss_mib'] = round(server_process.memory_info().rss / 1048576, 3)
            record['server_cpu_percent_one_core'] = server_process.cpu_percent()
        samples.append(record)
        with (output / 'samples.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record) + '\n')
        state.update(record, status='holding')
        save_json(output / 'status.json', state)
        print(json.dumps(record), flush=True)
        return alive

    try:
        server = await asyncio.create_subprocess_exec(
            sys.executable, '-m', 'scripts.debug.benchmark_tcp', '--server',
            cwd=PROJECT_ROOT, stdout=asyncio.subprocess.PIPE, stderr=server_log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        port = json.loads(await asyncio.wait_for(server.stdout.readline(), timeout=20))['port']
        state.update(server_pid=server.pid, mock_endpoint=f'{LOCAL_HOST}:{port}', status='connecting')
        save_json(output / 'status.json', state)
        slots = asyncio.Semaphore(args.connect_concurrency)
        asyncio.open_connection = timed_open  # Instrument this benchmark process only.
        establish_start = time.perf_counter()
        with (output / 'client.log').open('w', encoding='utf-8') as log, redirect_stdout(log):
            await asyncio.gather(*(connect_one(i + 1, slots, port) for i in range(args.connections)))
        state['establish_all_seconds'] = round(time.perf_counter() - establish_start, 3)
        asyncio.open_connection = original_open
        save_json(output / 'connection-latencies.json', {'connections': connection_records})
        if len(connections) != args.connections:
            raise RuntimeError(f'Only {len(connections)}/{args.connections} connections established')
        hold_start = time.perf_counter()  # Every established connection shares the full hold window.
        server_process = psutil.Process(server.pid)
        client.cpu_percent()
        server_process.cpu_percent()
        probe_tasks = [asyncio.create_task(probe(c)) for c in connections]
        sample(server_process)
        while time.perf_counter() - hold_start < args.hold_seconds:
            # Windows timers may wake slightly early; avoid tiny final sampling intervals.
            await asyncio.sleep(min(30, max(0, args.hold_seconds - (time.perf_counter() - hold_start))) + .02)
            if sample(server_process) != args.connections:
                raise RuntimeError('Unexpected connection loss; no automatic reconnect')
        completed = True
    except (Exception, asyncio.CancelledError) as exc:
        state['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        asyncio.open_connection = original_open
        for task in probe_tasks:
            task.cancel()
        await asyncio.gather(*probe_tasks, return_exceptions=True)
        held_seconds = 0 if hold_start is None else time.perf_counter() - hold_start
        connection_health = [{'connection': c['index'], 'connected_at_end': c['context'].socket.connected,
                              'probes_ok': c['probes_ok'], 'probes_failed': c['probes_failed'],
                              'receive_error': c['context'].messages.last_receive_error,
                              'session_error': c['context'].automation_block_reason}
                             for c in connections]
        cleanup_results = await asyncio.gather(
            *(c['context'].close() for c in connections), return_exceptions=True)
        cleanup_errors = [str(result) for result in cleanup_results if isinstance(result, BaseException)]
        if server is not None and server.returncode is None:
            server.terminate()
            await server.wait()
        server_log.close()
        passed = (completed and held_seconds >= args.hold_seconds and not cleanup_errors
                  and all(c['probes_ok'] > 0 and c['probes_failed'] == 0
                          and not c['receive_error'] and not c['session_error']
                          for c in connection_health))
        state.update(status='passed' if passed else 'failed', finished_at=now(),
                     actual_hold_seconds=round(held_seconds, 3),
                     established_connections=len(connections),
                     connected_after_cleanup=sum(c['context'].socket.connected for c in connections),
                     latency_statistics={key: statistics([r[key] for r in connection_records
                                                          if r.get('connected') and key in r])
                                         for key in ['tcp_connect_ms', 'tcp_with_policy_ms', 'mock_login_ms']},
                     probe_rtt=statistics(rtt_values), probe_failures=failures,
                     connection_health=connection_health, cleanup_errors=cleanup_errors)
        if samples:
            state['resources'] = {
                'client_rss_start_mib': samples[0]['client_rss_mib'],
                'client_rss_end_mib': samples[-1]['client_rss_mib'],
                'client_rss_peak_mib': max(s['client_rss_mib'] for s in samples),
                'client_cpu_peak_percent_one_core': max(s['client_cpu_percent_one_core'] for s in samples),
                'latency_sample_storage_mib': round(len(rtt_values) * 8 / 1048576, 3),
            }
        save_json(output / 'summary.json', state)
        save_json(output / 'status.json', state)
        print(json.dumps({'status': state['status'], 'summary': str(output / 'summary.json'),
                          'connections': len(connections), 'hold_seconds': round(held_seconds, 3),
                          'probe_rtt': state['probe_rtt'], 'error': state.get('error', '')}), flush=True)
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--connections', type=int, default=200)
    parser.add_argument('--hold-seconds', type=float, default=3600)
    parser.add_argument('--connect-concurrency', type=int, default=5)
    parser.add_argument('--probe-interval', type=float, default=5)
    parser.add_argument('--server', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if min(args.connections, args.hold_seconds, args.connect_concurrency, args.probe_interval) <= 0:
        parser.error('All numeric settings must be positive')
    logging.basicConfig(level=logging.ERROR)
    if args.server:
        asyncio.run(mock_server())
    else:
        result = asyncio.run(benchmark(args))
        raise SystemExit(0 if result['status'] == 'passed' else 1)


if __name__ == '__main__':
    main()
