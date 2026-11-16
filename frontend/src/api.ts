import type { DatasetRecord, DatasetScenario, DeploymentResult, ExecutionProgress, InputFrame, MatchResult, RecordValue, Representation, ResourceSnapshot } from './types'

async function request<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = (payload as RecordValue).detail
    throw new Error(typeof detail === 'string' ? detail : `请求失败 (${response.status})`)
  }
  return payload as T
}

export const api = {
  datasets: () => request<{ records: DatasetRecord[]; warnings: string[] }>('/datasets'),
  datasetScenarios: () => request<DatasetScenario[]>('/datasets/scenarios'),
  generateDatasetTask: (scenarioId: string, recordId: string, experiment: RecordValue) => request<{task: RecordValue; limitations: string[]; compatibility: {match_status: string}}>('/datasets/tasks',{scenario_id:scenarioId,record_id:recordId,experiment}),
  startDatasetExecution: (task: RecordValue, keepWarm = false) => request<ExecutionProgress>('/datasets/executions',{task,keep_warm:keepWarm}),
  startExecution: (task: RecordValue, frames: InputFrame[], keepWarm = false) => request<ExecutionProgress>('/executions', { task, frames, keep_warm: keepWarm }),
  executionProgress: (id: string) => request<ExecutionProgress>(`/executions/${id}`),
  executionResult: (id: string) => request<DeploymentResult>(`/executions/${id}/result`),
  represent: (task: RecordValue | RecordValue[]) => request<Representation>('/task-representations/external', task),
  representNormalized: (task: RecordValue) => request<Representation>('/task-representations/normalized', task),
  restoreTemplate: (taskTemplate: RecordValue, taskId: string) =>
    request<Representation>('/task-representations/from-template',
      { task_template: taskTemplate, task_id: taskId }),
  match: (task: RecordValue) => request<MatchResult>('/matches', task),
  plan: (task: RecordValue, keepWarm = false) => request<RecordValue>(`/plans?keep_warm=${keepWarm}`, task),
  resources: () => request<ResourceSnapshot>('/deployments/resources'),
  dynamicState: () => request<RecordValue>('/dynamic'),
  dynamicEvent: (event: RecordValue) => request<RecordValue>('/dynamic/events', event),
  releasePoolInstance: (id: string) => request<RecordValue>(`/pool/instances/${encodeURIComponent(id)}/release`, {}),
  checkpointInstance: (id: string) => request<RecordValue>(`/pool/instances/${encodeURIComponent(id)}/checkpoint`, {}),
  removeCheckpoint: (id: string) => request<RecordValue>(`/pool/checkpoints/${encodeURIComponent(id)}/remove`, {}),
  execute: (task: RecordValue, frames: InputFrame[]) =>
    request<DeploymentResult>('/deployments/execute', { task, frames }),
}
