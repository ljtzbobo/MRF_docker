<script setup lang="ts">
import { computed } from 'vue'
import { Cpu, CircuitBoard, Layers3, Box } from 'lucide-vue-next'
import type { ResourceSnapshot, ResourceUsage } from '../types'

const props = defineProps<{ resources: ResourceSnapshot }>()
type Node = ResourceSnapshot['nodes'][number]
type Metric = { key: keyof ResourceUsage; label: string; unit: string; scale?: number }
const simulated = computed(() => props.resources.execution_mode === 'software_simulation')
const kind = (n: Node) => n.processor || ((n.capacity.dsp_compute_mcycles_per_s ?? 0) > 0 || n.capacity.dsp_units > 0 ? 'dsp' : (n.capacity.fpga_lut ?? 0) > 0 || n.capacity.fpga_units > 0 ? 'fpga' : 'cpu')
const groups = computed(() => [
  { key: 'cpu', name: 'CPU 与主机内存', icon: Cpu, nodes: props.resources.nodes.filter(n => kind(n) === 'cpu'), metrics: [
    { key: 'cpu_millicores', label: 'CPU 时间份额', unit: '核', scale: 1000 },
    { key: 'memory_bytes', label: '内存', unit: 'GiB', scale: 2 ** 30 },
  ] as Metric[] },
  { key: 'dsp', name: 'DSP · C66x 核心', icon: CircuitBoard, nodes: props.resources.nodes.filter(n => kind(n) === 'dsp'), metrics: [
    { key: 'dsp_compute_mcycles_per_s', label: '计算预算', unit: '百万周期/秒' },
    { key: 'dsp_local_memory_bytes', label: '本地 L2', unit: 'KiB', scale: 1024 },
    { key: 'dsp_units', label: '处理单元（旧模型）', unit: '单元' },
  ] as Metric[] },
  { key: 'fpga', name: 'FPGA · XC7K410T 区域', icon: Layers3, nodes: props.resources.nodes.filter(n => kind(n) === 'fpga'), metrics: [
    { key: 'fpga_lut', label: 'LUT', unit: '个' }, { key: 'fpga_ff', label: 'FF', unit: '个' },
    { key: 'fpga_dsp48', label: 'DSP48E1', unit: '个' }, { key: 'fpga_bram36', label: 'BRAM36', unit: '块' },
    { key: 'fpga_units', label: '区域槽（旧模型）', unit: '单元' },
  ] as Metric[] },
])
const templates = computed(() => props.resources.component_templates ?? [])
const active = computed(() => props.resources.containers.filter(c => c.state.toUpperCase() === 'ACTIVE').length)
const warm = computed(() => props.resources.containers.filter(c => c.state.toUpperCase() === 'WARM').length)
function total(n: Node, m: Metric) { return n.capacity[m.key] ?? 0 }
function used(n: Node, m: Metric) { return n.reserved[m.key] ?? 0 }
function available(n: Node, m: Metric) { return n.available?.[m.key] ?? total(n,m) - used(n,m) }
function format(value: number, m: Metric) { return (value / (m.scale ?? 1)).toLocaleString('zh-CN', { maximumFractionDigits: 2 }) }
const creationLabel: Record<string,string> = { SOFTWARE_REFERENCE: '参考实现已配置', IMAGE_CONFIGURED: '镜像已配置，待执行检查', NEEDS_CALIBRATION: '缺少执行画像', MISSING_IMAGE: '缺少镜像', MISSING_RUNTIME: '缺少运行实现' }
</script>

<template>
  <div class="resource-overview">
    <div class="inventory-counts">
      <div v-for="g in groups" :key="g.key"><component :is="g.icon" :size="19" /><span>{{ g.key === 'cpu' ? 'CPU 节点' : g.key === 'dsp' ? 'DSP 核心 / 单元' : 'FPGA 区域 / 单元' }}</span><strong>{{ g.nodes.length }}</strong></div>
      <div><Box :size="19" /><span>{{ simulated ? '运行仿真实例' : '运行实例记录' }}</span><strong>{{ active }}</strong></div>
    </div>
    <p class="inventory-explainer">下方卡片是固定的硬件资源节点，切换任务后仍会显示。“已占用”包含背景、执行和 Warm 保留；Warm 可在实例表中释放。可用预算不代表实时硬件利用率，各核心/区域的容量不能跨区域拼接。</p>

    <section v-for="g in groups" :key="g.key" class="inventory-group">
      <div class="inventory-heading"><h2><component :is="g.icon" :size="18" />{{ g.name }}</h2><span>{{ g.nodes.length }} 个可放置节点</span></div>
      <div v-if="!g.nodes.length" class="inventory-empty">当前配置未提供此类资源</div>
      <div v-else class="resource-node-grid">
        <article v-for="node in g.nodes" :key="node.node_id" class="resource-node-card">
          <div class="resource-node-heading"><strong>{{ node.unit_index != null ? (g.key === 'dsp' ? '核心 ' : '区域 ') + node.unit_index : node.node_id }}</strong><span>{{ simulated ? '模拟预算' : '配置预算' }}</span></div>
          <p class="resource-device">{{ node.device_id || node.hardware_id }}</p>
          <template v-for="metric in g.metrics" :key="metric.key">
            <div v-if="total(node,metric) > 0" class="resource-metric">
              <div><span>{{ metric.label }}</span><strong>可用 {{ format(available(node,metric),metric) }} / {{ format(total(node,metric),metric) }} {{ metric.unit }}</strong></div>
              <meter :value="used(node,metric)" min="0" :max="total(node,metric)" :aria-label="`${node.node_id} ${metric.label}：已占用 ${used(node,metric)}，容量 ${total(node,metric)}`" />
              <small>已占用 {{ format(used(node,metric),metric) }} {{ metric.unit }}</small>
              <small v-if="node.reserved_breakdown" style="display:block">背景 {{ format(node.reserved_breakdown.background[metric.key] ?? 0,metric) }} · 执行 {{ format(node.reserved_breakdown.active[metric.key] ?? 0,metric) }} · Warm {{ format(node.reserved_breakdown.warm[metric.key] ?? 0,metric) }}</small>
            </div>
          </template>
          <small class="resource-node-id">{{ node.node_id }}</small>
        </article>
      </div>
    </section>

    <section class="inventory-group">
      <div class="inventory-heading"><h2><Box :size="18" />{{ simulated ? '可创建的仿真组件' : '组件容器创建配置' }}</h2><span>{{ templates.length }} 类组件</span></div>
      <p class="inventory-explainer">{{ simulated ? '以下是当前画像覆盖的 Python 参考组件，提交后才创建实例；DSP/FPGA 资源为软件模型。' : '镜像名称来自配置，存在性、节点和资源在执行前复核。这里只显示当前后端管理的实例。' }}</p>
      <p class="inventory-explainer">{{ resources.idle_container_pool_supported ? '支持跨任务 Warm 复用；池在当前后端进程内共享。' : '当前运行后端未提供池化接口。' }}空闲 Warm 实例 {{ warm }} 个，保留的内存与逻辑资源仍计入预留。</p>
      <div v-if="!templates.length" class="inventory-empty">暂无组件创建配置</div>
      <div v-else class="table-scroll"><table><thead><tr><th>组件</th><th>可选处理资源</th><th>创建状态</th><th>{{ simulated ? '执行方式' : '配置镜像' }}</th></tr></thead><tbody>
        <tr v-for="item in templates" :key="item.cid"><td><strong>{{ item.cid }}</strong> · {{ item.name }}</td><td>{{ [...new Set(item.implementations.map(i => i.processor.toUpperCase()))].join(' / ') || '—' }}</td><td>{{ creationLabel[item.creation_status] || item.creation_status }}</td><td>{{ simulated ? 'CPU 上的 Python 参考实现' : item.image || '—' }}</td></tr>
      </tbody></table></div>
    </section>
  </div>
</template>

<style scoped>
.inventory-counts{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:22px 0 14px}.inventory-counts>div{display:flex;align-items:center;gap:9px;padding:17px;background:#fff;border:1px solid #dce7e1;border-radius:6px;color:#50766a}.inventory-counts span{font-size:12px}.inventory-counts strong{margin-left:auto;font-size:25px;color:#203e35}.inventory-explainer{font-size:12px;line-height:1.8;color:#6d847b;margin:9px 0 15px}.inventory-group{margin-top:28px}.inventory-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:13px}.inventory-heading h2{display:flex;align-items:center;gap:8px;font-size:15px;color:#29413e}.inventory-heading>span{font-size:12px;color:#71867d}.resource-node-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.resource-node-card{padding:17px;background:#fff;border:1px solid #dce7e1;border-radius:6px;min-width:0}.resource-node-heading{display:flex;justify-content:space-between;gap:10px;font-size:14px;color:#28483d}.resource-node-heading>span{font-size:11px;color:#71867d}.resource-device{font-size:12px;color:#70877d;margin:6px 0 17px;overflow-wrap:anywhere}.resource-metric{margin:13px 0}.resource-metric>div{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;font-size:12px}.resource-metric strong{font-weight:500;color:#234f43}.resource-metric meter{display:block;width:100%;height:13px;margin:6px 0 3px;accent-color:#16837b}.resource-metric small,.resource-node-id{font-size:11px;color:#7a8f85}.resource-node-id{display:block;border-top:1px solid #edf2ef;padding-top:10px;overflow-wrap:anywhere}.inventory-empty{padding:22px 0;font-size:12px;color:#758a80}.table-scroll td{white-space:normal;min-width:110px;overflow-wrap:anywhere}@media(max-width:1000px){.inventory-counts{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:600px){.resource-node-grid{grid-template-columns:1fr}.inventory-counts>div{padding:12px;flex-wrap:wrap}.inventory-counts span{font-size:11px}.inventory-counts strong{font-size:21px}.inventory-heading{align-items:flex-start}.resource-metric>div{align-items:flex-start}.resource-metric strong{font-size:11px}}
</style>
