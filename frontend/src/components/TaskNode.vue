<script setup lang="ts">
import { computed } from 'vue'
import { Handle, Position } from '@vue-flow/core'

const props = defineProps<{ data: Record<string, unknown> }>()
const title = computed(() => String(props.data.capability ?? props.data.role ?? props.data.id))
const subtitle = computed(() => String(props.data.cid ?? props.data.canonical_role ?? props.data.id))
const input = computed(() => Object.values((props.data.inputs ?? {}) as Record<string, string>).join(' · '))
const output = computed(() => Object.values((props.data.outputs ?? {}) as Record<string, string>).join(' · '))
</script>

<template>
  <div class="flow-node" :class="[`flow-node--${data.level}`, data.execution_state ? `flow-node--${String(data.execution_state).toLowerCase()}` : '']">
    <Handle type="target" :position="Position.Left" />
    <div class="flow-node__top">
      <span class="flow-node__kind">{{ data.level === 'business' ? '业务功能' : data.level === 'specification' ? '规格能力' : '组件实例' }}</span>
      <span v-if="data.cid" class="flow-node__cid">{{ data.cid }}</span>
    </div>
    <strong :title="title">{{ title }}</strong>
    <span class="flow-node__sub" :title="subtitle">{{ subtitle }}</span>
    <span v-if="data.processing_node_id" class="flow-node__sub">{{ data.processing_node_id }}</span>
    <span v-if="data.execution_state" class="flow-node__sub">{{ ({ RUNNING:'运行中', COMPLETED:'本帧已完成', FAILED:'失败', READY:'就绪', STARTING:'创建中', WAITING:'等待' } as Record<string,string>)[String(data.execution_state)] }}</span>
    <div v-if="input || output" class="flow-node__ports">
      <span :title="input">{{ input || '—' }}</span><span :title="output">{{ output || '—' }}</span>
    </div>
    <Handle type="source" :position="Position.Right" />
  </div>
</template>

<style scoped>
.flow-node--running{border-color:#c28c30;background:#fff9ec}.flow-node--completed{border-color:#16837b;background:#f1faf6}.flow-node--failed{border-color:#c96351;background:#fff3f0}
</style>
