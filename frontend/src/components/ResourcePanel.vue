<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart, LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { RefreshCw, Server, Box } from 'lucide-vue-next'
import { useWorkbench } from '../stores/workbench'

echarts.use([BarChart, LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])
const store = useWorkbench()
const chartElement = ref<HTMLDivElement | null>(null)
const trendElement = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null
let trend: echarts.ECharts | null = null
let timer: ReturnType<typeof setInterval> | null = null
let observer: ResizeObserver | null = null

const chartItems = computed(() => (store.resources?.nodes ?? []).flatMap((node) => [
  { name: `${node.node_id} · CPU`, reserved: node.reserved.cpu_millicores, capacity: node.capacity.cpu_millicores, unit: 'mCPU' },
  { name: `${node.node_id} · 内存`, reserved: node.reserved.memory_bytes / 2 ** 20, capacity: node.capacity.memory_bytes / 2 ** 20, unit: 'MiB' },
  { name: `${node.node_id} · 带宽`, reserved: node.reserved.bandwidth_bytes_per_s / 2 ** 20, capacity: node.capacity.bandwidth_bytes_per_s / 2 ** 20, unit: 'MiB/s' },
]).filter((item) => item.capacity > 0))
const bandwidthUnmeasured = computed(() => store.resources?.nodes.some(
  (node) => node.capacity.bandwidth_bytes_per_s === 0) ?? false)

function draw() {
  if (!chartElement.value || !chartItems.value.length) return
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
      <div><div class="eyebrow">DEPLOYMENT / RESOURCES</div><h1>容器资源</h1></div>
      <div class="resource-actions"><span v-if="store.resourceUpdatedAt" class="muted">{{ store.resourceUpdatedAt.toLocaleTimeString('zh-CN') }} 更新</span><button class="icon-button" title="刷新资源" aria-label="刷新资源" @click="store.refreshResources"><RefreshCw :size="18" /></button></div>
    </div>
    <div v-if="store.resourceError" class="notice notice--error">{{ store.resourceError }}</div>
    <div v-if="store.resources && !store.resources.configured" class="resource-offline"><Server :size="24" /><strong>尚未配置物理部署</strong><span>设置 MRF_DEPLOYMENT_CONFIG 后显示处理节点和容器资源。</span></div>
    <template v-else>
      <div class="resource-summary">
        <div><span>处理节点</span><strong>{{ store.resources?.nodes.length ?? '—' }}</strong></div>
        <div><span>运行任务</span><strong>{{ store.resources?.active_tasks.length ?? '—' }}</strong></div>
        <div><span>活动容器</span><strong>{{ store.resources?.containers.length ?? '—' }}</strong></div>
      </div>
      <section class="resource-band"><div class="band-heading"><h2>物理容量与预留</h2><span class="muted">调度器数据</span></div><div v-if="chartItems.length" ref="chartElement" class="resource-chart" :style="{ height: Math.max(215, chartItems.length * 52 + 55) + 'px' }"></div><div v-else class="empty-line">暂无处理节点</div><p v-if="bandwidthUnmeasured" class="resource-note">带宽容量尚未测量，图中仅显示已知容量。</p></section>
      <section class="resource-band"><div class="band-heading"><h2>实际使用趋势</h2><span class="muted">本页采样 · 最近 60 次</span></div><div ref="trendElement" class="resource-chart resource-trend"></div></section>
      <section class="resource-band"><div class="band-heading"><h2>容器实例</h2><span class="muted">Docker 实际统计每 4 秒刷新</span></div>
        <div v-if="!store.resources?.containers.length" class="empty-line"><Box :size="18" />暂无活动容器</div>
        <div v-else class="table-scroll"><table><thead><tr><th>运行单元</th><th>CID</th><th>处理节点</th><th>状态</th><th>实际 CPU</th><th>实际内存</th><th>网络 I/O</th></tr></thead><tbody><tr v-for="item in store.resources.containers" :key="item.container_unit_id"><td :title="item.container_unit_id" class="mono truncate">{{ item.container_unit_id }}</td><td class="mono">{{ item.cid }}</td><td>{{ item.processing_node_id }}</td><td><span class="status-dot" :class="item.actual?.status === 'RUNNING' ? 'status-dot--ok' : ''"></span>{{ item.actual?.status ?? item.state }}</td><td>{{ item.actual?.cpu_percent ?? '—' }}</td><td>{{ item.actual?.memory_usage ?? '—' }}</td><td>{{ item.actual?.network_io ?? '—' }}</td></tr></tbody></table></div>
      </section>
    </template>
  </section>
</template>
