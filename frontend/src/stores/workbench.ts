import { defineStore } from 'pinia'
import { api } from '../api'
import type { DeploymentResult, ExecutionProgress, GraphData, GraphLevel, GraphNode, InputFrame, MatchResult, RecordValue, Representation, ResourceSnapshot } from '../types'

function memoryMiB(value: string | undefined): number {
  const match = value?.split('/')[0].trim().match(/^([\d.]+)\s*(B|kB|MB|GB|KiB|MiB|GiB)$/i)
  if (!match) return 0
  const factors: Record<string, number> = { b: 1 / 2 ** 20, kb: 1000 / 2 ** 20,
    mb: 1000000 / 2 ** 20, gb: 1000000000 / 2 ** 20, kib: 1 / 1024,
    mib: 1, gib: 1024 }
  return Number(match[1]) * factors[match[2].toLowerCase()]
}

export const useWorkbench = defineStore('workbench', {
  state: () => ({
    task: null as RecordValue | null,
    fileName: '',
    representation: null as Representation | null,
    match: null as MatchResult | null,
    plan: null as RecordValue | null,
    frames: [] as InputFrame[],
    frameFileName: '',
    deployment: null as DeploymentResult | null,
    executing: false,
    keepWarm: true,
    executionError: '',
    executionProgress: null as ExecutionProgress | null,
    parallelProgress: [] as ExecutionProgress[],
    resources: null as ResourceSnapshot | null,
    level: 'business' as GraphLevel,
    selectedNodeId: null as string | null,
    busy: false,
    error: '',
    resourceError: '',
    resourceUpdatedAt: null as Date | null,
    resourceHistory: [] as Array<{ time: string; cpuPercent: number; memoryMiB: number }>,
  }),
  getters: {
    taskId: (state) => String(state.task?.task_id ?? '未选择任务'),
    graph(state): GraphData | null {
      if (state.level === 'business') {
        return state.representation?.task_representation?.G ?? state.representation?.task_representations?.[0]?.G ?? null
      }
      if (state.level === 'specification') return state.match?.refined_graph ?? null
      return state.match?.execution_graph ?? null
    },
    selectedNode(): GraphNode | null {
      return this.graph?.nodes.find((node) => node.id === this.selectedNodeId) ?? null
    },
    canExecute: (state) => Boolean(state.resources?.configured && !state.resourceError &&
      state.match?.status === 'MATCHED' &&
      state.plan?.status === 'FEASIBLE_ESTIMATE' && state.frames.length && !state.busy && !state.executing),
  },
  actions: {
    async refreshPlan() {
      if (!this.task || this.match?.status !== 'MATCHED') return
      this.busy = true
      try { this.plan = await api.plan(this.task, this.keepWarm) }
      catch (error) { this.error = error instanceof Error ? error.message : '方案预览失败' }
      finally { this.busy = false }
    },
    async analyze(task: RecordValue, fileName: string, represented?: Representation) {
      this.task = task
      this.fileName = fileName
      this.representation = null
      this.match = null
      this.plan = null
      this.frames = []
      this.frameFileName = ''
      this.deployment = null
      this.executionError = ''
      this.executionProgress = null
      this.parallelProgress = []
      this.level = 'business'
      this.selectedNodeId = null
      this.error = ''
      this.busy = true
      try {
        this.representation = represented ?? (task.schema_version === 'rf_task_representation.v1'
          ? await api.representNormalized(task) : await api.represent(task))
        this.match = await api.match(task)
        if (this.match.status === 'MATCHED') {
          this.plan = await api.plan(task, this.keepWarm)
        }
        await this.refreshResources()
      } catch (error) {
        this.error = error instanceof Error ? error.message : '任务解析失败'
      } finally {
        this.busy = false
      }
    },
    setFrames(frames: InputFrame[], fileName: string) {
      this.frames = frames
      this.frameFileName = fileName
      this.deployment = null
      this.executionError = ''
      this.executionProgress = null
      this.parallelProgress = []
    },
    async execute() {
      if (!this.canExecute || !this.task) return
      this.executing = true
      this.executionError = ''
      this.deployment = null
      this.parallelProgress = []
      try {
        this.executionProgress = await api.startExecution(this.task, this.frames, this.keepWarm)
        this.level = 'component'
        while (this.executionProgress.state === 'QUEUED' || this.executionProgress.state === 'RUNNING') {
          await new Promise(resolve => setTimeout(resolve, 250))
          this.executionProgress = await api.executionProgress(this.executionProgress.job_id)
          if (this.executionProgress.plan) this.plan = this.executionProgress.plan
        }
        if (this.executionProgress.state === 'FAILED') throw new Error(this.executionProgress.error || '任务执行失败')
        this.deployment = await api.executionResult(this.executionProgress.job_id)
      } catch (error) {
        this.executionError = error instanceof Error ? error.message : '容器执行失败'
      } finally {
        this.executing = false
        await this.refreshResources()
      }
    },
    async executePair() {
      if (!this.canExecute || !this.task || !this.keepWarm) return
      this.executing = true
      this.executionError = ''
      this.deployment = null
      this.executionProgress = null
      this.parallelProgress = []
      this.level = 'component'
      const errors: string[] = []
      try {
        const suffix = Date.now()
        const submissions = await Promise.allSettled(['a', 'b'].map(label => api.startExecution(
          { ...this.task!, task_id: `${this.task!.task_id}-parallel-${label}-${suffix}` }, this.frames, true)))
        for (const result of submissions) {
          if (result.status === 'fulfilled') this.parallelProgress.push(result.value)
          else errors.push(result.reason instanceof Error ? result.reason.message : '并发提交失败')
        }
        while (this.parallelProgress.some(job => job.state === 'QUEUED' || job.state === 'RUNNING')) {
          await new Promise(resolve => setTimeout(resolve, 250))
          this.parallelProgress = await Promise.all(this.parallelProgress.map(job => api.executionProgress(job.job_id)))
          this.executionProgress = this.parallelProgress[this.parallelProgress.length - 1] ?? null
          if (this.executionProgress?.plan) this.plan = this.executionProgress.plan
        }
        for (const job of this.parallelProgress) if (job.state === 'FAILED') errors.push(`${job.task_id}: ${job.error}`)
        const last = this.parallelProgress[this.parallelProgress.length - 1]
        if (last?.state === 'COMPLETED') this.deployment = await api.executionResult(last.job_id)
        this.executionError = errors.join('；')
      } catch (error) {
        this.executionError = error instanceof Error ? error.message : '并发执行查询失败'
      } finally {
        this.executing = false
        await this.refreshResources()
      }
    },
    setLevel(level: GraphLevel) {
      this.level = level
      this.selectedNodeId = null
    },
    async refreshResources() {
      try {
        this.resources = await api.resources()
        this.resourceError = ''
        this.resourceUpdatedAt = new Date()
        if (this.resources.configured && this.resources.execution_mode !== 'software_simulation') {
          const running = this.resources.containers.filter((item) => item.actual?.status === 'RUNNING')
          this.resourceHistory.push({
            time: this.resourceUpdatedAt.toLocaleTimeString('zh-CN', { hour12: false }),
            cpuPercent: running.reduce((total, item) => total + (parseFloat(item.actual?.cpu_percent ?? '') || 0), 0),
            memoryMiB: running.reduce((total, item) => total + memoryMiB(item.actual?.memory_usage), 0),
          })
          this.resourceHistory = this.resourceHistory.slice(-60)
        }
      } catch (error) {
        this.resourceError = error instanceof Error ? error.message : '资源状态读取失败'
      }
    },
  },
})
