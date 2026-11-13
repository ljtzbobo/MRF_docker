export type RecordValue = Record<string, unknown>
export type GraphLevel = 'business' | 'specification' | 'component'
export type InputFrame = Record<string, RecordValue>

export interface ExecutionProgress {
  job_id: string; task_id: string; state: 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED'
  stage: string; total_frames: number; completed_frames: number
  completed_calls: number; total_calls: number; current_node: string | null
  nodes: Record<string, { state: string; completed_frames: number }>
  events: Array<{ event: string; at: number; node_id?: string; frame_index?: number; error?: string }>
  plan: RecordValue | null; error: string | null; resources_released: boolean
  waiting_ms?: number
}

export interface DeploymentResult {
  task_id: string
  status: string
  plan: RecordValue
  execution_graph: GraphData
  frame_outputs: Array<Record<string, { shape: number[]; dtype: string; metadata: RecordValue }>>
}

export interface GraphNode extends RecordValue {
  id: string
  role?: string
  capability?: string
  cid?: string
  inputs?: Record<string, string>
  outputs?: Record<string, string>
  parameters?: RecordValue
}

export interface GraphEdge extends RecordValue {
  source: string
  target: string
  source_port?: string
  target_port?: string
  format?: string
}

export interface GraphData extends RecordValue {
  nodes: GraphNode[]
  edges: GraphEdge[]
  boundaries?: RecordValue[]
  topological_order?: string[]
}

export interface Representation {
  task_representation?: { task_id: string; G: GraphData; S: RecordValue; Q: RecordValue; R: RecordValue; X: RecordValue }
  task_representations?: Array<{ task_id: string; G: GraphData; S: RecordValue; Q: RecordValue; R: RecordValue; X: RecordValue }>
  task_template?: RecordValue
  task_templates?: Record<string, RecordValue>
  status: string
}

export interface MatchResult {
  task_id: string
  status: string
  refined_graph?: GraphData & { profile_id?: string; prerequisites?: string[]; uncovered_outputs?: string[] }
  execution_graph?: GraphData
  unresolved: Array<{ code: string; detail: string; node_id?: string }>
  bindings: Record<string, { cid: string; parameters: RecordValue }>
}

export interface ResourceUsage {
  cpu_millicores: number
  memory_bytes: number
  bandwidth_bytes_per_s: number
  dsp_units: number
  fpga_units: number
  dsp_compute_mcycles_per_s?: number
  dsp_local_memory_bytes?: number
  fpga_lut?: number
  fpga_ff?: number
  fpga_dsp48?: number
  fpga_bram36?: number
}

export interface ResourceSnapshot {
  configured: boolean
  execution_mode?: 'docker' | 'software_simulation'
  nodes: Array<{ node_id: string; hardware_id: string; device_id?: string | null; processor?: string | null; unit_index?: number | null; capacity: ResourceUsage; reserved: ResourceUsage; available?: ResourceUsage; reserved_breakdown?: { background: ResourceUsage; active: ResourceUsage; warm: ResourceUsage } }>
  component_templates?: Array<{
    cid: string; name: string; creation_status: string; image: string | null
    image_verified: boolean; execution_backend: string
    implementations: Array<{ implementation_id: string; processor: string; processing_nodes: string[] }>
  }>
  idle_container_pool_supported?: boolean
  pool?: {
    scope: string; max_warm: number; idle_timeout_s: number
    checkpoint_supported?: boolean
    checkpoints?: Array<{ id: string; unit_id: string; status: string; error: string | null; configuration: { cid: string; processing_node_id: string } }>
    persistence?: { enabled: boolean; error: string | null; recovery: RecordValue }
    component_bindings: Array<{ task_id: string; node_id: string; instance_id: string; lease_id: string }>
    resource_bindings: Array<{ instance_id: string; processing_node_id: string; core_id: number | null; region_id: number | null }>
    pending_tasks?: Array<{ task_id: string; owner_task_id: string; instance_ids: string[]; waited_ms: number; remaining_queue_budget_ms: number }>
    action_costs?: Array<{ implementation_id: string; processing_node_id: string; action: string; sample_count: number; max_ms: number; mean_ms: number; latest_ms: number }>
  }
  active_tasks: string[]
  containers: Array<{
    container_unit_id: string
    cid: string
    processing_node_id: string
    state: string
    phase?: string
    reuse_count?: number
    queue_depth?: number
    task_id: string | null
    actual: null | { status: string; cpu_percent?: string; memory_usage?: string; network_io?: string; error?: string }
  }>
}
