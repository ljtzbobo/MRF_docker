<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useRoute, useRouter } from 'vue-router'
import { Activity, ArrowRight, Box, CheckCircle2, ChevronDown, CircleAlert, Cpu, FileJson2, Layers3, Network, Play, Upload } from 'lucide-vue-next'
import GraphPanel from './components/GraphPanel.vue'
import DetailValue from './components/DetailValue.vue'
import { api } from './api'
import { spectrumSample } from './sample'
import { useWorkbench } from './stores/workbench'
import type { GraphLevel, InputFrame, RecordValue, Representation } from './types'

const store = useWorkbench()
const route = useRoute()
const router = useRouter()
const ResourcePanel = defineAsyncComponent(() => import('./components/ResourcePanel.vue'))
const { graph, selectedNode } = storeToRefs(store)
const view = computed(() => route.name === 'resources' ? 'resources' : 'task')
const fileInput = ref<HTMLInputElement | null>(null)
const frameInput = ref<HTMLInputElement | null>(null)
const dragOver = ref(false)
const uploadError = ref('')
type TaskOption = { taskId: string; source: RecordValue; represented: Representation }
const taskOptions = ref<TaskOption[]>([])
const selectedTaskId = ref('')
const uploadedTaskName = ref('')
type TemplateOption = { key: string; label: string; template: RecordValue; taskId: string }
const templateOptions = ref<TemplateOption[]>([])
const uploadedTemplateName = ref('')
const selectedVariantKey = ref('')

function optionsForTemplate(template: RecordValue, group: string): TemplateOption[] {
  const variants = template.variants
  if (!variants || typeof variants !== 'object' || Array.isArray(variants)) return []
  return Object.keys(variants).map((taskId) => ({
    key: `${group}:${taskId}`, label: group ? `${group} · ${taskId}` : taskId, template, taskId,
  }))
}
const frameError = ref('')
const sourceNodes = computed(() => store.match?.execution_graph?.boundaries
  ?.filter((boundary) => boundary.direction === 'source').map((boundary) => String(boundary.node_id)) ?? [])
const executionHint = computed(() => {
  if (!store.resources?.configured) return '尚未配置容器部署'
  if (store.match?.status !== 'MATCHED') return '需要完整匹配的组件执行图'
  if (store.plan?.status !== 'FEASIBLE_ESTIMATE') return '资源分配尚不可行'
  if (!store.frames.length) return '等待输入帧'
  return `${store.frames.length} 帧已就绪`
})
const stages: Array<{ id: GraphLevel; label: string; number: string }> = [
  { id: 'business', label: '业务功能 G', number: '01' },
  { id: 'specification', label: '规格 DAG', number: '02' },
  { id: 'component', label: '组件执行图', number: '03' },
]
const graphCount = computed(() => `${graph.value?.nodes.length ?? 0} 节点 · ${graph.value?.edges.length ?? 0} 连线`)
const statusText = computed(() => {
  if (store.busy) return '处理中'
  if (store.error) return '解析失败'
  if (store.match?.status === 'MATCHED') return '已匹配'
  if (store.match?.status === 'PARTIAL') return '部分匹配'
  if (store.match?.status === 'NEEDS_REFINEMENT') return '待细化'
  return store.representation ? '已表征' : '待上传'
})
const issues = computed(() => store.match?.unresolved ?? [])
const abstractTask = computed(() => store.representation?.task_representation ?? store.representation?.task_representations?.[0])
const taskType = computed(() => String(abstractTask.value?.S?.task_type ?? store.task?.task_type ?? '—'))
const taskDirection = computed(() => String(abstractTask.value?.S?.direction ?? store.task?.direction ?? '—').toUpperCase())
const representationParts = computed(() => abstractTask.value ? [
  { key: 'S', label: '语义 S', data: abstractTask.value.S },
  { key: 'Q', label: '服务质量 Q', data: abstractTask.value.Q },
  { key: 'R', label: '资源 R', data: abstractTask.value.R },
  { key: 'X', label: '状态 X', data: abstractTask.value.X },
] : [])

function formatValue(value: unknown): string {
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return JSON.stringify(value)
}

async function loadFile(file?: File) {
  if (!file || store.executing) return
  uploadError.value = ''
  if (!file.name.toLowerCase().endsWith('.json') || file.size > 8 * 1024 * 1024) {
    uploadError.value = '请选择不超过 8 MiB 的 JSON 文件'
    return
  }
  try {
    const data: unknown = JSON.parse(await file.text())
    const sources = Array.isArray(data) ? data
      : data && typeof data === 'object' && Array.isArray((data as RecordValue).tasks)
        ? (data as RecordValue).tasks as unknown[] : null
    if (sources) {
      const result = await api.represent(data as RecordValue | RecordValue[])
      const representedTasks = result.task_representations
      if (!representedTasks || representedTasks.length !== sources.length) {
        throw new Error('任务合集未生成完整的逐任务表征')
      }
      taskOptions.value = sources.map((source, index) => {
        const normalized = representedTasks[index]
        return { taskId: normalized.task_id, source: source as RecordValue,
          represented: { task_representation: normalized, status: result.status } }
      })
      templateOptions.value = []
      uploadedTaskName.value = file.name
      selectedTaskId.value = taskOptions.value[0].taskId
      await loadSelectedTask()
      if (fileInput.value) fileInput.value.value = ''
      return
    }
    if (!data || typeof data !== 'object') throw new Error('任务 JSON 必须是对象或任务数组')
    const document = data as RecordValue
    const template = (document.task_template && typeof document.task_template === 'object'
      ? document.task_template : document) as RecordValue
    const grouped = document.task_templates
    const options = grouped && typeof grouped === 'object' && !Array.isArray(grouped)
      ? Object.entries(grouped).flatMap(([family, item]) => optionsForTemplate(item as RecordValue, family))
      : optionsForTemplate(template, '')
    if (options.length) {
      taskOptions.value = []
      templateOptions.value = options
      uploadedTemplateName.value = file.name
      const preferredId = (document.task_representation as RecordValue | undefined)?.task_id
      selectedVariantKey.value = options.find((item) => item.taskId === preferredId)?.key ?? options[0].key
      if (options.length === 1) await loadSelectedVariant()
    } else if (document.schema_version === 'external_rf_task.v1' ||
               document.schema_version === 'rf_task_representation.v1') {
      taskOptions.value = []
      templateOptions.value = []
      await router.push('/tasks')
      await store.analyze(document, file.name)
    } else {
      throw new Error('无法识别任务文件；请上传原始业务请求或包含 tasks 的任务合集')
    }
  } catch (error) {
    uploadError.value = error instanceof Error ? error.message : '文件读取失败'
  }
  if (fileInput.value) fileInput.value.value = ''
}

async function loadSelectedTask() {
  const selected = taskOptions.value.find((item) => item.taskId === selectedTaskId.value)
  if (!selected) return
  uploadError.value = ''
  await router.push('/tasks')
  await store.analyze(selected.source, `${uploadedTaskName.value} · ${selected.taskId}`, selected.represented)
}

async function loadSelectedVariant() {
  const selected = templateOptions.value.find((item) => item.key === selectedVariantKey.value)
  if (!selected) return
  uploadError.value = ''
  try {
    const result = await api.restoreTemplate(selected.template, selected.taskId)
    if (!result.task_representation) throw new Error('模板未恢复出任务表征')
    await router.push('/tasks')
    await store.analyze(result.task_representation, `${uploadedTemplateName.value} · ${selected.taskId}`, result)
  } catch (error) {
    uploadError.value = error instanceof Error ? error.message : '任务变体恢复失败'
  }
}

function onDrop(event: DragEvent) {
  dragOver.value = false
  if (store.executing) return
  loadFile(event.dataTransfer?.files[0])
}

function validateFrames(value: unknown): InputFrame[] {
  if (!Array.isArray(value) || !value.length || !sourceNodes.value.length) {
    throw new Error('输入帧必须是非空 JSON 数组，且任务已完整匹配')
  }
  const required = [...sourceNodes.value].sort().join('|')
  const packetFields = ['dtype', 'shape', 'data_base64', 'sample_rate',
    'center_frequency', 'timestamp', 'domain', 'metadata']
  for (const [index, frame] of value.entries()) {
    if (!frame || typeof frame !== 'object' || Array.isArray(frame) ||
        Object.keys(frame).sort().join('|') !== required) {
      throw new Error(`第 ${index + 1} 帧的入口节点必须为 ${sourceNodes.value.join('、')}`)
    }
    for (const packet of Object.values(frame)) {
      if (!packet || typeof packet !== 'object' || Array.isArray(packet) ||
          packetFields.some((field) => !(field in packet)) ||
          typeof (packet as RecordValue).data_base64 !== 'string' ||
          !Array.isArray((packet as RecordValue).shape)) {
        throw new Error(`第 ${index + 1} 帧的数据包格式不完整`)
      }
    }
  }
  return value as InputFrame[]
}

async function loadFrames(file?: File) {
  if (!file) return
  frameError.value = ''
  try {
    if (!file.name.toLowerCase().endsWith('.json') || file.size > 8 * 1024 * 1024) {
      throw new Error('请选择不超过 8 MiB 的输入帧 JSON')
    }
    store.setFrames(validateFrames(JSON.parse(await file.text())), file.name)
  } catch (error) {
    store.setFrames([], '')
    frameError.value = error instanceof Error ? error.message : '输入帧读取失败'
  }
  if (frameInput.value) frameInput.value.value = ''
}

async function loadReceiveSample() {
  frameError.value = ''
  taskOptions.value = []
  templateOptions.value = []
  try {
    const [taskResponse, frameResponse] = await Promise.all([
      fetch('/samples/data-link-rx-task.json'), fetch('/samples/data-link-rx-frames.json'),
    ])
    if (!taskResponse.ok || !frameResponse.ok) throw new Error('接收链样例读取失败')
    const task = await taskResponse.json() as RecordValue
    const frames: unknown = await frameResponse.json()
    await router.push('/tasks')
    await store.analyze(task, '数据链接收样例')
    if (store.match?.status === 'MATCHED') {
      store.setFrames(validateFrames(frames), '接收链演示帧.json')
    }
  } catch (error) {
    frameError.value = error instanceof Error ? error.message : '样例读取失败'
  }
}

onMounted(() => store.analyze(spectrumSample, '频谱分析样例'))
</script>

<template>
  <div class="shell">
    <aside class="sidebar">
      <div class="brand"><span class="brand-mark"><Activity :size="21" :stroke-width="2.4" /></span><div><strong>MRF</strong><span>任务工作台</span></div></div>
      <nav class="main-nav" aria-label="主导航">
        <RouterLink to="/tasks" :class="{ active: view === 'task' }"><Network :size="18" />任务拆解</RouterLink>
        <RouterLink to="/resources" :class="{ active: view === 'resources' }"><Cpu :size="18" />容器资源</RouterLink>
      </nav>
      <div class="sidebar-bottom"><span class="status-dot" :class="store.resourceError ? '' : 'status-dot--ok'"></span>MRF 控制台</div>
    </aside>

    <main class="workspace">
      <header class="topbar"><div class="breadcrumb">工作台 <span>/</span> {{ view === 'task' ? '任务拆解' : '容器资源' }}</div><div class="topbar-right"><span class="topbar-label">射频任务编排</span><span class="topbar-avatar">MRF</span></div></header>
      <ResourcePanel v-if="view === 'resources'" />
      <div v-else class="task-view">
        <div class="section-heading"><div><div class="eyebrow">TASK / REPRESENTATION</div><h1>任务拆解</h1></div><span class="task-status" :class="{ 'task-status--warn': store.error || issues.length }"><span class="status-dot" :class="{ 'status-dot--ok': store.match?.status === 'MATCHED' }"></span>{{ statusText }}</span></div>

        <section class="upload-band" :class="{ 'upload-band--drag': dragOver }" @dragover.prevent="dragOver = true" @dragleave.prevent="dragOver = false" @drop.prevent="onDrop">
          <div class="upload-icon"><FileJson2 :size="21" /></div>
          <div class="upload-copy"><strong>仿真任务</strong><span>{{ store.fileName || '上传原始业务请求或任务合集 JSON' }}</span></div>
          <div class="upload-actions"><button class="secondary-button" :disabled="store.executing" @click="taskOptions = []; templateOptions = []; store.analyze(spectrumSample, '频谱分析样例')">载入样例</button><button class="primary-button" :disabled="store.executing" @click="fileInput?.click()"><Upload :size="16" />上传任务</button></div>
          <input ref="fileInput" class="sr-only" type="file" accept=".json,application/json" @change="loadFile(($event.target as HTMLInputElement).files?.[0])" />
        </section>
        <div v-if="taskOptions.length > 1" class="template-selection"><label for="task-id">选择仿真任务</label><select id="task-id" v-model="selectedTaskId" :disabled="store.busy || store.executing" @change="loadSelectedTask"><option v-for="option in taskOptions" :key="option.taskId" :value="option.taskId">{{ option.taskId }}</option></select></div>
        <div v-if="templateOptions.length > 1" class="template-selection"><label for="variant-id">选择任务变体</label><select id="variant-id" v-model="selectedVariantKey"><option v-for="option in templateOptions" :key="option.key" :value="option.key">{{ option.label }}</option></select><button class="primary-button" :disabled="store.executing" @click="loadSelectedVariant">恢复任务</button></div>
        <div v-if="uploadError || store.error" class="notice notice--error"><CircleAlert :size="16" />{{ uploadError || store.error }}</div>

        <div class="task-meta"><div><span>任务 ID</span><strong class="mono">{{ store.taskId }}</strong></div><div><span>任务类型</span><strong>{{ taskType }}</strong></div><div><span>方向</span><strong>{{ taskDirection }}</strong></div><div><span>分配预览</span><strong :class="{ 'positive': store.plan?.status === 'FEASIBLE_ESTIMATE' }">{{ store.plan?.status ?? '—' }}</strong></div></div>

        <section class="execution-band" aria-label="容器执行">
          <div class="execution-heading"><div><h2>容器执行</h2><span>{{ executionHint }}</span></div><div class="execution-actions"><button class="secondary-button" :disabled="store.busy || store.executing" @click="loadReceiveSample">载入接收链样例</button><button class="secondary-button" :disabled="store.match?.status !== 'MATCHED' || store.executing" @click="frameInput?.click()"><Upload :size="15" />上传输入帧</button><button class="primary-button" :disabled="!store.canExecute" @click="store.execute()"><Play :size="15" />{{ store.executing ? '执行中…' : '提交执行' }}</button></div></div>
          <input ref="frameInput" class="sr-only" type="file" accept=".json,application/json" aria-label="输入帧 JSON" @change="loadFrames(($event.target as HTMLInputElement).files?.[0])" />
          <div v-if="store.frameFileName" class="execution-file"><FileJson2 :size="15" /><span>{{ store.frameFileName }}</span><span>{{ store.frames.length }} 帧</span></div>
          <div v-if="frameError || store.executionError" class="notice notice--error"><CircleAlert :size="16" />{{ frameError || store.executionError }}</div>
          <div v-if="store.deployment" class="execution-result"><CheckCircle2 :size="17" /><strong>{{ store.deployment.status }}</strong><span>{{ store.deployment.frame_outputs.length }} 帧完成</span><span>任务 {{ store.deployment.task_id }}</span></div>
        </section>

        <section class="analysis-section">
          <div class="analysis-heading"><div><h2>功能链路</h2><span>{{ graphCount }}</span></div><div class="segmented" role="tablist" aria-label="图层"><button v-for="stage in stages" :key="stage.id" role="tab" :aria-selected="store.level === stage.id" :class="{ active: store.level === stage.id }" @click="store.setLevel(stage.id)">{{ stage.label }}</button></div></div>
          <div class="analysis-layout">
            <div class="graph-column"><div class="graph-topline"><div class="stage-trail"><template v-for="(stage, index) in stages" :key="stage.id"><span :class="{ current: store.level === stage.id }">{{ stage.number }} {{ stage.label }}</span><ArrowRight v-if="index < stages.length - 1" :size="13" /></template></div><span class="muted">{{ store.match?.refined_graph?.profile_id ?? '业务请求' }}</span></div><GraphPanel :graph="graph" :level="store.level" :selected-id="store.selectedNodeId" @select="store.selectedNodeId = $event" /></div>
            <aside class="inspector"><div class="inspector-title"><Layers3 :size="17" /><h3>{{ selectedNode ? '节点详情' : '图层详情' }}</h3></div>
              <template v-if="selectedNode"><div class="inspector-id mono">{{ selectedNode.id }}</div><div class="detail-row"><span>功能</span><strong>{{ selectedNode.capability ?? selectedNode.role ?? '—' }}</strong></div><div v-if="selectedNode.cid" class="detail-row"><span>组件 CID</span><strong class="mono">{{ selectedNode.cid }}</strong></div><div v-if="selectedNode.business_node_id" class="detail-row"><span>业务节点</span><strong class="mono">{{ selectedNode.business_node_id }}</strong></div><div v-if="selectedNode.inputs" class="detail-block"><span>输入端口</span><div v-for="(format, port) in selectedNode.inputs" :key="port" class="port-row"><b>{{ port }}</b><code>{{ format }}</code></div></div><div v-if="selectedNode.outputs" class="detail-block"><span>输出端口</span><div v-for="(format, port) in selectedNode.outputs" :key="port" class="port-row"><b>{{ port }}</b><code>{{ format }}</code></div></div><div v-if="selectedNode.parameters && Object.keys(selectedNode.parameters).length" class="detail-block"><span>参数</span><div v-for="(value, key) in selectedNode.parameters" :key="key" class="parameter-row"><b>{{ key }}</b><span :title="formatValue(value)">{{ formatValue(value) }}</span></div></div></template>
              <template v-else><div class="inspector-placeholder"><Box :size="25" :stroke-width="1.4" /><span>选择图中的节点查看契约与参数</span></div><div class="detail-row"><span>节点数量</span><strong>{{ graph?.nodes.length ?? 0 }}</strong></div><div class="detail-row"><span>连接数量</span><strong>{{ graph?.edges.length ?? 0 }}</strong></div><div v-if="store.match?.refined_graph?.uncovered_outputs?.length" class="detail-block"><span>未覆盖输出</span><p v-for="output in store.match.refined_graph.uncovered_outputs" :key="output">{{ output }}</p></div><div v-if="store.level === 'business'" class="representation-parts"><details v-for="part in representationParts" :key="part.key"><summary>{{ part.label }}<ChevronDown :size="14" /></summary><DetailValue :value="part.data" :resource="part.key === 'R'" /><div v-if="part.key === 'R' && store.plan?.unit_system === 'physical' && store.plan.node_demand" class="physical-preview"><h4>处理节点预留</h4><DetailValue :value="store.plan.node_demand" resource /></div></details></div></template>
            </aside>
          </div>
        </section>
        <section v-if="issues.length" class="issues-section"><div class="band-heading"><h2>待处理项</h2><span>{{ issues.length }}</span></div><div v-for="(issue, index) in issues" :key="index" class="issue-row"><CircleAlert :size="17" /><span class="mono">{{ issue.code }}</span><p>{{ issue.detail }}</p><span v-if="issue.node_id" class="mono">{{ issue.node_id }}</span></div></section>
        <section v-if="store.plan?.issues && (store.plan.issues as string[]).length" class="issues-section"><div class="band-heading"><h2>分配诊断</h2><ChevronDown :size="17" /></div><div v-for="(issue, index) in store.plan.issues as string[]" :key="index" class="issue-row"><CircleAlert :size="17" /><p>{{ issue }}</p></div></section>
      </div>
    </main>
  </div>
</template>
