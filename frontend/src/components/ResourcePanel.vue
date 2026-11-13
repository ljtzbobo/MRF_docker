<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart, LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { RefreshCw, Server, Box } from 'lucide-vue-next'
import { useWorkbench } from '../stores/workbench'
import ResourceOverview from './ResourceOverview.vue'
import DynamicPanel from './DynamicPanel.vue'
import { api } from '../api'

echarts.use([BarChart, LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])
const store = useWorkbench()
const simulated = computed(() => store.resources?.execution_mode === 'software_simulation')
const chartElement = ref<HTMLDivElement | null>(null)
const trendElement = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null
let trend: echarts.ECharts | null = null
let timer: ReturnType<typeof setInterval> | null = null
let observer: ResizeObserver | null = null
const releasing = ref('')
const releaseError = ref('')
const idleWarm = computed(() => store.resources?.containers.filter(item => item.state === 'Warm' && item.phase === 'READY' && !item.queue_depth) ?? [])
async function checkpointAction(id: string, remove = false) {
  releasing.value = id
  try {
    await (remove ? api.removeCheckpoint(id) : api.checkpointInstance(id))
    await store.refreshResources()
  } catch (error) { store.resourceError = error instanceof Error ? error.message : '快照操作失败' }
  finally { releasing.value = '' }
}
async function releaseIdleWarm() {
  releaseError.value = ''
  releasing.value = 'all'
  try { for (const item of idleWarm.value) await api.releasePoolInstance(item.container_unit_id) }
  catch (error) { releaseError.value = error instanceof Error ? error.message : '空闲实例释放失败' }
  finally { await store.refreshResources(); releasing.value = '' }
}
async function releaseInstance(id: string) {
  releasing.value = id
  try { await api.releasePoolInstance(id); await store.refreshResources() }
  catch (error) { store.resourceError = error instanceof Error ? error.message : '实例释放失败' }
  finally { releasing.value = '' }
}

const chartItems = computed(() => (store.resources?.nodes ?? []).flatMap((node) => [
  { name: `${node.node_id} · CPU`, reserved: node.reserved.cpu_millicores, capacity: node.capacity.cpu_millicores, unit: 'mCPU' },
  { name: `${node.node_id} · 内存`, reserved: node.reserved.memory_bytes / 2 ** 20, capacity: node.capacity.memory_bytes / 2 ** 20, unit: 'MiB' },
  { name: `${node.node_id} · 带宽`, reserved: node.reserved.bandwidth_bytes_per_s / 2 ** 20, capacity: node.capacity.bandwidth_bytes_per_s / 2 ** 20, unit: 'MiB/s' },
  { name: `${node.node_id} · DSP 槽`, reserved: node.reserved.dsp_units, capacity: node.capacity.dsp_units, unit: '旧版单元' },
  { name: `${node.node_id} · FPGA 槽`, reserved: node.reserved.fpga_units, capacity: node.capacity.fpga_units, unit: '旧版单元' },
  { name: `${node.node_id} · 计算`, reserved: node.reserved.dsp_compute_mcycles_per_s ?? 0, capacity: node.capacity.dsp_compute_mcycles_per_s ?? 0, unit: '百万周期/秒' },
  { name: `${node.node_id} · 本地 L2`, reserved: (node.reserved.dsp_local_memory_bytes ?? 0) / 2 ** 20, capacity: (node.capacity.dsp_local_memory_bytes ?? 0) / 2 ** 20, unit: 'MiB' },
  { name: `${node.node_id} · LUT`, reserved: node.reserved.fpga_lut ?? 0, capacity: node.capacity.fpga_lut ?? 0, unit: 'LUT' },
  { name: `${node.node_id} · FF`, reserved: node.reserved.fpga_ff ?? 0, capacity: node.capacity.fpga_ff ?? 0, unit: 'FF' },
  { name: `${node.node_id} · DSP48`, reserved: node.reserved.fpga_dsp48 ?? 0, capacity: node.capacity.fpga_dsp48 ?? 0, unit: 'DSP48E1' },
  { name: `${node.node_id} · BRAM`, reserved: node.reserved.fpga_bram36 ?? 0, capacity: node.capacity.fpga_bram36 ?? 0, unit: '36Kb 块' },
]).filter((item) => item.capacity > 0))
const bandwidthUnmeasured = computed(() => store.resources?.nodes.some(
  (node) => node.capacity.bandwidth_bytes_per_s === 0) ?? false)

function draw() {
  if (!chartElement.value || !chartElement.value.clientWidth || !chartItems.value.length) return
  chart ??= echarts.init(chartElement.value)
  const rows = chartItems.value
  chart.setOption({
    animationDuration: 350,
    grid: { left: 148, right: 22, top: 16, bottom: 26 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: (params: unknown) => {
        const entry = (params as Array<{ dataIndex: number }>)[0]
        const row = rows[entry.dataIndex]
        return `${row.name}<br/>已预留 ${row.reserved.toFixed(1)} ${row.unit}<br/>容量 ${row.capacity.toFixed(1)} ${row.unit}`
      } },
    xAxis: { type: 'value', max: 100, axisLabel: { formatter: '{value}%' }, splitLine: { lineStyle: { color: '#e9eeee' } } },
    yAxis: { type: 'category', inverse: true, data: rows.map((item) => item.name), axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: '#405755', width: 138, overflow: 'truncate' } },
    series: [
      { type: 'bar', name: '已预留', stack: 'usage', barWidth: 13, itemStyle: { color: '#16837b', borderRadius: [3, 0, 0, 3] }, data: rows.map((item) => item.capacity ? Math.min(100, item.reserved / item.capacity * 100) : 0) },
      { type: 'bar', name: '可用', stack: 'usage', barWidth: 13, itemStyle: { color: '#e3ebe8', borderRadius: [0, 3, 3, 0] }, data: rows.map((item) => item.capacity ? Math.max(0, 100 - item.reserved / item.capacity * 100) : 0) },
    ],
  })
}

async function openMetricDetails() { await nextTick(); draw(); chart?.resize() }

function drawTrend() {
  if (!trendElement.value || !store.resourceHistory.length) return
  trend ??= echarts.init(trendElement.value)
  trend.setOption({
    animationDuration: 300,
    grid: { left: 55, right: 52, top: 38, bottom: 32 },
    tooltip: { trigger: 'axis' },
    legend: { top: 7, right: 12, textStyle: { color: '#607972', fontSize: 10 } },
    xAxis: { type: 'category', boundaryGap: false, data: store.resourceHistory.map((item) => item.time),
      axisLabel: { color: '#859a93', fontSize: 10 }, axisLine: { lineStyle: { color: '#dce7e1' } } },
    yAxis: [
      { type: 'value', min: 0, axisLabel: { color: '#859a93', fontSize: 10 }, splitLine: { lineStyle: { color: '#eef2ef' } } },
      { type: 'value', min: 0, axisLabel: { color: '#859a93', fontSize: 10 }, splitLine: { show: false } },
    ],
    series: [
      { type: 'line', name: 'CPU (%)', data: store.resourceHistory.map((item) => item.cpuPercent),
        showSymbol: true, symbolSize: 5, smooth: true, lineStyle: { color: '#16837b', width: 2 }, itemStyle: { color: '#16837b' } },
      { type: 'line', name: '内存 (MiB)', yAxisIndex: 1, data: store.resourceHistory.map((item) => item.memoryMiB),
        showSymbol: true, symbolSize: 5, smooth: true, lineStyle: { color: '#ca8f42', width: 2 }, itemStyle: { color: '#ca8f42' } },
    ],
  })
}

watch(chartItems, async () => { await nextTick(); draw() }, { deep: true })
watch(() => store.resourceHistory, async () => { await nextTick(); drawTrend() }, { deep: true })
onMounted(() => {
  store.refreshResources()
  timer = setInterval(() => store.refreshResources(), 4000)
  observer = new ResizeObserver(() => { chart?.resize(); trend?.resize() })
  if (chartElement.value) observer.observe(chartElement.value)
  if (trendElement.value) observer.observe(trendElement.value)
})
onBeforeUnmount(() => { if (timer) clearInterval(timer); observer?.disconnect(); chart?.dispose(); trend?.dispose() })
</script>

<template>
  <section class="resources-view">
    <div class="section-heading">
      <div><div class="eyebrow">DEPLOYMENT / RESOURCES</div><h1>{{ simulated ? '异构资源仿真' : '容器资源' }}</h1></div>
      <div class="resource-actions"><span v-if="store.resourceUpdatedAt" class="muted">{{ store.resourceUpdatedAt.toLocaleTimeString('zh-CN') }} 更新</span><button class="icon-button" title="刷新资源" aria-label="刷新资源" @click="store.refreshResources"><RefreshCw :size="18" /></button></div>
    </div>
    <div v-if="store.resourceError" class="notice notice--error">{{ store.resourceError }}</div>
    <div v-if="releaseError" class="notice notice--error">{{ releaseError }}</div>
    <div v-if="simulated" class="notice">软件仿真模式：DSP、FPGA 容量与耗时采用模型参数，数据处理使用 CPU 参考实现；运行单元为进程内实例。</div>
    <div v-if="store.resources && !store.resources.configured" class="resource-offline"><Server :size="24" /><strong>尚未配置物理部署</strong><span>设置 MRF_DEPLOYMENT_CONFIG 后显示处理节点和容器资源。</span></div>
    <template v-else-if="store.resources?.configured">
      <div v-if="store.resources.idle_container_pool_supported" class="resource-actions"><span class="muted">空闲 Warm {{ idleWarm.length }} 个；切换任务会保留以供复用</span><button class="secondary-button" :disabled="!idleWarm.length || Boolean(releasing) || store.executing" @click="releaseIdleWarm">{{ releasing === 'all' ? '释放中…' : '释放空闲 Warm' }}</button></div>
      <ResourceOverview :resources="store.resources" />
      <DynamicPanel />
      <section v-if="store.resources.pool" class="resource-band">
        <div class="band-heading"><h2>快照与后端接管</h2><span class="muted">{{ store.resources.pool.persistence?.enabled ? '持久化已启用 · 单进程独占管理' : '持久化未启用' }}</span></div>
        <p class="resource-note">{{ store.resources.pool.checkpoint_supported ? '空闲 Warm 可保存为真实 CRIU 快照；确认容器停止后释放运行预算，后续任务通过资源规划选择恢复。' : '当前后端未配置真实 Docker 快照；软件仿真不会生成进程快照。' }}</p>
        <p v-if="store.resources.pool.persistence?.error" class="notice notice--error">{{ store.resources.pool.persistence.error }}</p>
        <div v-if="store.resources.pool.checkpoint_supported" class="resource-actions"><button v-for="item in idleWarm" :key="item.container_unit_id" class="secondary-button" :disabled="Boolean(releasing)" @click="checkpointAction(item.container_unit_id)">保存 {{ item.cid }} 快照</button></div>
        <div v-if="store.resources.pool.checkpoints?.length" class="table-scroll"><table><thead><tr><th>快照</th><th>组件</th><th>处理节点</th><th>状态</th><th>操作</th></tr></thead><tbody><tr v-for="cp in store.resources.pool.checkpoints" :key="cp.id"><td :title="cp.error || cp.id" class="mono truncate">{{ cp.id }}</td><td>{{ cp.configuration.cid }}</td><td>{{ cp.configuration.processing_node_id }}</td><td>{{ cp.status }}</td><td><button v-if="['available','invalid'].includes(cp.status)" class="secondary-button" :disabled="Boolean(releasing)" @click="checkpointAction(cp.id,true)">移除</button></td></tr></tbody></table></div>
        <details v-if="store.resources.pool.persistence?.enabled"><summary>查看重启接管结果</summary><pre>{{ JSON.stringify(store.resources.pool.persistence.recovery,null,2) }}</pre></details>
      </section>
      <section v-if="store.resources.pool?.pending_tasks?.length" class="resource-band"><div class="band-heading"><h2>等待复用的任务</h2><span class="muted">整批排队 · 尚未新增资源预留</span></div><div class="table-scroll"><table><thead><tr><th>等待任务</th><th>前序任务</th><th>目标实例</th><th>已等待</th><th>剩余等待预算</th></tr></thead><tbody><tr v-for="task in store.resources.pool.pending_tasks" :key="task.task_id"><td>{{ task.task_id }}</td><td>{{ task.owner_task_id }}</td><td>{{ task.instance_ids.length }}</td><td>{{ task.waited_ms.toFixed(1) }} ms</td><td>{{ task.remaining_queue_budget_ms.toFixed(1) }} ms</td></tr></tbody></table></div></section>
      <details v-if="store.resources.pool?.action_costs?.length" class="resource-band"><summary>实例动作的实际耗时记录</summary><p class="resource-note">统计当前后端近期最多 64 次成功动作的墙钟时间；画像估算和实际记录分别展示，不作为硬件性能标定。</p><div class="table-scroll"><table><thead><tr><th>实现</th><th>处理位置</th><th>动作</th><th>样本数</th><th>平均</th><th>最大</th><th>最近一次</th></tr></thead><tbody><tr v-for="(cost,i) in store.resources.pool.action_costs" :key="i"><td>{{ cost.implementation_id }}</td><td>{{ cost.processing_node_id }}</td><td>{{ cost.action }}</td><td>{{ cost.sample_count }}</td><td>{{ cost.mean_ms.toFixed(3) }} ms</td><td>{{ cost.max_ms.toFixed(3) }} ms</td><td>{{ cost.latest_ms.toFixed(3) }} ms</td></tr></tbody></table></div></details>
      <details class="resource-band" @toggle="openMetricDetails"><summary>查看全部容量与预留指标</summary><div v-if="chartItems.length" ref="chartElement" class="resource-chart" :style="{ height: Math.max(215, chartItems.length * 35 + 55) + 'px' }"></div><div v-else class="empty-line">暂无处理节点</div><p v-if="bandwidthUnmeasured" class="resource-note">带宽容量尚未测量，图中仅显示已知容量。</p></details>
      <section v-if="!simulated" class="resource-band"><div class="band-heading"><h2>实际使用趋势</h2><span class="muted">本页采样 · 最近 60 次</span></div><div ref="trendElement" class="resource-chart resource-trend"></div></section>
      <section class="resource-band"><div class="band-heading"><h2>{{ simulated ? '仿真实例' : '容器实例' }}</h2><span class="muted">{{ simulated ? '状态每 4 秒刷新' : 'Docker 实际统计每 4 秒刷新' }}</span></div>
        <div v-if="!store.resources?.containers.length" class="empty-line"><Box :size="18" />{{ simulated ? '暂无活动仿真实例' : '暂无活动容器' }}</div>
        <div v-else class="table-scroll"><table><thead><tr><th>运行单元</th><th>CID</th><th>处理节点</th><th>生命周期 / 阶段</th><th>复用次数</th><th>实际 CPU</th><th>实际内存</th><th>操作</th></tr></thead><tbody><tr v-for="item in store.resources.containers" :key="item.container_unit_id"><td :title="item.container_unit_id" class="mono truncate">{{ item.container_unit_id }}</td><td class="mono">{{ item.cid }}</td><td>{{ item.processing_node_id }}</td><td>{{ item.state }} / {{ item.phase ?? item.actual?.status ?? '—' }}{{ item.queue_depth ? ' · 等待 ' + item.queue_depth + ' 个任务' : '' }}</td><td>{{ item.reuse_count ?? 0 }}</td><td>{{ item.actual?.cpu_percent ?? '—' }}</td><td>{{ item.actual?.memory_usage ?? '—' }}</td><td><button v-if="item.state === 'Warm' && item.phase === 'READY' && !item.queue_depth" class="secondary-button" :disabled="Boolean(releasing)" @click="releaseInstance(item.container_unit_id)">{{ releasing === item.container_unit_id ? '释放中' : '释放' }}</button></td></tr></tbody></table></div>
      </section>
      <details v-if="store.resources.pool" class="resource-band"><summary>组件到实例、实例到资源的两层映射</summary>
        <p class="resource-note">任务完成后解除第一层绑定；Warm 实例保留第二层资源绑定。</p>
        <div class="table-scroll"><table><thead><tr><th>任务</th><th>组件节点</th><th>实例</th></tr></thead><tbody><tr v-for="binding in store.resources.pool.component_bindings" :key="binding.lease_id"><td>{{ binding.task_id }}</td><td>{{ binding.node_id }}</td><td>{{ binding.instance_id }}</td></tr></tbody></table></div>
        <div class="table-scroll"><table><thead><tr><th>实例</th><th>处理资源</th><th>核心 / 区域</th></tr></thead><tbody><tr v-for="binding in store.resources.pool.resource_bindings" :key="binding.instance_id"><td>{{ binding.instance_id }}</td><td>{{ binding.processing_node_id }}</td><td>{{ binding.core_id ?? binding.region_id ?? '—' }}</td></tr></tbody></table></div>
      </details>
    </template>
  </section>
</template>
