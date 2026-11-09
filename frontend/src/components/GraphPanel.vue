<script setup lang="ts">
import { computed } from 'vue'
import { VueFlow, type NodeMouseEvent } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { Maximize2 } from 'lucide-vue-next'
import { layoutGraph } from '../graph'
import type { GraphData, GraphLevel } from '../types'
import TaskNode from './TaskNode.vue'

const props = defineProps<{ graph: GraphData | null; level: GraphLevel; selectedId: string | null }>()
const emit = defineEmits<{ select: [id: string | null] }>()
const layout = computed(() => props.graph ? layoutGraph(props.graph, props.level) : { nodes: [], edges: [] })
const narrow = window.matchMedia('(max-width: 800px)').matches

function selectNode(event: NodeMouseEvent) {
  emit('select', event.node.id)
}
</script>

<template>
  <div class="graph-panel">
    <div v-if="!graph" class="graph-empty">
      <Maximize2 :size="23" :stroke-width="1.5" />
      <span>{{ level === 'business' ? '上传任务后显示业务功能图' : level === 'specification' ? '当前任务尚无规格图' : '当前任务尚无组件执行图' }}</span>
    </div>
    <VueFlow
      v-else
      :key="level + ':' + graph.nodes.length"
      :nodes="layout.nodes"
      :edges="layout.edges"
      :nodes-draggable="false"
      :nodes-connectable="false"
      :elements-selectable="true"
      :min-zoom="0.3"
      :max-zoom="1.7"
      :fit-view-on-init="!narrow"
      :fit-view-on-init-options="{ padding: 0.22 }"
      :default-viewport="{ x: 20, y: 185, zoom: 0.9 }"
      @node-click="selectNode"
      @pane-click="emit('select', null)"
    >
      <template #node-task="nodeProps">
        <TaskNode :data="nodeProps.data" :class="{ 'is-selected': selectedId === nodeProps.id }" />
      </template>
      <Background :gap="20" :size="1" pattern-color="#dce7e4" />
      <Controls position="bottom-left" />
    </VueFlow>
  </div>
</template>
