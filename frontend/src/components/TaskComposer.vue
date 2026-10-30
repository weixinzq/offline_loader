<script setup lang="ts">
import { computed, ref, toRef, watch } from 'vue'
import AccountPagination from './AccountPagination.vue'
import { useAccountPagination } from '../accountPagination'
import { send } from '../api'
import type { Account, Script, Session, TaskRecord, Step, MessagesValidation } from '../types'

const props = withDefaults(defineProps<{ accounts: Account[]; scripts: Script[]; selectedIds: string[]; sessions?: Session[] }>(), { sessions: () => [] })
const emit = defineEmits<{ created: [taskId: string]; failure: [error: unknown] }>()
type Draft = { key: number; kind: 'messages' | 'script'; text: string; scriptId: string; repeat: number; filename: string }
const ids = ref([...props.selectedIds])
const mode = ref<'script' | 'combination'>('script')
const scriptId = ref('')
const steps = ref<Draft[]>([])
const repetitions = ref(1)
const repeatInterval = ref(0.5)
const messageDelay = ref(0.2)
const busy = ref(false)
const error = ref('')
const checked = ref('')
let nextKey = 0
const { accountPage, accountPages, visibleAccounts } = useAccountPagination(toRef(props, 'accounts'))
const selected = computed(() => new Set(ids.value))
const ready = computed(() => mode.value === 'script' ? !!scriptId.value : steps.value.length > 0 && steps.value.every(s => s.kind === 'script' ? !!s.scriptId : !!s.text.trim()))
watch(() => props.selectedIds, value => { ids.value = [...value] })
watch(() => props.accounts, value => {
  const existing = new Set(value.map(a => a.account_id)); ids.value = ids.value.filter(id => existing.has(id))
})
watch([steps, repetitions, repeatInterval, messageDelay], () => { checked.value = '' }, { deep: true })

function toggle(id: string, value: boolean) {
  ids.value = value ? [...new Set([...ids.value, id])] : ids.value.filter(item => item !== id)
}
function add(kind: Draft['kind']) {
  steps.value.push({ key: ++nextKey, kind, text: '', scriptId: '', repeat: 1, filename: '' })
  error.value = ''
}
function move(index: number, offset: number) {
  const target = index + offset
  if (target < 0 || target >= steps.value.length) return
  const [step] = steps.value.splice(index, 1)
  steps.value.splice(target, 0, step!)
}
async function importFile(step: Draft, event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  try { step.text = await file.text(); step.filename = file.name; error.value = '' }
  catch { error.value = '消息文件读取失败，请重新选择' }
  finally { input.value = '' }
}
async function buildSteps(): Promise<Step[]> {
  if (!steps.value.length) throw new Error('请先添加组合步骤')
  const groups = await Promise.all(steps.value.map(async (draft, index) => {
    const step = { ...draft }
    if (!Number.isInteger(step.repeat) || step.repeat < 1) throw new Error(`第 ${index + 1} 步的执行次数必须为正整数`)
    let value: Step
    if (step.kind === 'script') {
      if (!step.scriptId) throw new Error(`请选择第 ${index + 1} 步的专用脚本`)
      value = { kind: 'script', script_id: step.scriptId }
    } else {
      if (!step.text.trim()) throw new Error(`请填写第 ${index + 1} 步的消息`)
      try {
        const parsed = await send<MessagesValidation>('/messages/parse', { text: step.text })
        if (!parsed.valid || !parsed.messages.length) throw new Error('没有可执行的消息')
        value = { kind: 'messages', messages: parsed.messages }
      } catch (cause) { throw new Error(`第 ${index + 1} 步：${cause instanceof Error ? cause.message : '消息格式错误'}`) }
    }
    return Array.from({ length: step.repeat }, () => value)
  }))
  return groups.flat()
}
async function check() {
  busy.value = true; error.value = ''; checked.value = ''
  try { const result = await buildSteps(); checked.value = `组合检查通过，共 ${result.length} 个执行步骤。可以选择账号并提交。` }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '检查失败' }
  finally { busy.value = false }
}
async function submit() {
  if (!ready.value || !ids.value.length || busy.value) return
  busy.value = true; error.value = ''
  try {
    const account_ids = [...ids.value]
    const body = mode.value === 'script' ? { account_ids, script_id: scriptId.value } : {
      account_ids, steps: await buildSteps(), repetitions: repetitions.value,
      repeat_interval: repeatInterval.value, message_delay: messageDelay.value,
    }
    const task = await send<TaskRecord>('/tasks', body)
    emit('created', task.task_id)
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '提交失败'; emit('failure', cause) }
  finally { busy.value = false }
}
</script>

<template>
  <div class="page-heading"><div><p class="eyebrow">NEW TASK</p><h1>提交任务</h1><p>先编排任务，再选择账号，由后台按顺序执行。</p></div></div>
  <form class="task-form" @submit.prevent="submit">
    <section class="panel composition" aria-label="任务编排">
      <div class="panel-heading"><div><p class="eyebrow">01 · BUILD</p><h2>编排任务</h2></div><div class="mode-switch" role="group" aria-label="任务类型"><button type="button" :aria-pressed="mode === 'script'" :class="{ active: mode === 'script' }" :disabled="busy" @click="mode = 'script'">专用任务</button><button type="button" :aria-pressed="mode === 'combination'" :class="{ active: mode === 'combination' }" :disabled="busy" @click="mode = 'combination'">组合任务</button></div></div>
      <div class="composition-body">
        <label v-if="mode === 'script'" class="field single-script">执行脚本<select v-model="scriptId" required aria-label="执行脚本" :disabled="busy"><option value="" disabled>请选择脚本</option><option v-for="script in scripts" :key="script.script_id" :value="script.script_id">{{ script.name }}</option></select></label>
        <template v-else>

          <ol class="composition-steps">
            <li v-for="(step, index) in steps" :key="step.key" class="composition-step">
              <div class="step-top"><span class="step-number">{{ index + 1 }}</span><h3>{{ step.kind === 'messages' ? '通用消息' : '专用脚本' }}<small v-if="step.filename"> · {{ step.filename }}</small></h3><div class="step-actions"><button type="button" class="icon-button" :aria-label="`上移第${index + 1}步`" :disabled="busy || index === 0" @click="move(index, -1)">↑</button><button type="button" class="icon-button" :aria-label="`下移第${index + 1}步`" :disabled="busy || index === steps.length - 1" @click="move(index, 1)">↓</button><button type="button" class="text-button danger-text" :aria-label="`删除第${index + 1}步`" :disabled="busy" @click="steps.splice(index, 1)">删除</button></div></div>
              <template v-if="step.kind === 'messages'">
                <label class="field">消息内容<textarea v-model="step.text" :aria-label="`第${index + 1}步消息内容`" rows="4" spellcheck="false" :disabled="busy" placeholder='#send={"id":21,"param":{"ed":false,"mi":60,"tp":2,"qt":1,"posud":false},"cmd":"buyMaterial"}|' /></label>
                <div class="message-tools"><label class="file-button">导入消息文件<input type="file" accept=".txt,text/plain" :aria-label="`导入第${index + 1}步消息文件`" :disabled="busy" @change="importFile(step, $event)" /></label><span>支持粘贴多条 #send 消息，也可导入 .txt 后继续编辑。</span></div>
              </template>
              <label v-else class="field">执行脚本<select v-model="step.scriptId" :aria-label="`第${index + 1}步专用脚本`" :disabled="busy"><option value="" disabled>请选择脚本</option><option v-for="script in scripts" :key="script.script_id" :value="script.script_id">{{ script.name }}</option></select></label>
              <label class="step-repeat">此步骤执行<input v-model.number="step.repeat" type="number" min="1" step="1" required :aria-label="`第${index + 1}步执行次数`" :disabled="busy" />次</label>
            </li>
          </ol>
          <div class="composition-toolbar"><div class="button-row"><button type="button" class="button secondary small" :disabled="busy" @click="add('messages')">＋ 通用消息</button><button type="button" class="button secondary small" :disabled="busy" @click="add('script')">＋ 专用脚本</button></div><button type="button" class="text-button" :disabled="busy || !steps.length" @click="check">检查组合</button></div>
          <div class="execution-options"><label class="field">整组执行次数<input v-model.number="repetitions" type="number" min="1" step="1" required aria-label="整组执行次数" :disabled="busy" /></label><label class="field">消息间隔（秒）<input v-model.number="messageDelay" type="number" min="0" step="0.1" required aria-label="消息间隔" :disabled="busy" /></label><label class="field">组间间隔（秒）<input v-model.number="repeatInterval" type="number" min="0" step="0.1" required aria-label="组间间隔" :disabled="busy" /></label></div>
          <p v-if="checked" class="checked-note" role="status">{{ checked }}</p>
        </template>
        <p v-if="error" class="danger-text" role="alert">{{ error }}</p>
      </div>
    </section>
    <section class="panel account-section" aria-label="选择执行账号">
      <div class="panel-heading"><div><p class="eyebrow">02 · RUN</p><h2>选择执行账号</h2></div><span class="selection-count">已选 {{ ids.length }} 个</span></div>
      <div class="account-body"><p class="help">在线账号直接执行；离线账号从 IP 池申请出口，池满时等待。任务结束后保持在线。</p><div class="button-row selection-tools"><button type="button" class="text-button" :disabled="busy" @click="ids = accounts.map(a => a.account_id)">选择全部账号</button><button type="button" class="text-button" :disabled="busy" @click="ids = []">清空选择</button></div>
        <div class="table-scroll account-table"><table><thead><tr><th>选择</th><th>账号</th></tr></thead><tbody><tr v-for="account in visibleAccounts" :key="account.account_id"><td><input type="checkbox" :aria-label="`选择${account.label}`" :checked="selected.has(account.account_id)" :disabled="busy" @change="toggle(account.account_id, ($event.target as HTMLInputElement).checked)" /></td><td>{{ account.label }}</td></tr><tr v-if="!accounts.length"><td colspan="2" class="empty">请先在账号管理中导入账号</td></tr></tbody></table></div>
        <AccountPagination v-model:page="accountPage" :pages="accountPages" :total="accounts.length" />
      </div>
    </section>
    <div class="submit-bar"><div><strong>{{ mode === 'combination' ? `${steps.length} 个编排步骤` : '专用任务' }} · {{ ids.length }} 个账号</strong><p>确认顺序与执行次数后提交，后台将为各账号执行整条流程。</p></div><button class="button primary" :disabled="busy || !ids.length || !ready">{{ busy ? '处理中…' : mode === 'combination' ? '提交组合任务' : '提交任务' }}</button></div>
  </form>
</template>

<style scoped>
.task-form{display:grid;gap:22px}.panel-heading{display:flex;justify-content:space-between;align-items:center;gap:16px}.composition-body,.account-body{padding:0 28px 24px}.mode-switch{display:flex;padding:4px;background:#f1f4fa;border-radius:8px;gap:3px}.mode-switch button{padding:7px 16px;border-radius:5px;font-size:12px;color:#7b879a}.mode-switch .active{background:white;color:#3456bd;box-shadow:0 1px 5px #263d7520}.single-script{max-width:600px}.intro{color:#8490a2;font-size:12px;margin-bottom:18px}.composition-empty{text-align:center;padding:28px 12px;background:#f8faff;border:1px dashed #dce4f3;border-radius:8px;margin-bottom:16px}.empty-symbol{display:block;color:#8b9ecb;font-size:24px;margin-bottom:4px}.composition-empty h3{color:#62728c}.composition-empty p{font-size:12px;color:#96a0b0;margin-top:7px}.composition-steps{list-style:none;margin:0;padding:0;display:grid;gap:14px}.composition-step{padding:16px 20px;border:1px solid #e2e8f2;border-radius:8px;background:#fcfdff}.step-top{display:flex;align-items:center;gap:10px}.step-top h3{color:#4c5e78;font-weight:550}.step-top small{font-weight:400;color:#939eb0;overflow-wrap:anywhere}.step-top .step-actions{gap:8px;align-items:center}.composition-step .field{margin:13px 0 10px}.composition-step textarea{min-height:105px}.message-tools{display:flex;align-items:center;flex-wrap:wrap;gap:12px;font-size:11px;color:#99a3b3}.file-button{display:inline-flex;align-items:center;color:#4266c3;background:#edf2ff;padding:6px 10px;border-radius:5px;cursor:pointer;position:relative;overflow:hidden}.file-button input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%}.step-repeat{display:flex;align-items:center;gap:8px;margin-top:14px;color:#8390a2;font-size:12px}.step-repeat input{width:72px;border:1px solid #dfe5ee;border-radius:5px;padding:6px 8px;background:white;color:#36465f}.composition-toolbar{display:flex;justify-content:space-between;gap:12px;margin:18px 0}.execution-options{display:grid;grid-template-columns:repeat(3,minmax(0,160px));gap:16px;border-top:1px solid #e9edf4;margin-top:20px;padding-top:2px}.checked-note{color:#4d8b73;font-size:12px;margin-top:10px}.selection-count{font-size:12px;color:#5a72a2;background:#eef3ff;padding:5px 12px;border-radius:6px}.account-body .help{margin:0 0 14px}.selection-tools{margin-bottom:16px}.account-table{max-height:360px;overflow:auto}.account-table thead{position:sticky;top:0;z-index:1;background:#f9fafc}.submit-bar{margin-top:0}.submit-bar strong{font-size:13px;color:#53637c}.submit-bar p{margin-top:4px}@media(max-width:760px){.composition-body,.account-body{padding:0 16px 18px}.panel-heading{align-items:flex-start;flex-wrap:wrap}.composition-step{padding:12px}.mode-switch button{padding:6px 12px}.execution-options{grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.composition-toolbar{align-items:center;flex-wrap:wrap}.account-table{max-height:300px}}
</style>
