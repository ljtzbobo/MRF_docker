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
  <div class="flow-node" :class="`flow-node--${data.level}`">
    <Handle type="target" :position="Position.Left" />
    <div class="flow-node__top">
      <span class="flow-node__kind">{{ data.level === 'business' ? '业务功能' : data.level === 'specification' ? '规格能力' : '组件实例' }}</span>
      <span v-if="data.cid" class="flow-node__cid">{{ data.cid }}</span>
    </div>
    <strong :title="title">{{ title }}</strong>
    <span class="flow-node__sub" :title="subtitle">{{ subtitle }}</span>
    <div v-if="input || output" class="flow-node__ports">
      <span :title="input">{{ input || '—' }}</span><span :title="output">{{ output || '—' }}</span>
    </div>
    <Handle type="source" :position="Position.Right" />
  </div>
</template>
