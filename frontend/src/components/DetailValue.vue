<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{
  value: unknown
  resource?: boolean
  keyName?: string
  depth?: number
}>(), { resource: false, keyName: '', depth: 0 })

const labels: Record<string, string> = {
  estimation_status: '估算状态', required_resources: '资源需求',
  allocation_constraints: '分配约束', cpu_millicores: 'CPU',
  memory_mib: '内存', memory_bytes: '内存', bandwidth_mbps: '带宽',
  bandwidth_bytes_per_s: '带宽', dsp_units: 'DSP', fpga_units: 'FPGA',
  rf_channels: '射频通道', rf_bandwidth_mhz: '射频带宽',
  not_estimated: '未估算', arrival_ms: '到达时间', duration_ms: '持续时间',
  deadline_ms: '截止时间',
}

function isContainer(value: unknown): value is Record<string, unknown> | unknown[] {
  return value !== null && typeof value === 'object'
}

function label(key: string): string {
  return labels[key] ?? key.replaceAll('_', ' ')
}

function display(value: unknown, key: string): string {
  if (value === null || value === undefined) return props.resource ? '未估算' : '未提供'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value === 'number') {
    const units: Record<string, string> = {
      cpu_millicores: 'mCPU', memory_mib: 'MiB', memory_bytes: 'B',
      bandwidth_mbps: 'Mb/s', bandwidth_bytes_per_s: 'B/s',
      rf_bandwidth_mhz: 'MHz', arrival_ms: 'ms', duration_ms: 'ms', deadline_ms: 'ms',
    }
    return `${value.toLocaleString('zh-CN')}${units[key] ? ` ${units[key]}` : ''}`
  }
  return labels[String(value)] ?? String(value)
}

const entries = computed(() => Array.isArray(props.value)
  ? props.value.map((item, index) => [String(index + 1), item] as const)
  : isContainer(props.value) ? Object.entries(props.value) : [])
</script>

<template>
  <div v-if="entries.length" class="structured-details" :class="{ 'structured-details--nested': depth > 0 }">
    <div v-for="[key, item] in entries" :key="key" class="structured-item">
      <div class="structured-row"><span>{{ label(key) }}</span><strong v-if="!isContainer(item)">{{ display(item, key) }}</strong></div>
      <DetailValue v-if="isContainer(item)" :value="item" :resource="resource" :key-name="key" :depth="depth + 1" />
    </div>
  </div>
  <div v-else class="structured-empty">{{ Array.isArray(value) ? '暂无条目' : isContainer(value) ? '暂无约束' : display(value, keyName) }}</div>
</template>
