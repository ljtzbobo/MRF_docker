<script setup lang="ts">
import { computed,onMounted,ref,watch } from 'vue'
import { api } from '../api'
import { useWorkbench } from '../stores/workbench'
import type { DatasetRecord,DatasetScenario } from '../types'
const store=useWorkbench()
const records=ref<DatasetRecord[]>([]),scenarios=ref<DatasetScenario[]>([])
const scenarioId=ref('adsb_spectrum'),recordId=ref(''),error=ref(''),message=ref('')
const start=ref(0),length=ref(4096),count=ref(3),observation=ref(0),budget=ref(1000)
const busy=ref(false)
const scenario=computed(()=>scenarios.value.find(s=>s.scenario_id===scenarioId.value))
const options=computed(()=>records.value.filter(r=>{
  const band=scenario.value?.frequency_range_hz,f=r.signal.center_frequency_hz
  return ['ready','description_ready'].includes(r.status) && r.signal.representation===scenario.value?.input_representation
    && (!band || (f!=null && f>=band[0] && f<=band[1]))
}))
const selected=computed(()=>records.value.find(r=>r.record_id===recordId.value))
const pending=computed(()=>records.value.filter(r=>r.status!=='ready'))
function selectRecord(){
  recordId.value=options.value.find(r=>scenarioId.value.startsWith('iridium') ? r.adapter==='iridium_mat' && r.title.includes('QPSK') : true)?.record_id ?? ''
}
watch(scenarioId,()=>{
  const defaults=scenario.value?.defaults
  length.value=defaults?.frame_length ?? 4096;count.value=defaults?.frame_count ?? 3
  start.value=0;observation.value=0
  selectRecord()
})
async function refresh(){
  error.value=''
  try {
    const [inventory,choices]=await Promise.all([api.datasets(),api.datasetScenarios()])
    records.value=inventory.records;scenarios.value=choices
    selectRecord()
    if(inventory.warnings.length)message.value=inventory.warnings.join('；')
  }catch(e){error.value=e instanceof Error?e.message:'数据目录读取失败'}
}
async function generate(){
  busy.value=true;error.value='';message.value=''
  try {
    const bundle=await api.generateDatasetTask(scenarioId.value,recordId.value,{
      sample_start:start.value,frame_length:length.value,frame_count:count.value,
      observation_index:observation.value,deadline_after_ms:budget.value,
      task_id:`dataset-${scenarioId.value}-${Date.now()}`,
    })
    await store.analyze(bundle.task,`数据集 · ${selected.value?.title ?? recordId.value}`)
    message.value=`已生成外部任务；组件匹配：${bundle.compatibility.match_status}。${bundle.limitations.join(' ')}`
  }catch(e){error.value=e instanceof Error?e.message:'任务生成失败'}
  finally{busy.value=false}
}
onMounted(refresh)
</script>

<template>
  <details class="dataset-panel" open>
    <summary>真实数据集任务生成器 <span>{{ records.length }} 条记录已登记</span></summary>
    <p>先选业务场景与记录，再生成任务。任务 JSON 只携带引用；提交执行后由后端分帧读取信号。</p>
    <div v-if="error" class="notice notice--error">{{ error }}</div>
    <div class="dataset-fields">
      <label>射频业务场景<select v-model="scenarioId" :disabled="busy || store.executing"><option v-for="s in scenarios" :key="s.scenario_id" :value="s.scenario_id">{{ s.title }}</option></select></label>
      <label>数据记录<select v-model="recordId" :disabled="busy || store.executing"><option v-for="r in options" :key="r.record_id" :value="r.record_id">{{ r.title }}</option></select></label>
      <label>{{ selected?.adapter==='radar_reference' ? '起始脉冲' : '起始样点' }}<input v-model.number="start" type="number" min="0" /></label>
      <label>{{ selected?.adapter==='radar_reference' ? '每帧脉冲' : '每帧样点' }}<input v-model.number="length" type="number" min="1" /></label>
      <label>帧数<input v-model.number="count" type="number" min="1" max="1024" /></label>
      <label>处理时限（ms）<input v-model.number="budget" type="number" min="1" /></label>
      <label v-if="selected?.adapter==='iridium_mat'">突发记录索引<input v-model.number="observation" type="number" min="0" :max="(selected.signal.observation_count ?? 1)-1" /></label>
    </div>
    <p v-if="selected">采样率 {{ selected.signal.sample_rate_hz?.toLocaleString() ?? '未知' }} Hz · 中心频率 {{ selected.signal.center_frequency_hz?.toLocaleString() ?? '不适用（音频）' }} Hz · {{ selected.signal.sample_count?.toLocaleString() }} 样点{{ selected.signal.observation_count ? ' / 每条独立突发' : '' }}</p>
    <div class="dataset-actions"><button class="primary-button" :disabled="!recordId || busy || store.executing || store.busy" @click="generate">{{ busy ? '生成中…' : '生成任务并载入模块一' }}</button><button class="secondary-button" :disabled="busy" @click="refresh">刷新目录</button></div>
    <p v-if="message" role="status">{{ message }}</p>
    <p v-if="!records.length">尚无索引，请先在项目目录运行 python -m datasets scan --hash。</p>
    <details v-if="pending.length"><summary>待完善的数据（{{ pending.length }}）</summary><p v-for="r in pending" :key="r.record_id">{{ r.title }}：{{ r.missing_metadata?.join('、') ?? r.status }}</p></details>
  </details>
</template>

<style scoped>
.dataset-panel{margin:0 0 20px;padding:18px;border:1px solid #dce7e1;border-radius:6px;background:#fff}.dataset-panel summary{font-size:14px;font-weight:600;cursor:pointer;color:#29413e}.dataset-panel summary span{font-size:11px;font-weight:400;margin-left:12px;color:#72867e}.dataset-panel p{font-size:12px;color:#6b8279;line-height:1.8;overflow-wrap:anywhere}.dataset-fields{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:14px 0}.dataset-fields label{display:flex;flex-direction:column;gap:6px;font-size:12px;color:#476b5c}.dataset-fields label:nth-child(-n+2){grid-column:span 2}.dataset-fields select,.dataset-fields input{width:100%;min-width:0;padding:8px;border:1px solid #dce7e1;border-radius:4px;background:white;color:#29413e}.dataset-actions{display:flex;gap:10px;flex-wrap:wrap}@media(max-width:700px){.dataset-fields{grid-template-columns:repeat(2,minmax(0,1fr))}.dataset-fields label:nth-child(-n+2){grid-column:span 2}}
</style>
