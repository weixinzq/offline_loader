<script setup lang="ts">
import { computed, ref, toRef, watch } from 'vue'
import AccountPagination from './AccountPagination.vue'
import { useAccountPagination } from '../accountPagination'
import { send } from '../api'
import { label, tone } from '../presentation'
import type { Account, MessagesValidation, Script, Session, Step, TaskSpec, TaskPreflight, TaskRecord } from '../types'

const props = withDefaults(defineProps<{ accounts: Account[]; scripts: Script[]; selectedIds: string[]; sessions?: Session[] }>(), { sessions: () => [] })
const emit = defineEmits<{ created: [taskId: string]; failure: [error: unknown] }>()
const ids = ref([...props.selectedIds])
const selectedSet = computed(() => new Set(ids.value))
const { accountPage, accountPages, visibleAccounts } = useAccountPagination(toRef(props, 'accounts'))
const sessionsById = computed(() => new Map(props.sessions.map(session => [session.account_id, session])))
const stateOf = (id: string) => sessionsById.value.get(id)?.state ?? 'offline'
const readyIds = computed(() => props.accounts.filter(account => stateOf(account.account_id) === 'ready').map(account => account.account_id))
const policy = ref<'existing' | 'temporary'>(props.selectedIds.some(id => stateOf(id) === 'ready') ? 'existing' : 'temporary')
const unavailable = computed(() => policy.value === 'existing'
  ? props.accounts.filter(account => selectedSet.value.has(account.account_id) && stateOf(account.account_id) !== 'ready') : [])
const mode = ref<'bounded' | 'all_online'>('bounded')
const concurrency = ref(5)
const repetitions = ref(1)
const repeatInterval = ref(0.5)
const messageDelay = ref(0.2)
const steps = ref<Step[]>([])
const text = ref('')
const scriptId = ref('')
const busy = ref('')
const error = ref('')
const validation = ref<TaskPreflight | null>(null)
watch(() => props.selectedIds, (value, previous) => {
  if (value.join('\0') !== previous.join('\0')) ids.value = [...value]
})
watch(() => props.accounts, (value) => {
  const accountIds = new Set(value.map(account => account.account_id))
  ids.value = ids.value.filter(id => accountIds.has(id))
})
const spec = computed<TaskSpec>(() => ({
  kind: 'execute', account_ids: ids.value, session_policy: policy.value, mode: policy.value === 'existing' ? 'bounded' : mode.value,
  concurrency: concurrency.value, repetitions: repetitions.value, repeat_interval: repeatInterval.value,
  message_delay: messageDelay.value, steps: steps.value,
}))
watch(spec, () => { validation.value = null }, { deep: true })
watch(() => props.sessions, () => { validation.value = null }, { deep: true })
const numericInputValid = computed(() => Number.isInteger(concurrency.value) && concurrency.value >= 1 && concurrency.value <= 100
  && Number.isInteger(repetitions.value) && repetitions.value >= 1
  && Number.isFinite(repeatInterval.value) && repeatInterval.value >= 0
  && Number.isFinite(messageDelay.value) && messageDelay.value >= 0)
const canSubmit = computed(() => ids.value.length > 0 && steps.value.length > 0 && numericInputValid.value && !busy.value)

function failure(cause: unknown) {
  error.value = cause instanceof Error ? cause.message : '操作失败，请重试'
  emit('failure', cause)
}
function toggleAccount(accountId: string, event: Event) {
  const next = new Set(ids.value)
  if ((event.target as HTMLInputElement).checked) next.add(accountId); else next.delete(accountId)
  ids.value = [...next]
}
async function parseMessages() {
  busy.value = 'parse'; error.value = ''
  try {
    const parsed = await send<MessagesValidation>('/messages/parse', { text: text.value })
    if (!parsed.messages.length) throw new Error('没有解析出可用消息')
    steps.value.push({ kind: 'messages', messages: parsed.messages })
    text.value = ''
  } catch (cause) { failure(cause) } finally { busy.value = '' }
}
async function readFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  try { if (file) text.value = await file.text() } catch (cause) { failure(cause) }
  input.value = ''
}
function addScript() {
  if (scriptId.value) { steps.value.push({ kind: 'script', script_id: scriptId.value }); scriptId.value = '' }
}
function move(index: number, direction: number) {
  const step = steps.value.splice(index, 1)[0]
  if (step) steps.value.splice(index + direction, 0, step)
}
async function validate() {
  busy.value = 'validate'; error.value = ''; validation.value = null
  try { validation.value = await send<TaskPreflight>('/tasks/validate', spec.value) }
  catch (cause) { failure(cause) } finally { busy.value = '' }
}
async function submit() {
  busy.value = 'submit'; error.value = ''
  try {
    const result = await send<TaskRecord>('/tasks', spec.value)
    emit('created', result.task_id)
  } catch (cause) { failure(cause) } finally { busy.value = '' }
}
</script>

<template>
  <div class="page-heading"><div><p class="eyebrow">NEW TASK</p><h1>编排任务</h1><p>选定账号和执行步骤，后台将按顺序运行。</p></div></div>
  <div class="composer-grid">
    <section class="panel">
      <div class="panel-heading"><h2>1. 选择账号</h2><span class="muted">已选 {{ ids.length }} 个</span></div>
      <div class="selection-toolbar"><button type="button" class="text-button" @click="ids = accounts.map(a => a.account_id)">全选</button><button type="button" class="text-button" :disabled="!readyIds.length" @click="ids = [...readyIds]; policy = 'existing'">选择就绪账号</button><button type="button" class="text-button" @click="ids = []">清空</button></div>
      <div v-if="unavailable.length" class="notice" role="status"><strong>所选账号中 {{ unavailable.length }} 个未就绪，将跳过并继续其他就绪账号。</strong><p>{{ unavailable.map(account => `${account.label || account.account}（${label(stateOf(account.account_id))}）`).join('、') }}</p><p class="help">提交时后台会再次检查；若全部未就绪，会提示先连接账号。</p></div>
      <div v-if="!accounts.length" class="empty compact">先在账号页面添加账号。</div>
      <AccountPagination v-model:page="accountPage" :pages="accountPages" :total="accounts.length" />
      <div class="account-picker"><label v-for="account in visibleAccounts" :key="account.account_id" class="account-choice"><input type="checkbox" :checked="selectedSet.has(account.account_id)" :value="account.account_id" :aria-label="`选择账号 ${account.label || account.account}`" @change="toggleAccount(account.account_id, $event)" /><span><strong>{{ account.label || account.account }}</strong><small>{{ account.account }}</small><span class="badge" :class="tone(stateOf(account.account_id))">{{ label(stateOf(account.account_id)) }}</span><small v-if="sessionsById.get(account.account_id)?.error" class="danger-text">{{ sessionsById.get(account.account_id)?.error }}</small></span></label></div>
      <div class="section-divider"></div>
      <h2>2. 会话与并发</h2>
      <label class="field">连接方式<select v-model="policy"><option value="existing">使用已有就绪会话</option><option value="temporary">创建临时会话</option></select></label>
      <p class="help">{{ policy === 'existing' ? '未就绪账号会记录为已跳过；并发数限制同时执行的账号，任务结束后保留会话。' : '任务负责登录和清理；已在线账号会返回冲突。' }}</p>
      <template v-if="policy === 'temporary'"><label class="field">批处理方式<select v-model="mode"><option value="bounded">分批执行，完成后释放连接</option><option value="all_online">全部上线后执行</option></select></label>
      <p class="help">{{ mode === 'bounded' ? '并发限制覆盖连接、执行和清理的完整过程。' : '并发值限制登录过程，启动前会检查在线容量。' }}</p></template>
      <div class="field-row"><label class="field">并发数<input v-model.number="concurrency" type="number" min="1" max="100" required /></label><label class="field">执行次数<input v-model.number="repetitions" type="number" min="1" required /></label></div>
      <div class="field-row"><label class="field">轮次间隔（秒）<input v-model.number="repeatInterval" type="number" min="0" step="0.1" required /></label><label class="field">消息间隔（秒）<input v-model.number="messageDelay" type="number" min="0" step="0.1" required /></label></div>
    </section>
    <section class="panel">
      <div class="panel-heading"><h2>3. 执行步骤</h2><span class="muted">{{ steps.length }} 步</span></div>
      <ol v-if="steps.length" class="step-list"><li v-for="(step, index) in steps" :key="index"><span class="step-number">{{ index + 1 }}</span><div><strong>{{ step.kind === 'messages' ? `发送 ${step.messages.length} 条消息` : scripts.find(s => s.script_id === step.script_id)?.name || step.script_id }}</strong><small>{{ step.kind === 'messages' ? step.messages.map(m => m.cmd).join(' · ') : '运行脚本' }}</small></div><div class="step-actions"><button type="button" class="icon-button" :disabled="index === 0" aria-label="上移步骤" @click="move(index, -1)">↑</button><button type="button" class="icon-button" :disabled="index === steps.length - 1" aria-label="下移步骤" @click="move(index, 1)">↓</button><button type="button" class="icon-button danger-text" aria-label="删除步骤" @click="steps.splice(index, 1)">×</button></div></li></ol>
      <div v-else class="empty compact">添加消息或脚本，建立执行顺序。</div>
      <div class="step-editor"><label class="field">添加消息<textarea v-model="text" rows="7" placeholder="粘贴消息内容，或从本机文件读取" spellcheck="false"></textarea></label><div class="split-actions"><label class="button subtle file-button">读取文件<input type="file" accept=".txt,.json,.log" @change="readFile" /></label><button type="button" class="button secondary" :disabled="!text.trim() || !!busy" @click="parseMessages">{{ busy === 'parse' ? '解析中…' : '解析并添加' }}</button></div></div>
      <div class="step-editor"><label class="field">添加脚本<select v-model="scriptId"><option value="">选择脚本</option><option v-for="script in scripts" :key="script.script_id" :value="script.script_id">{{ script.name }}</option></select></label><p v-if="scriptId" class="help">{{ scripts.find(s => s.script_id === scriptId)?.description }}</p><button type="button" class="button secondary" :disabled="!scriptId || !!busy" @click="addScript">添加脚本步骤</button></div>
    </section>
  </div>
  <div v-if="error" class="notice error" role="alert">{{ error }}</div>
  <p v-if="!numericInputValid" class="notice error" role="alert">并发数需为 1–100 的整数，执行次数需为正整数，间隔需为非负数字。</p>
  <div v-if="validation" class="notice success"><strong>预检完成</strong><p>可执行 {{ validation.account_count }} 个账号，跳过 {{ validation.skipped_accounts?.length ?? 0 }} 个。</p><p v-for="account in validation.skipped_accounts" :key="account.account_id">{{ account.label }}：{{ account.reason }}</p><p>预检未登录账号或发送游戏消息。</p><details><summary>查看预检结果</summary><pre>{{ JSON.stringify(validation, null, 2) }}</pre></details></div>
  <div class="submit-bar"><p>提交后可关闭页面，任务在后台继续运行。</p><div class="button-row"><button type="button" class="button secondary" :disabled="!canSubmit" @click="validate">{{ busy === 'validate' ? '检查中…' : '预检任务' }}</button><button type="button" class="button primary" :disabled="!canSubmit" @click="submit">{{ busy === 'submit' ? '提交中…' : '提交任务' }}</button></div></div>
</template>
