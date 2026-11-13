"""FastAPI entry point. Services can be replaced in tests or later deployments."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse

from task_repr import ValidationError
from scheduler import SchedulingError
from deployment import DeploymentError
from deployment.config import load_deployment_manager
from deployment.docker_runtime import DockerRuntimeError
from deployment.protocol import decode_packet, encode_packet

from ..repositories import DocumentNotFound, InvalidDocument, JsonDocumentRepository
from ..services import ResourceService, TaskService
from ..services.executions import ExecutionJobs


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def create_app(task_service: TaskService | None = None,
               resource_service: ResourceService | None = None) -> FastAPI:
    if task_service is None:
        deployment_config = os.environ.get("MRF_DEPLOYMENT_CONFIG")
        deployment = load_deployment_manager(deployment_config) if deployment_config else None
        task_service = TaskService(JsonDocumentRepository(
            os.environ.get("MRF_TASK_DIR", PROJECT_ROOT / "examples" / "TaskDemos")),
            deployment=deployment)
    resource_service = resource_service or ResourceService(JsonDocumentRepository(
        os.environ.get("MRF_RESOURCE_DIR", PROJECT_ROOT / "examples" / "ResourceDemos")))
    if task_service.deployment is not None and os.environ.get('MRF_POOL_STATE'):
        if task_service.deployment.pool.journal is None:
            task_service.deployment.pool.configure_persistence(os.environ['MRF_POOL_STATE'])
    execution_jobs = ExecutionJobs(task_service)
    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            execution_jobs.close()
            if task_service.deployment is not None:
                task_service.deployment.pool.shutdown()
    app = FastAPI(title="MRF Experiment API", version="0.1.0", lifespan=lifespan)
    app.state.execution_jobs = execution_jobs

    @app.exception_handler(DocumentNotFound)
    async def document_not_found(_request, exc: DocumentNotFound):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(InvalidDocument)
    @app.exception_handler(ValidationError)
    async def invalid_document(_request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(SchedulingError)
    @app.exception_handler(DeploymentError)
    async def scheduling_error(_request, exc: SchedulingError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(DockerRuntimeError)
    async def docker_error(_request, exc: DockerRuntimeError):
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/v1/tasks")
    def list_tasks():
        return {"items": task_service.list_names()}

    @app.post("/api/v1/tasks/validate")
    def validate_task(document: dict):
        return task_service.validate(document)

    @app.post("/api/v1/task-representations/external")
    def represent_external(request: dict | list = Body(...)):
        return task_service.represent_external(request)

    @app.post("/api/v1/task-representations/normalized")
    def represent_normalized(document: dict):
        return task_service.represent_normalized(document)

    @app.get("/api/v1/task-representations/template")
    def download_task_template():
        return FileResponse(PROJECT_ROOT / "task_repr" / "parsing" / "data" /
                            "unified_task_template.json",
                            filename="unified_task_template.json",
                            media_type="application/json")

    @app.post("/api/v1/task-representations/from-template")
    def represent_template_variant(payload: dict):
        if (set(payload) != {"task_template", "task_id"} or
                not isinstance(payload["task_template"], dict) or
                not isinstance(payload["task_id"], str)):
            raise ValidationError("task_template and task_id are required")
        return task_service.represent_template_variant(payload["task_template"], payload["task_id"])

    @app.get("/api/v1/tasks/{name}")
    def get_task(name: str):
        return task_service.get(name)

    @app.post("/api/v1/tasks/{name}/match")
    def match_saved_task(name: str):
        return task_service.match(task_service.get(name))

    @app.post("/api/v1/matches")
    def match_submitted_task(document: dict):
        return task_service.match(document)

    @app.post("/api/v1/plans")
    def plan_submitted_task(document: dict, keep_warm: bool = False):
        return task_service.plan(document, **({'keep_warm': True} if keep_warm else {}))

    @app.post("/api/v1/deployments/execute")
    def execute_deployed(payload: dict):
        if task_service.deployment is None:
            raise HTTPException(status_code=503, detail="Docker deployment is not configured")
        if (not {'task', 'frames'} <= set(payload) or set(payload) - {'task','frames','keep_warm'}
                or not isinstance(payload["frames"], list) or type(payload.get('keep_warm', False)) is not bool):
            raise ValidationError("deployment requires task and frames array")
        try:
            frames = [{node_id: decode_packet(packet) for node_id, packet in frame.items()}
                      for frame in payload["frames"]]
        except (TypeError, AttributeError, ValueError) as exc:
            raise ValidationError(f"invalid deployment frame: {exc}") from exc
        result = task_service.execute_deployed(payload["task"], frames,
            **({'keep_warm': True} if payload.get('keep_warm') else {}))
        return execution_result(result)

    def execution_result(result):
        return {"task_id": result.task_id, "status": "COMPLETED",
                "execution_mode": result.execution_mode,
                "plan": result.plan.to_dict(), "execution_graph": result.execution_graph,
                "frame_outputs": [{node: encode_packet(packet) for node, packet in outputs.items()}
                                  for outputs in result.frame_outputs]}

    @app.post('/api/v1/executions', status_code=202)
    def start_execution(payload: dict):
        if task_service.deployment is None:
            raise HTTPException(status_code=503, detail='Execution environment is not configured')
        if (not {'task','frames'} <= set(payload) or set(payload) - {'task','frames','keep_warm'}
                or type(payload.get('keep_warm', False)) is not bool or not isinstance(payload['task'],dict) or
                not isinstance(payload['frames'],list) or not payload['frames']):
            raise ValidationError('execution requires a task and nonempty frames array')
        try:
            frames = [{key: decode_packet(value) for key,value in frame.items()} for frame in payload['frames']]
        except (TypeError, AttributeError, ValueError) as exc:
            raise ValidationError(f'invalid execution frame: {exc}') from exc
        try:
            return execution_jobs.submit(payload['task'], frames, keep_warm=payload.get('keep_warm', False))
        except RuntimeError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc

    @app.get('/api/v1/executions/{job_id}')
    def execution_progress(job_id: str):
        try:
            return execution_jobs.snapshot(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail='Execution job not found')

    @app.get('/api/v1/executions/{job_id}/result')
    def completed_execution(job_id: str):
        progress = execution_progress(job_id)
        if progress['state'] != 'COMPLETED':
            raise HTTPException(status_code=409, detail=progress['error'] or 'Execution is not complete')
        return execution_result(execution_jobs.result(job_id))

    @app.get("/api/v1/deployments/resources")
    def deployment_resources():
        return task_service.deployment_resources()

    def configured_pool():
        if task_service.deployment is None:
            raise HTTPException(status_code=503, detail='Execution environment is not configured')
        return task_service.deployment.pool

    @app.get('/api/v1/dynamic')
    def dynamic_state():
        configured_pool()
        return task_service.deployment.dynamic.snapshot()

    @app.post('/api/v1/dynamic/events')
    def dynamic_event(payload: dict):
        configured_pool()
        if 'event' not in payload:
            raise ValidationError('event is required')
        try:
            return task_service.deployment.dynamic.emit(payload['event'],**{k:v for k,v in payload.items() if k!='event'})
        except SchedulingError:
            raise
        except (ValueError,TypeError) as exc:
            raise ValidationError(str(exc)) from exc

    @app.get('/api/v1/pool')
    def pool_state():
        return configured_pool().snapshot()

    @app.post('/api/v1/pool/instances/{instance_id}/checkpoint')
    def save_checkpoint(instance_id: str):
        return configured_pool().checkpoint(instance_id)

    @app.post('/api/v1/pool/checkpoints/{checkpoint_id}/remove')
    def remove_checkpoint(checkpoint_id: str):
        pool=configured_pool()
        if pool.checkpoints is None:raise SchedulingError('checkpoint store is not configured')
        pool.checkpoints.remove(checkpoint_id)
        return pool.snapshot()

    @app.post('/api/v1/pool/maintenance')
    def pool_maintenance():
        pool = configured_pool()
        pool.reap_expired()
        pool.trim()
        return pool.snapshot()

    @app.post('/api/v1/pool/instances/{instance_id}/release')
    def release_pool_instance(instance_id: str):
        pool = configured_pool()
        pool.evict(instance_id)
        return pool.snapshot()

    @app.post('/api/v1/pool/tasks/{task_id}/retry-cleanup')
    def retry_pool_cleanup(task_id: str):
        pool = configured_pool()
        pool.retry_cleanup(task_id)
        return pool.snapshot()

    @app.post("/api/v1/simulations/schedule")
    def simulate_schedule(document: dict):
        return task_service.simulate_schedule(document)

    @app.post("/api/v1/simulations/tasks/{task_id}/complete")
    def complete_simulation(task_id: str):
        return task_service.complete_simulation(task_id)

    @app.get("/api/v1/simulations/state")
    def simulation_state():
        return task_service.simulation_state()

    @app.get("/api/v1/resources")
    def list_resources():
        return {"items": resource_service.list_names()}

    @app.get("/api/v1/resources/{name}")
    def get_resource(name: str):
        return resource_service.get(name)

    return app


app = create_app()
