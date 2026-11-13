<script setup lang="ts">
import { computed } from 'vue'
import type { ExecutionProgress, RecordValue } from '../types'
const props = defineProps<{ plan: RecordValue | null; progress: ExecutionProgress | null; inputFrames: number }>()
const allocations = computed(() => (props.plan?.allocations ?? []) as Array<RecordValue>)
type Decision = { action: string; feasible: boolean; selected?: boolean; reason: string; estimated_wait_ms?: number; estimated_total_ms?: number; additional_preparation_ms?: number }
const dispatch = computed(() => props.plan?.dispatch as RecordValue | null | undefined)
const decisions = computed(() => (dispatch.value?.decisions ?? []) as Decision[])
const actions: Record<string,string> = { active_reuse:'Active 排队复用', warm_reuse:'Warm 复用', cold_start:'冷启动', restore:'快照恢复', queue_fallback:'排队回退' }
const reasons: Record<string,string> = { within_deadline_and_capacity:'满足资源与时限',queue_full:'等待队列已满',queue_and_execution_exceed_deadline:'等待加执行预计超时',not_implemented_stage_four:'第四阶段待实现',queue_budget_exhausted:'实际等待达到预算',owner_failed_or_instance_unavailable:'前序任务失败或实例不可用',capacity_or_deadline_changed:'资源或时限已变化',INFEASIBLE:'不满足资源或时限约束',HEURISTIC_FAILED:'当前搜索未找到可行方案' }
reasons.queue_wait_limit_exceeded = '预计等待超过队列上限'
reasons.decision_time_exhausts_deadline = '决策耗时已消耗剩余时限'
function ms(value: unknown) { return typeof value === 'number' ? value.toFixed(3)+' ms' : '—' }
function actualPreparation(id: unknown) { return (dispatch.value?.actual_preparation_ms as RecordValue | undefined)?.[String(id)] }
const stages: Record<string,string> = { queued:'等待执行', planning:'重新规划', waiting_for_instance:'等待 Active 实例', queue_fallback:'排队回退，重新选择', planned:'资源已预留', starting_node:'准备运行实例', instance_acquired:'取得实例', node_ready:'实例就绪', processing_node:'处理数据', node_completed:'组件本帧完成', node_failed:'组件失败', frame_completed:'整帧完成', cleanup:'归还或清理实例', pool_returned:'实例已归还池', resources_released:'任务预留已结算', completed:'任务完成', failed:'任务失败' }
const states: Record<string,string> = { WAITING:'等待', STARTING:'创建中', READY:'就绪', RUNNING:'运行中', COMPLETED:'已完成', FAILED:'失败', QUEUED:'排队' }
stages.dynamic_event = '处理动态调度事件'
stages.rebound = '局部重绑定完成'
stages.rebind_skipped = '保留原方案'
const events = computed(() => [...(props.progress?.events ?? [])].reverse().slice(0, 40))
function demand(a: RecordValue) {
  const d = (a.demand ?? {}) as Record<string,number>
  if (a.processor === 'dsp') return `${d.dsp_compute_mcycles_per_s ?? 0} 百万周期/秒 · ${(d.dsp_local_memory_bytes ?? 0)/1024} KiB L2`
  if (a.processor === 'fpga') return `LUT ${d.fpga_lut ?? 0} · FF ${d.fpga_ff ?? 0} · DSP48 ${d.fpga_dsp48 ?? 0} · BRAM ${d.fpga_bram36 ?? 0}`
  return `${d.cpu_millicores ?? 0} 毫核 · ${((d.memory_bytes ?? 0)/2**20).toFixed(1)} MiB`
}
</script>

<template>
  <section v-if="allocations.length || progress" class="execution-panel" aria-label="分配方案与执行进度">
    <div class="execution-panel-heading"><h2>分配方案与执行进度</h2><span>{{ progress ? stages[progress.stage] || progress.stage : '方案预览 · 尚未执行' }}</span></div>
    <div v-if="plan" class="plan-facts"><span>算法 <b>{{ plan.algorithm ?? '—' }}</b></span><span>计划帧数 <b>{{ plan.frame_count ?? 1 }}</b></span><span>估算 <b>{{ typeof plan.estimated_completion_ms === 'number' ? plan.estimated_completion_ms.toFixed(3) + ' ms' : '未验证' }}</b></span><span>{{ plan.profile_basis === 'synthetic' ? '合成资源画像' : '画像来源见方案' }}</span></div>
    <p v-if="!progress && inputFrames && inputFrames !== plan?.frame_count" class="execution-note">当前预览按 {{ plan?.frame_count ?? 1 }} 帧计算；提交时会按实际 {{ inputFrames }} 帧重新规划。</p>
    <div v-if="progress" class="real-progress" aria-live="polite">
      <p v-if="progress.stage === 'waiting_for_instance'">正在等待前一任务归还整批实例，已等待 {{ ms(progress.waiting_ms) }}。等待期间不额外预留硬件资源。</p>
      <p v-if="dispatch">本次实际排队 {{ ms(dispatch.actual_wait_ms) }}；实例准备耗时见下表。</p>
      <div><strong>组件调用 {{ progress.completed_calls }} / {{ progress.total_calls }}</strong><span>整帧 {{ progress.completed_frames }} / {{ progress.total_frames }}</span></div>
      <progress :value="progress.completed_calls" :max="Math.max(1, progress.total_calls)" aria-label="已完成的组件调用数" />
      <p>{{ progress.current_node ? '当前节点：' + progress.current_node : stages[progress.stage] || progress.stage }} · {{ progress.resources_released ? '任务预留已结算；Warm 常驻资源仍占用预算' : progress.state === 'FAILED' ? '请检查错误与资源释放状态' : '等待执行或清理完成' }}</p>
      <p class="execution-note">进度来自后端实际事件；调用进度完成后仍需清理。快速任务可能首次刷新就已完成，下方保留执行记录。</p>
    </div>
    <div class="table-scroll"><table><thead><tr><th>组件节点</th><th>分配位置</th><th>运行实例</th><th>资源需求</th><th>执行状态</th></tr></thead><tbody>
      <tr v-for="a in allocations" :key="String(a.node_id)"><td><strong>{{ a.cid }}</strong><small>{{ a.node_id }}</small></td><td><b>{{ String(a.processor).toUpperCase() }}</b><small>{{ a.processing_node_id }}</small></td><td>{{ progress ? actions[String(a.acquisition)] || a.acquisition : '提交时确认' }}<small v-if="progress">{{ a.container_unit_id }}</small><small>准备估算 {{ ms(a.estimated_acquisition_ms ?? a.setup_p95_ms) }} · 实际 {{ ms(actualPreparation(a.node_id)) }}</small></td><td>{{ demand(a) }}</td><td>{{ progress ? states[progress.nodes[String(a.node_id)]?.state ?? 'WAITING'] : '待提交' }}<small v-if="progress">{{ progress.nodes[String(a.node_id)]?.completed_frames ?? 0 }} / {{ progress.total_frames }} 帧</small></td></tr>
    </tbody></table></div>
    <details v-if="decisions.length" class="execution-events"><summary>动作选择与开销评价</summary><p>先检查 Active 排队复用，再检查 Warm 和冷启动；仅采用满足资源与剩余时限的方案。准备估算来自历史记录或画像，实际值来自本次执行计时。</p><div class="table-scroll"><table><thead><tr><th>动作</th><th>预计等待</th><th>预计准备</th><th>预计总耗时</th><th>判定</th></tr></thead><tbody><tr v-for="(decision,i) in decisions" :key="i"><td>{{ actions[decision.action] || decision.action }}</td><td>{{ ms(decision.estimated_wait_ms) }}</td><td>{{ ms(decision.additional_preparation_ms) }}</td><td>{{ ms(decision.estimated_total_ms) }}</td><td>{{ decision.selected ? '已选择 · ' : '' }}{{ reasons[decision.reason] || decision.reason }}</td></tr></tbody></table></div></details>
    <details v-if="events.length" class="execution-events"><summary>执行记录（保留最近 256 条，显示最近 40 条）</summary><ol><li v-for="(event,i) in events" :key="i"><time>{{ new Date(event.at*1000).toLocaleTimeString('zh-CN', { hour12:false }) }}</time><strong>{{ stages[event.event] || event.event }}</strong><span>{{ event.node_id }}{{ event.frame_index ? ' · 第 ' + event.frame_index + ' 帧' : '' }}</span></li></ol></details>
  </section>
</template>

<style scoped>
.execution-panel{background:#fff;border:1px solid #dce7e1;border-radius:6px;margin:0 0 28px;padding:18px;min-width:0}.execution-panel-heading,.real-progress>div{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap}.execution-panel-heading h2{font-size:15px;color:#29413e}.execution-panel-heading>span{font-size:12px;color:#16837b}.plan-facts{display:flex;gap:18px;flex-wrap:wrap;margin:15px 0;color:#71887c;font-size:12px}.plan-facts b{color:#31584b;font-weight:500}.real-progress{border-top:1px solid #e3ece6;padding:15px 0}.real-progress>div{font-size:12px;color:#31584b}.real-progress progress{display:block;width:100%;height:14px;margin:9px 0;accent-color:#16837b}.real-progress p,.execution-note{font-size:12px;color:#71887c;line-height:1.7;margin:8px 0}.table-scroll td{white-space:normal;min-width:100px;overflow-wrap:anywhere}.table-scroll td small{display:block;color:#7c9287;font-size:11px;margin-top:5px}.execution-events{margin-top:15px;font-size:12px;color:#476b5c}.execution-events summary{cursor:pointer}.execution-events ol{list-style:none;margin:10px 0 0;padding:0}.execution-events li{display:flex;gap:12px;flex-wrap:wrap;padding:8px 0;border-top:1px solid #edf2ef}.execution-events time{color:#8a9d93}.execution-events strong{font-weight:500}@media(max-width:600px){.execution-panel{padding:12px}.plan-facts{gap:9px}.table-scroll table{min-width:620px}}
</style>
