export type RecordValue = Record<string, unknown>
export type GraphLevel = 'business' | 'specification' | 'component'
export type InputFrame = Record<string, RecordValue>

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
}

export interface ResourceSnapshot {
  configured: boolean
  nodes: Array<{ node_id: string; hardware_id: string; capacity: ResourceUsage; reserved: ResourceUsage }>
  active_tasks: string[]
  containers: Array<{
    container_unit_id: string
    cid: string
    processing_node_id: string
    state: string
    task_id: string | null
    actual: null | { status: string; cpu_percent?: string; memory_usage?: string; network_io?: string; error?: string }
  }>
}
