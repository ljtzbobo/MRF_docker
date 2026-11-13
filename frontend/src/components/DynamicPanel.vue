<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref } from 'vue'
import { api } from '../api'
import type { RecordValue } from '../types'
const state=ref<RecordValue>({}), error=ref(''), kind=ref('resource_load'), taskId=ref(''), node=ref('sim-cpu'), value=ref(.95), busy=ref(false)
let timer:ReturnType<typeof setInterval>|undefined
async function refresh(){try{state.value=await api.dynamicState()}catch(e){error.value=e instanceof Error?e.message:'读取失败'}}
async function send(){busy.value=true;error.value='';try{
 const payload:RecordValue={event:kind.value}
 if(kind.value==='resource_load'){payload.processing_node_id=node.value;payload.utilization=value.value}
 else if(kind.value==='new_task')payload.source_task_id=taskId.value
 else{payload.task_id=taskId.value;if(kind.value==='priority_change')payload.priority=Number(value.value)}
 await api.dynamicEvent(payload);await refresh()
}catch(e){error.value=e instanceof Error?e.message:'事件提交失败'}finally{busy.value=false}}
function entries(){return (state.value.events??[]) as RecordValue[]}
onMounted(()=>{refresh();timer=setInterval(refresh,2000)})
onBeforeUnmount(()=>{if(timer)clearInterval(timer)})
</script>
<template><section class="resource-band"><h2>动态调度与局部重绑定</h2><p class="resource-note">软件仿真在完整数据帧之间调整一个组件，保留组件状态。负载事件是测试输入，不改变硬件总容量或背景预算；资源利用率恢复后请发送较低数值。</p>
 <div class="resource-actions"><select v-model="kind" aria-label="事件类型"><option value="resource_load">资源高负载</option><option value="priority_change">优先级变化</option><option value="deadline_risk">剩余时限风险</option><option value="new_task">新任务到达通知</option></select>
 <input v-if="kind==='resource_load'" v-model="node" aria-label="处理节点" placeholder="处理节点 ID" />
 <input v-else v-model="taskId" aria-label="任务 ID" placeholder="运行任务 ID / 新任务 ID" />
 <input v-if="kind==='resource_load'||kind==='priority_change'" v-model.number="value" type="number" :step="kind === 'resource_load' ? 0.05 : 1" aria-label="利用率或优先级" />
 <button class="secondary-button" :disabled="busy" @click="send">发送调度事件</button></div>
 <p class="resource-note">负载填 0～1，达到 0.85 触发避让；优先级填整数 0～100。运行任务 ID 可从实例表复制，事件会在下一帧边界处理。</p><p v-if="error" class="notice notice--error">{{ error }}</p>
 <details><summary>最近调度记录</summary><div class="table-scroll"><table><thead><tr><th>事件</th><th>任务</th><th>结果</th><th>原位置</th><th>新位置</th></tr></thead><tbody><tr v-for="(e,i) in entries().slice(-30).reverse()" :key="i"><td>{{ e.event }}</td><td>{{ e.task_id ?? e.source_task_id }}</td><td>{{ e.status ?? '已接收' }}</td><td>{{ e.source ?? e.processing_node_id }}</td><td>{{ e.target ?? '—' }}</td></tr></tbody></table></div></details></section></template>
