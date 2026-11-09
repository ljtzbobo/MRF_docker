import type { Edge, Node } from '@vue-flow/core'
import type { GraphData, GraphLevel } from './types'

export function layoutGraph(graph: GraphData, level: GraphLevel): { nodes: Node[]; edges: Edge[] } {
  const incoming = new Map(graph.nodes.map((node) => [node.id, 0]))
  const successors = new Map(graph.nodes.map((node) => [node.id, [] as string[]]))
  for (const edge of graph.edges) {
    incoming.set(edge.target, (incoming.get(edge.target) ?? 0) + 1)
    successors.get(edge.source)?.push(edge.target)
  }
  const depth = new Map<string, number>()
  const queue = graph.nodes.filter((node) => incoming.get(node.id) === 0).map((node) => node.id)
  for (const id of queue) depth.set(id, 0)
  for (let index = 0; index < queue.length; index += 1) {
    const id = queue[index]
    for (const next of successors.get(id) ?? []) {
      depth.set(next, Math.max(depth.get(next) ?? 0, (depth.get(id) ?? 0) + 1))
      incoming.set(next, (incoming.get(next) ?? 1) - 1)
      if (incoming.get(next) === 0) queue.push(next)
    }
  }
  const columns = new Map<number, string[]>()
  for (const node of graph.nodes) {
    const column = depth.get(node.id) ?? 0
    columns.set(column, [...(columns.get(column) ?? []), node.id])
  }
  const positions = new Map<string, { x: number; y: number }>()
  for (const [column, ids] of columns) {
    ids.forEach((id, row) => positions.set(id, {
      x: column * 300,
      y: row * 154 - ((ids.length - 1) * 154) / 2,
    }))
  }
  return {
    nodes: graph.nodes.map((node) => ({
      id: node.id,
      type: 'task',
      position: positions.get(node.id) ?? { x: 0, y: 0 },
      data: { ...node, level },
      draggable: false,
    })),
    edges: graph.edges.map((edge, index) => ({
      id: `${edge.source}:${edge.source_port ?? 'out'}-${edge.target}:${edge.target_port ?? 'in'}-${index}`,
      source: edge.source,
      target: edge.target,
      type: 'smoothstep',
      label: edge.format ?? (edge.source_port ? `${edge.source_port} → ${edge.target_port}` : ''),
      markerEnd: 'arrowclosed',
      style: { stroke: '#83a6a3', strokeWidth: 1.7 },
      labelStyle: { fill: '#54716c', fontSize: 10 },
      labelBgStyle: { fill: '#f7faf9', fillOpacity: 0.94 },
    })),
  }
}
