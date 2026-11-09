import type { DeploymentResult, InputFrame, MatchResult, RecordValue, Representation, ResourceSnapshot } from './types'

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
  represent: (task: RecordValue | RecordValue[]) => request<Representation>('/task-representations/external', task),
  representNormalized: (task: RecordValue) => request<Representation>('/task-representations/normalized', task),
  restoreTemplate: (taskTemplate: RecordValue, taskId: string) =>
    request<Representation>('/task-representations/from-template',
      { task_template: taskTemplate, task_id: taskId }),
  match: (task: RecordValue) => request<MatchResult>('/matches', task),
  plan: (task: RecordValue) => request<RecordValue>('/plans', task),
  resources: () => request<ResourceSnapshot>('/deployments/resources'),
  execute: (task: RecordValue, frames: InputFrame[]) =>
    request<DeploymentResult>('/deployments/execute', { task, frames }),
}
