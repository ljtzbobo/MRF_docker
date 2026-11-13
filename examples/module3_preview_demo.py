"""Submit an existing synthetic interface fixture up to module 3, without execution."""
import argparse
import json
from pathlib import Path
from urllib.request import Request, urlopen

from backend.repositories import JsonDocumentRepository
from backend.services import TaskService
from deployment.config import load_deployment_manager
from deployment.protocol import encode_packet
from examples.heterogeneous_simulation_demo import CONFIG, ROOT, sample_frame, sample_task


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-base', help='Optional running API root, e.g. http://127.0.0.1:8010')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'outputs/module3-preview')
    args = parser.parse_args()
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    request, task, _ = sample_task()
    frames = [{key: encode_packet(packet) for key, packet in sample_frame(task).items()}]

    def save(name, value):
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    save('input-task.json', request)
    save('input-frames.json', frames)
    save('input-manifest.json', dict(origin='synthetic_interface_test',
        source='examples/BusinessRequests/External/07_spectrum_analysis.json',
        generator='examples.heterogeneous_simulation_demo.sample_frame',
        request_adjustment='Explicit deadline_ms=arrival_ms+1000 in this copy only',
        frames=1, samples_per_frame=4096, sample_rate_hz=4000000,
        note='Not a real dataset. Module 3 uses task structure and profiles, not IQ sample values.'))

    if args.api_base:
        base = args.api_base.rstrip('/')
        def call(path, body=None):
            encoded = None if body is None else json.dumps(body).encode('utf-8')
            req = Request(base + '/api/v1' + path, encoded,
                          {'Content-Type': 'application/json'})
            with urlopen(req, timeout=30) as response:
                return json.load(response)
        before = call('/deployments/resources')
        if before.get('execution_mode') != 'software_simulation':
            raise RuntimeError('Start the API with the reference simulation configuration first')
        representation = call('/task-representations/external', request)
        match = call('/matches', request)
        plan = call('/plans', request)
        after = call('/deployments/resources')
        entry = 'HTTP API: ' + base
    else:
        manager = load_deployment_manager(CONFIG)
        service = TaskService(JsonDocumentRepository(ROOT / 'examples/TaskDemos'), deployment=manager)
        before = service.deployment_resources()
        representation = service.represent_external(request)
        match = service.match(request)
        plan = service.plan(request)
        after = service.deployment_resources()
        entry = 'TaskService (same application service used by HTTP API)'

    save('task-representation.json', representation)
    save('component-match.json', match)
    save('module3-plan.json', plan)
    save('resources-before.json', before)
    save('resources-after.json', after)
    summary = dict(entry=entry, input_origin='synthetic_interface_test',
        task_id=plan.get('task_id'), match_status=match.get('status'),
        status=plan.get('status'), algorithm=plan.get('algorithm'),
        profile_basis=plan.get('profile_basis'), frame_count=plan.get('frame_count'),
        estimated_completion_ms=plan.get('estimated_completion_ms'),
        time_limit_ms=plan.get('time_limit_ms'), optimality_proven=plan.get('optimality_proven'),
        explored=plan.get('explored'), allocations=plan.get('allocations'),
        resources_unchanged=before == after, execution_submitted=False)
    save('summary.json', summary)
    if match.get('status') != 'MATCHED' or plan.get('status') != 'FEASIBLE_ESTIMATE':
        raise RuntimeError('Preview did not succeed; inspect saved diagnostics')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print('Output directory: ' + str(output))


if __name__ == '__main__':
    main()
