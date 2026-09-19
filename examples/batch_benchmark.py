"""Compare actual MCP round trips on temporary data; no model or live tracker data.
Run: .venv/bin/python -m examples.batch_benchmark
"""
import asyncio
import json
import os
from pathlib import Path
from statistics import median
import sys
import tempfile
from time import perf_counter

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from tracker.db import Database
from tracker.service import Tracker


async def benchmark(path):
    tracker = Tracker(Database(str(path)), 'benchmark', 'benchmark')
    collection = tracker.create_collection('tasks', 'One task')
    records = [tracker.create_record(collection['id'], f'Task {i}', {}) for i in range(4)]
    params = StdioServerParameters(command=sys.executable, args=['-m', 'tracker.server'],
        env={**os.environ, 'TRACKER_DB_PATH': str(path), 'TRACKER_WORKSPACE_ID': 'benchmark',
             'TRACKER_ACTOR_ID': 'benchmark'})
    async with Client(params, read_timeout_seconds=10) as client:
        async def call(name, **arguments):
            response = await client.call_tool(name, arguments)
            assert not response.is_error, response
            assert response.structured_content['ok'], response
            return response.structured_content['result']
        requests = [dict(operation='search_records', query=f'Task {i}', limit=10) for i in range(3)]
        requests += [dict(operation='get_record_context', record_id=r['id'], event_limit=0) for r in records]
        await call('batch_read', requests=requests)  # warm connection, exclude startup
        single_times, batch_times = [], []
        for _ in range(10):
            start = perf_counter()
            singles = [await call(r['operation'], **{k: v for k, v in r.items() if k != 'operation'}) for r in requests]
            single_times.append((perf_counter()-start)*1000)
            start = perf_counter()
            batch = await call('batch_read', requests=requests)
            batch_times.append((perf_counter()-start)*1000)
            assert singles == [item['result'] for item in batch['results']]
        versions = [1]*3
        write_single, write_batch = [], []
        for iteration in range(5):
            start = perf_counter()
            for i, r in enumerate(records[:3]):
                review = await call('resolve_record', query=r['id'])
                prepared = await call('prepare_write', operation='update_record',
                    arguments=dict(record_id=r['id'], expected_version=versions[i], changes={'iteration': iteration}),
                    review_ids=[review['resolution_id']], decision_reason='Benchmark requested update')
                await call('commit_write', action_id=prepared['action_id'])
                versions[i] += 1
            write_single.append((perf_counter()-start)*1000)
            start = perf_counter()
            reviews = await call('batch_resolve_records', requests=[dict(query=r['id']) for r in records[:3]])
            actions = [dict(operation='update_record',
                arguments=dict(record_id=r['id'], expected_version=versions[i], changes={'iteration': iteration}),
                review_ids=[reviews['results'][i]['result']['resolution_id']], decision_reason='Benchmark requested update')
                for i, r in enumerate(records[:3])]
            prepared = await call('prepare_batch_write', actions=actions)
            await call('commit_batch_write', action_id=prepared['action_id'])
            write_batch.append((perf_counter()-start)*1000)
            versions = [v+1 for v in versions]
        assert all(tracker.get_record_context(r['id'])['record']['version'] == v for r, v in zip(records, versions))
        return {'scope': 'Warm local MCP stdio; temporary 4-record fixture; excludes model reasoning and startup',
                'read': {'single_calls': 7, 'batch_calls': 1, 'single_median_ms': round(median(single_times), 2),
                         'batch_median_ms': round(median(batch_times), 2), 'identical_results': True},
                'three_updates': {'single_calls': 9, 'batch_calls': 3, 'single_median_ms': round(median(write_single), 2),
                                  'batch_median_ms': round(median(write_batch), 2)}}


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='tracker-batch-') as directory:
        print(json.dumps(asyncio.run(benchmark(Path(directory) / 'benchmark.db')), indent=2))
