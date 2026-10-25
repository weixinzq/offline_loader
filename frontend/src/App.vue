<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import TaskComposer from './components/TaskComposer.vue'
import TaskDetail from './components/TaskDetail.vue'
import AccountPagination from './components/AccountPagination.vue'
import EgressGroups from './components/EgressGroups.vue'
import { useAccountPagination } from './accountPagination'
import { request, send } from './api'
import { accountBody, isActive, label, time, tone } from './presentation'
import type { Account, AccountImportResult, EgressGroup, LogEntry, Script, Session, TaskClearResult, TaskRecord } from './types'

type Page = 'accounts' | 'sessions' | 'tasks' | 'compose' | 'egress'
const page = ref<Page>('accounts')
const booting = ref(true)
const error = ref('')
const success = ref('')
const refreshing = ref(false)
const action = ref('')
const streamState = ref('connecting')
const accounts = ref<Account[]>([])
const sessions = ref<Session[]>([])
const scripts = ref<Script[]>([])
const tasks = ref<TaskRecord[]>([])
const cancellingIds = ref<string[]>([])
const clearedTaskIds = new Set<string>()
const groups = ref<EgressGroup[]>([])
const assignment = ref('__choose__')
const groupsById = computed(() => new Map(groups.value.map(group => [group.group_id, group.name])))
const groupOf = (account: Account) => account.egress_group_id ? groupsById.value.get(account.egress_group_id) ?? '未知分组' : '默认系统出口'
const selectedIds = ref<string[]>([])
const selectedSet = computed(() => new Set(selectedIds.value))
const { accountPage, accountPages, visibleAccounts } = useAccountPagination(accounts)
const selectedTask = ref<TaskRecord | null>(null)
const logs = ref<LogEntry[]>([])
const detailLoading = ref(false)
const editAccount = ref<Account | null | undefined>(undefined)
const form = ref({ label: '', account: '', password: '', char_id: 0, zone_index: 1 })
const formError = ref('')
const importReport = ref<AccountImportResult | null>(null)
const importReportText = computed(() => JSON.stringify(importReport.value, null, 2))
const moreLogs = ref(false)
const logLoading = ref(false)
let stream: EventSource | undefined
let refreshTimer: ReturnType<typeof setTimeout> | undefined
let eventTimer: ReturnType<typeof setTimeout> | undefined
const pendingSessions = new Map<string, Session>()
const pendingTasks = new Map<string, TaskRecord>()
let pendingLogs: LogEntry[] = []
let resyncTimer: ReturnType<typeof setInterval> | undefined
let detailSequence = 0
let selectedTaskId: string | null = null
let refreshPending = false
let unmounted = false

const readyCount = computed(() => sessions.value.filter(s => s.state === 'ready').length)
const activeCount = computed(() => tasks.value.filter(t => isActive(t.state)).length)
const finishedCount = computed(() => tasks.value.filter(t => !isActive(t.state)).length)
const allSelected = computed(() => accounts.value.length > 0 && selectedIds.value.length === accounts.value.length)
const sessionsById = computed(() => new Map(sessions.value.map(session => [session.account_id, session])))
const sessionOf = (id: string) => sessionsById.value.get(id)
const stateOf = (id: string) => sessionOf(id)?.state ?? 'offline'
const taskKind = (task: TaskRecord) => label(task.kind)

function clearPendingEvents() {
  if (eventTimer) clearTimeout(eventTimer)
  eventTimer = undefined
  pendingSessions.clear(); pendingTasks.clear(); pendingLogs = []
}
function fail(cause: unknown) {
  error.value = cause instanceof Error ? cause.message : '操作失败，请重试'
}
async function loadDetail(taskId: string, quiet = false) {
  selectedTaskId = taskId
  const sequence = ++detailSequence
  if (!quiet) detailLoading.value = true
  try {
    const [task, entries] = await Promise.all([
      request<TaskRecord>(`/tasks/${encodeURIComponent(taskId)}`),
      request<LogEntry[]>(`/tasks/${encodeURIComponent(taskId)}/logs?after=${quiet ? logs.value.at(-1)?.id ?? 0 : 0}&limit=250`),
    ])
    if (sequence === detailSequence && !unmounted) {
      selectedTask.value = task
      if (quiet) {
        const existing = new Set(logs.value.map(entry => entry.id))
        logs.value.push(...entries.filter(entry => !existing.has(entry.id)))
      } else logs.value = entries
      if (logs.value.length > 2000) logs.value.splice(0, logs.value.length - 2000)
      moreLogs.value = entries.length === 250
    }
  } catch (cause) { if (sequence === detailSequence && !unmounted) fail(cause) }
  finally { if (sequence === detailSequence) detailLoading.value = false }
}
async function refresh() {
  if (unmounted) return
  if (refreshing.value) { refreshPending = true; return }
  refreshing.value = true
  try {
    const [accountData, sessionData, taskData, scriptData, groupData] = await Promise.all([
      request<Account[]>('/accounts'), request<Session[]>('/sessions'), request<TaskRecord[]>('/tasks'), request<Script[]>('/scripts'),
      request<EgressGroup[]>('/egress-groups'),
    ])
    if (unmounted) return
    accounts.value = accountData; sessions.value = sessionData
    tasks.value = taskData.filter(task => !clearedTaskIds.has(task.task_id)); scripts.value = scriptData
    groups.value = groupData
    const accountIds = new Set(accountData.map(account => account.account_id))
    selectedIds.value = selectedIds.value.filter(id => accountIds.has(id))
    if (selectedTaskId) await loadDetail(selectedTaskId, true)
  } catch (cause) { if (!unmounted) fail(cause) } finally {
    refreshing.value = false
    if (refreshPending) { refreshPending = false; scheduleRefresh() }
  }
}
function scheduleRefresh() {
  if (refreshTimer || unmounted) return
  refreshTimer = setTimeout(() => { refreshTimer = undefined; void refresh() }, 200)
}
function flushEvents() {
  eventTimer = undefined
  if (unmounted) { clearPendingEvents(); return }
  if (pendingSessions.size) {
    const known = new Set(sessions.value.map(session => session.account_id))
    sessions.value = sessions.value.map(session => pendingSessions.get(session.account_id) ?? session)
      .concat([...pendingSessions.values()].filter(session => !known.has(session.account_id)))
    pendingSessions.clear()
  }
  if (pendingTasks.size) {
    const known = new Set(tasks.value.map(task => task.task_id))
    tasks.value = [...pendingTasks.values()].filter(task => !known.has(task.task_id))
      .concat(tasks.value.map(task => pendingTasks.get(task.task_id) ?? task))
    if (selectedTaskId && pendingTasks.has(selectedTaskId)) selectedTask.value = pendingTasks.get(selectedTaskId)!
    pendingTasks.clear()
  }
  if (pendingLogs.length && !moreLogs.value) {
    const known = new Set(logs.value.map(entry => entry.id))
    const entries = pendingLogs.filter(entry => {
      if (entry.task_id !== selectedTask.value?.task_id || known.has(entry.id)) return false
      known.add(entry.id)
      return true
    })
    if (entries.length) logs.value = [...logs.value, ...entries].slice(-2000)
  }
  pendingLogs = []
}
function handleEvent(event: MessageEvent<string>) {
  if (unmounted) return
  try {
    const envelope = JSON.parse(event.data)
    const kind = String(envelope.event || event.type).split('.')[0]
    if (kind === 'session' && envelope.data?.state && envelope.data?.account_id) {
      const session = envelope.data as Session
      pendingSessions.set(session.account_id, session)
    } else if (kind === 'task' && Array.isArray(envelope.data?.deleted_task_ids)) {
      removeClearedTasks(envelope.data.deleted_task_ids)
    } else if (kind === 'task' && envelope.data?.state && envelope.data?.task_id) {
      const task = envelope.data as TaskRecord
      if (!clearedTaskIds.has(task.task_id)) pendingTasks.set(task.task_id, task)
    } else if ((kind === 'log' || envelope.event === 'task.log') && envelope.data?.id) {
      const entry = envelope.data as LogEntry
      if (entry.task_id === selectedTaskId) {
        // Preserve ordered pagination: a live entry after a gap is fetched with the next page.
        if (!moreLogs.value) pendingLogs.push(entry)
        if (pendingLogs.length > 2000) pendingLogs = pendingLogs.slice(-2000)
      }
    } else if (kind === 'account' || kind === 'egress') scheduleRefresh()
    if (!eventTimer && (pendingSessions.size || pendingTasks.size || pendingLogs.length)) {
      eventTimer = setTimeout(flushEvents, 200)
    }
  } catch { scheduleRefresh() }
}
function connectEvents() {
  if (unmounted) return
  stream?.close(); streamState.value = 'connecting'
  stream = new EventSource('/api/v1/events', { withCredentials: true })
  stream.onopen = () => { streamState.value = 'connected'; void refresh() }
  stream.onerror = () => { streamState.value = 'reconnecting' }
  stream.onmessage = handleEvent
  for (const name of ['session', 'task', 'log', 'account', 'session.updated', 'task.updated', 'task.log']) {
    stream.addEventListener(name, handleEvent as EventListener)
  }
  if (!resyncTimer) resyncTimer = setInterval(() => {
    if (document.visibilityState === 'visible') void refresh()
  }, 30000)
}
function toggleAll(event: Event) {
  selectedIds.value = (event.target as HTMLInputElement).checked ? accounts.value.map(a => a.account_id) : []
}
function toggleAccount(accountId: string, event: Event) {
  const next = new Set(selectedIds.value)
  if ((event.target as HTMLInputElement).checked) next.add(accountId); else next.delete(accountId)
  selectedIds.value = [...next]
}
function openAccount(account: Account | null) {
  if (action.value) return
  editAccount.value = account; formError.value = ''
  form.value = account ? { label: account.label, account: account.account, password: '', char_id: account.char_id, zone_index: account.zone_index }
    : { label: '', account: '', password: '', char_id: 0, zone_index: 1 }
}
function closeAccount() { editAccount.value = undefined; form.value.password = '' }
async function saveAccount() {
  action.value = 'save'; formError.value = ''
  try {
    const editing = !!editAccount.value
    await send(editing ? `/accounts/${encodeURIComponent(editAccount.value!.account_id)}` : '/accounts', accountBody(form.value, editing), editing ? 'PATCH' : 'POST')
    closeAccount(); success.value = editing ? '账号已保存。' : '账号已添加。'; await refresh()
  } catch (cause) { formError.value = cause instanceof Error ? cause.message : '保存失败'; fail(cause) }
  finally { action.value = '' }
}
async function removeAccount(account: Account) {
  if (!window.confirm(`删除账号“${account.label || account.account}”？此操作只删除后台账号配置。`)) return
  action.value = account.account_id
  try { await request(`/accounts/${encodeURIComponent(account.account_id)}`, { method: 'DELETE' }); await refresh() }
  catch (cause) { fail(cause) } finally { action.value = '' }
}
async function importAccounts(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  action.value = 'import'; importReport.value = null
  try { importReport.value = await send<AccountImportResult>('/accounts/import', { config: JSON.parse(await file.text()) }); await refresh() }
  catch (cause) { fail(cause) } finally { action.value = ''; input.value = '' }
}
async function sessionAction(operation: 'connect' | 'reconnect' | 'disconnect') {
  action.value = operation; error.value = ''
  try {
    const result = await send<TaskRecord>(`/sessions/${operation}`, { account_ids: selectedIds.value, concurrency: 5 })
    await taskCreated(result.task_id)
  } catch (cause) { fail(cause) } finally { action.value = '' }
}
async function taskCreated(taskId: string) {
  page.value = 'tasks'; success.value = '任务已提交，后台将继续执行。'; await refresh(); await loadDetail(taskId)
}
async function cancelTask(taskId: string) {
  if (cancellingIds.value.includes(taskId)) return
  cancellingIds.value.push(taskId)
  try { await send(`/tasks/${encodeURIComponent(taskId)}/cancel`); await refresh() }
  catch (cause) { fail(cause) }
  finally { cancellingIds.value = cancellingIds.value.filter(id => id !== taskId) }
}
function removeClearedTasks(taskIds: string[]) {
  for (const id of taskIds) { clearedTaskIds.add(id); pendingTasks.delete(id) }
  tasks.value = tasks.value.filter(task => !clearedTaskIds.has(task.task_id))
  pendingLogs = pendingLogs.filter(entry => !clearedTaskIds.has(entry.task_id ?? ''))
  if (selectedTaskId && clearedTaskIds.has(selectedTaskId)) closeDetail()
}
async function clearFinishedTasks() {
  action.value = 'clear'; error.value = ''
  try {
    const result = await request<TaskClearResult>('/tasks', { method: 'DELETE' })
    removeClearedTasks(result.task_ids)
    success.value = `已清除 ${result.deleted_count} 条已结束任务记录。`
    await refresh()
  } catch (cause) { fail(cause) } finally { action.value = '' }
}
async function assignGroup() {
  if (assignment.value === '__choose__' || !selectedIds.value.length) return
  action.value = 'assign'
  try {
    await send('/accounts/assign-egress', { account_ids: selectedIds.value, egress_group_id: assignment.value || null })
    success.value = `已为 ${selectedIds.value.length} 个账号更新出口分组`
    assignment.value = '__choose__'; await refresh()
  } catch (cause) { fail(cause) } finally { action.value = '' }
}
function closeDetail() { ++detailSequence; selectedTaskId = null; selectedTask.value = null; detailLoading.value = false; logs.value = []; pendingLogs = [] }
async function loadMoreLogs() {
  if (!selectedTask.value || logLoading.value) return
  logLoading.value = true
  const taskId = selectedTask.value.task_id
  const sequence = detailSequence
  const after = logs.value.at(-1)?.id ?? 0
  try {
    const entries = await request<LogEntry[]>(`/tasks/${encodeURIComponent(taskId)}/logs?after=${after}&limit=250`)
    if (sequence === detailSequence && !unmounted) {
      const existing = new Set(logs.value.map(entry => entry.id))
      logs.value.push(...entries.filter(entry => !existing.has(entry.id)))
      if (logs.value.length > 2000) logs.value.splice(0, logs.value.length - 2000)
      moreLogs.value = entries.length === 250
    }
  } catch (cause) { fail(cause) } finally { logLoading.value = false }
}
function visibilityRefresh() { if (document.visibilityState === 'visible') void refresh() }
onMounted(async () => {
  document.addEventListener('visibilitychange', visibilityRefresh)
  try {
    await refresh()
    if (!unmounted) connectEvents()
  }
  finally { booting.value = false }
})
onBeforeUnmount(() => { unmounted = true; ++detailSequence; stream?.close(); clearPendingEvents(); if (refreshTimer) clearTimeout(refreshTimer); if (resyncTimer) clearInterval(resyncTimer); document.removeEventListener('visibilitychange', visibilityRefresh) })
</script>

<template>
  <div v-if="booting" class="boot-screen"><span class="brand-mark">A</span><p>正在连接工作台…</p></div>
  <div v-else class="workspace">
    <aside class="sidebar"><a href="#" class="brand" @click.prevent="page = 'accounts'"><span class="brand-mark">A</span><span>奥拉工作台<small>AOLA WORKSPACE</small></span></a><p class="nav-label">工作空间</p><nav><button :class="{ active: page === 'accounts' }" @click="page = 'accounts'"><span>▤</span>账号管理<small>{{ accounts.length }}</small></button><button :class="{ active: page === 'sessions' }" @click="page = 'sessions'"><span>◉</span>在线会话<small>{{ readyCount }}</small></button><button :class="{ active: page === 'tasks' }" @click="page = 'tasks'"><span>☷</span>任务记录<small v-if="activeCount">{{ activeCount }}</small></button><button :class="{ active: page === 'compose' }" @click="page = 'compose'"><span>＋</span>编排任务</button><button :class="{ active: page === 'egress' }" @click="page = 'egress'"><span>⇄</span>出口分组<small>{{ groups.length }}</small></button></nav><div class="sidebar-bottom"><div class="connection-status"><i :class="{ live: streamState === 'connected' }"></i>{{ streamState === 'connected' ? '实时更新已连接' : streamState === 'reconnecting' ? '连接恢复中…' : '正在连接实时更新…' }}</div></div></aside>
    <div class="main-shell"><header class="topbar"><span>统一后台 <span class="separator">/</span> {{ { accounts: '账号管理', sessions: '在线会话', tasks: '任务记录', compose: '编排任务', egress: '出口分组' }[page] }}</span><button class="text-button" :disabled="refreshing" @click="refresh">{{ refreshing ? '刷新中…' : '↻ 刷新' }}</button></header><main class="main-content">
      <div v-if="error" class="notice error global-notice" role="alert"><span>{{ error }}</span><button class="icon-button" aria-label="关闭错误提示" @click="error = ''">×</button></div><div v-if="success" class="notice success global-notice" role="status"><span>{{ success }}</span><button class="icon-button" aria-label="关闭成功提示" @click="success = ''">×</button></div>
      <template v-if="page === 'accounts' || page === 'sessions'">
        <div class="page-heading"><div><p class="eyebrow">{{ page === 'accounts' ? 'ACCOUNTS' : 'SESSIONS' }}</p><h1>{{ page === 'accounts' ? '账号管理' : '在线会话' }}</h1><p>{{ page === 'accounts' ? '维护账号配置，并选择需要操作的账号。' : '查看后台连接状态，管理常驻会话。' }}</p></div><div v-if="page === 'accounts'" class="button-row"><label class="button secondary file-button" :class="{ disabled: !!action }">导入配置<input type="file" accept=".json" :disabled="!!action" @change="importAccounts" /></label><button class="button primary" @click="openAccount(null)">＋ 添加账号</button></div></div>
        <div class="metrics"><div><span>账号总数</span><strong>{{ accounts.length }}<small>个账号</small></strong></div><div><span>就绪会话</span><strong>{{ readyCount }}<small> 个就绪会话</small></strong></div><div><span>进行中任务</span><strong>{{ activeCount }}<small>项任务</small></strong></div></div>
        <div v-if="importReport" class="notice success"><strong>配置导入完成</strong><details><summary>查看转换报告</summary><pre>{{ importReportText }}</pre></details><p class="help">本机原文件保持不变。</p></div>
        <section class="panel accounts-panel"><div class="panel-heading"><h2>{{ page === 'accounts' ? '全部账号' : '会话列表' }}</h2><span class="muted">已选 {{ selectedIds.length }} 个</span></div><div class="selection-actions"><div class="button-row"><button class="text-button" :disabled="!readyCount" @click="selectedIds = accounts.filter(a => stateOf(a.account_id) === 'ready').map(a => a.account_id)">选择就绪账号</button><button class="button secondary small" :disabled="!selectedIds.length || !!action" @click="sessionAction('connect')">连接</button><button class="button secondary small" :disabled="!selectedIds.length || !!action" @click="sessionAction('reconnect')">重连</button><button class="button secondary small" :disabled="!selectedIds.length || !!action" @click="sessionAction('disconnect')">断开</button></div><button class="text-button" :disabled="!selectedIds.length" @click="page = 'compose'">为所选账号编排任务 →</button></div>
          <div v-if="page === 'accounts'" class="selection-actions"><div class="button-row"><select v-model="assignment" aria-label="选择出口分组" :disabled="!!action"><option value="__choose__" disabled>选择出口分组</option><option value="">默认系统出口（移出分组）</option><option v-for="group in groups" :key="group.group_id" :value="group.group_id">{{ group.name }} · 上限 {{ group.max_online }}</option></select><button class="button secondary small" :disabled="!selectedIds.length || assignment === '__choose__' || !!action" @click="assignGroup">分配所选账号</button></div><span class="help">仅离线且未被任务占用的账号可调整出口</span></div>
          <AccountPagination v-model:page="accountPage" :pages="accountPages" :total="accounts.length" />
          <div class="table-scroll"><table><thead><tr><th class="checkbox-cell"><input type="checkbox" :checked="allSelected" aria-label="选择全部账号" @change="toggleAll" /></th><th>账号</th><th>角色 / 区服</th><th>会话状态</th><th v-if="page === 'accounts'" class="align-right">操作</th></tr></thead><tbody><tr v-for="account in visibleAccounts" :key="account.account_id" :class="{ selected: selectedSet.has(account.account_id) }"><td><input type="checkbox" :checked="selectedSet.has(account.account_id)" @change="toggleAccount(account.account_id, $event)" :value="account.account_id" :aria-label="`选择账号 ${account.label || account.account}`" /></td><td><strong>{{ account.label || account.account }}</strong><small>{{ account.account }}</small><small>{{ groupOf(account) }}</small></td><td>{{ account.char_id }}<span class="muted"> / {{ account.zone_index }}</span></td><td><span class="badge" :class="tone(stateOf(account.account_id))"><i></i>{{ label(stateOf(account.account_id)) }}</span><small v-if="sessionOf(account.account_id)?.error" class="danger-text">{{ sessionOf(account.account_id)?.error }}</small></td><td v-if="page === 'accounts'" class="align-right"><div class="row-actions"><button class="text-button" @click="openAccount(account)">编辑</button><button class="text-button danger-text" :disabled="!!action" @click="removeAccount(account)">删除</button></div></td></tr><tr v-if="!accounts.length"><td :colspan="page === 'accounts' ? 5 : 4" class="empty"><span class="empty-icon">▤</span><h3>从一个账号开始</h3><p>添加账号，或导入已有配置。</p><button class="button secondary" @click="openAccount(null)">添加账号</button></td></tr></tbody></table></div>
        </section><p class="page-footnote">会话由后台持有，关闭此页面不会断开连接。</p>
      </template>
      <EgressGroups v-else-if="page === 'egress'" :groups="groups" @changed="refresh" @failure="fail" />
      <TaskComposer v-else-if="page === 'compose'" :accounts="accounts" :sessions="sessions" :scripts="scripts" :selected-ids="selectedIds" @created="taskCreated" @failure="fail" />
      <template v-else-if="page === 'tasks'">
        <div class="page-heading"><div><p class="eyebrow">TASKS</p><h1>任务记录</h1><p>查看执行进度、逐账号结果与后台日志。</p></div><div class="button-row"><button class="button secondary" :disabled="!finishedCount || !!action" @click="clearFinishedTasks">{{ action === 'clear' ? '清除中…' : '清除已结束任务' }}</button><button class="button primary" @click="page = 'compose'">＋ 编排任务</button></div></div>
        <section class="panel"><div class="panel-heading"><h2>全部任务</h2><span class="muted">{{ tasks.length }} 项</span></div><div class="table-scroll"><table><thead><tr><th>任务</th><th>状态</th><th>创建时间</th><th>账号数</th><th></th></tr></thead><tbody><tr v-for="task in tasks" :key="task.task_id" :class="{ selected: selectedTask?.task_id === task.task_id }"><td><strong>{{ taskKind(task) }}</strong><small class="mono">{{ task.task_id.slice(0, 12) }}</small></td><td><span class="badge" :class="task.state === 'succeeded' && task.account_results.some(row => row.skipped_repetitions) ? 'working' : tone(task.state)">{{ task.state === 'succeeded' && task.account_results.some(row => row.skipped_repetitions) ? '完成，含跳过' : label(task.state) }}</span></td><td>{{ time(task.created_at) }}</td><td>{{ task.account_results.length }}</td><td class="align-right"><div class="button-row task-row-actions"><button v-if="isActive(task.state)" class="button danger small" :disabled="cancellingIds.includes(task.task_id) || task.state === 'cancelling' && task.error?.code !== 'cleanup_failed'" @click="cancelTask(task.task_id)">{{ cancellingIds.includes(task.task_id) ? '取消中…' : task.error?.code === 'cleanup_failed' ? '重试取消' : task.state === 'cancelling' ? '取消中…' : '取消任务' }}</button><button class="text-button" @click="loadDetail(task.task_id)">查看详情 →</button></div></td></tr><tr v-if="!tasks.length"><td colspan="5" class="empty"><h3>还没有任务</h3><p>连接账号或编排执行步骤后，记录会显示在这里。</p></td></tr></tbody></table></div></section>
        <p v-if="detailLoading" class="muted loading-note" role="status">正在读取任务详情…</p><TaskDetail v-if="selectedTask" :task="selectedTask" :logs="logs" :more-logs="moreLogs" :log-loading="logLoading" @more-logs="loadMoreLogs" :accounts="accounts" :cancelling="cancellingIds.includes(selectedTask.task_id)" @close="closeDetail" @cancel="cancelTask(selectedTask.task_id)" />
      </template>
    </main></div>
    <div v-if="editAccount !== undefined" class="modal-backdrop" @click.self="closeAccount"><section class="modal panel" role="dialog" aria-modal="true" aria-labelledby="account-dialog-title"><div class="panel-heading"><h2 id="account-dialog-title">{{ editAccount ? '编辑账号' : '添加账号' }}</h2><button class="icon-button" aria-label="关闭账号编辑" @click="closeAccount">×</button></div><form @submit.prevent="saveAccount"><label class="field">备注名称<input v-model="form.label" required maxlength="120" autocomplete="off" placeholder="例如：主账号" /></label><label class="field">登录账号<input v-model="form.account" required autocomplete="off" /></label><label class="field">{{ editAccount ? '新密码（留空保留原密码）' : '登录密码' }}<input v-model="form.password" type="password" :required="!editAccount" autocomplete="new-password" /></label><div class="field-row"><label class="field">角色 ID<input v-model.number="form.char_id" type="number" min="0" required /></label><label class="field">区服序号<input v-model.number="form.zone_index" type="number" min="0" required /></label></div><p v-if="formError" class="danger-text" role="alert">{{ formError }}</p><div class="modal-actions"><button type="button" class="button secondary" :disabled="action === 'save'" @click="closeAccount">取消</button><button type="submit" class="button primary" :disabled="action === 'save'">{{ action === 'save' ? '保存中…' : '保存账号' }}</button></div></form></section></div>
  </div>
</template>

<style scoped>
.task-row-actions { justify-content: flex-end; white-space: nowrap; }
</style>
