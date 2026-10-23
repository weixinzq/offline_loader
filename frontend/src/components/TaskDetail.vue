<script setup lang="ts">
import { computed } from 'vue'
import { isActive, label, time, tone } from '../presentation'
import type { Account, LogEntry, TaskRecord } from '../types'

const props = defineProps<{ task: TaskRecord; logs: LogEntry[]; accounts: Account[]; cancelling: boolean; moreLogs?: boolean; logLoading?: boolean }>()
defineEmits<{ close: []; cancel: []; moreLogs: [] }>()
const accountNames = computed(() => new Map(props.accounts.map(account => [account.account_id, account.label || account.account])))
const name = (id?: string | null) => id ? accountNames.value.get(id) || id : '服务'
const visibleLogs = computed(() => props.logs.slice(-200))
const completed = computed(() => props.task.account_results.filter(a => a.state === 'succeeded').length)
const recovered = computed(() => props.task.account_results.reduce((n, a) => n + (a.skipped_repetitions ?? 0), 0))
const skipped = computed(() => props.task.account_results.filter(a => a.state === 'skipped'))
</script>

<template>
  <section class="panel task-detail" aria-label="任务详情">
    <div class="panel-heading"><div><p class="eyebrow">TASK DETAIL</p><h2>任务详情 <span class="badge" :class="task.state === 'succeeded' && recovered ? 'working' : tone(task.state)">{{ task.state === 'succeeded' && recovered ? '完成，含跳过' : label(task.state) }}</span></h2></div><button class="icon-button" aria-label="关闭任务详情" @click="$emit('close')">×</button></div>
    <div class="detail-summary"><div><span>任务编号</span><code>{{ task.task_id }}</code></div><div><span>创建时间</span><strong>{{ time(task.created_at) }}</strong></div><div><span>账号完成</span><strong>{{ completed }} / {{ task.account_results.length }}</strong></div><button v-if="isActive(task.state)" class="button danger" :disabled="cancelling || task.state === 'cancelling'" @click="$emit('cancel')">{{ cancelling || task.state === 'cancelling' ? '正在取消与清理…' : '取消任务' }}</button></div>
    <div v-if="task.error" class="notice error" role="alert"><strong>{{ label(task.error.code) }}</strong> · {{ task.error.message }}</div>
    <div v-if="skipped.length" class="notice" role="status"><strong>{{ skipped.length }} 个账号已跳过</strong><p>{{ skipped.map(result => name(result.account_id)).join('、') }}。其余账号的执行结果见下表。</p></div>
    <h3>逐账号结果</h3>
    <p class="help">本地网络写入与服务器业务确认分别展示；未知表示尚无业务确认。</p>
    <div class="table-scroll"><table><thead><tr><th>账号</th><th>执行结果</th><th>轮次：尝试 / 正常 / 跳过</th><th>本地网络写入</th><th>服务器业务确认</th></tr></thead><tbody><tr v-for="result in task.account_results" :key="result.account_id"><td><strong>{{ name(result.account_id) }}</strong><small v-if="result.error" class="danger-text">{{ result.error.message }}</small></td><td><span class="badge" :class="result.state === 'succeeded' && result.skipped_repetitions ? 'working' : tone(result.state)">{{ result.state === 'succeeded' && result.skipped_repetitions ? '完成，含跳过' : label(result.state) }}</span></td><td>{{ result.attempted_repetitions ?? result.completed_repetitions }} / {{ result.completed_repetitions }} / {{ result.skipped_repetitions ?? 0 }}</td><td>{{ result.network_write.count === null ? '次数未知' : `${result.network_write.count} 次` }}<span class="muted"> · {{ result.network_write.status === 'succeeded' ? '已写入' : label(result.network_write.status) }}</span></td><td>{{ label(result.business_confirmation) }}</td></tr><tr v-if="!task.account_results.length"><td colspan="5" class="empty">暂时没有账号结果</td></tr></tbody></table></div>
    <h3 class="log-heading">运行日志 <span class="muted">已载入 {{ logs.length }} 条，显示最近 {{ visibleLogs.length }} 条，最多显示 200 条；实时更新间隔 0.2 秒</span> <button v-if="moreLogs" class="text-button" :disabled="logLoading" @click="$emit('moreLogs')">{{ logLoading ? '读取中…' : '加载更多' }}</button></h3>
    <div class="log-view" role="log" aria-label="任务运行日志"><div v-for="entry in visibleLogs" :key="entry.id" class="log-line"><time>{{ time(entry.timestamp) }}</time><span class="log-account">{{ name(entry.account_id) }}</span><span>{{ entry.message }}</span></div><p v-if="!logs.length" class="muted">暂无日志</p></div>
  </section>
</template>

