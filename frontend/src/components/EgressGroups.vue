<script setup lang="ts">
import { computed, ref } from 'vue'
import { request, send } from '../api'
import { time } from '../presentation'
import type { EgressGroup, EgressRules } from '../types'

const props = defineProps<{ groups: EgressGroup[] }>()
const emit = defineEmits<{ changed: []; failure: [error: unknown] }>()
const editing = ref<string | null>(null)
const form = ref({ name: '', clash_policy: '', max_online: 30, routing_confirmed: false })
const busy = ref('')
const rules = ref<EgressRules | null>(null)
const duplicateIps = computed(() => {
  const counts = new Map<string, number>()
  props.groups.forEach(group => { if (group.exit_ip) counts.set(group.exit_ip, (counts.get(group.exit_ip) ?? 0) + 1) })
  return new Set([...counts].filter(([, count]) => count > 1).map(([ip]) => ip))
})
const state = (value: string) => ({ offline: '未启动', starting: '启动中', ready: '运行中', failed: '进程异常' }[value] ?? value)
function edit(group?: EgressGroup) {
  editing.value = group?.group_id ?? null
  form.value = group ? { name: group.name, clash_policy: group.clash_policy, max_online: group.max_online,
    routing_confirmed: group.routing_confirmed } : { name: '', clash_policy: '', max_online: 30, routing_confirmed: false }
}
async function save() {
  busy.value = 'save'
  try {
    await send(editing.value ? `/egress-groups/${editing.value}` : '/egress-groups', form.value, editing.value ? 'PUT' : 'POST')
    edit(); rules.value = null; emit('changed')
  } catch (error) { emit('failure', error) } finally { busy.value = '' }
}
async function probe(group: EgressGroup) {
  busy.value = group.group_id
  try { await send(`/egress-groups/${group.group_id}/probe`); emit('changed') }
  catch (error) { emit('failure', error) } finally { busy.value = '' }
}
async function remove(group: EgressGroup) {
  if (!window.confirm(`删除出口分组“${group.name}”？请先将账号移出此分组。`)) return
  busy.value = group.group_id
  try { await send(`/egress-groups/${group.group_id}`, undefined, 'DELETE'); rules.value = null; emit('changed'); if (editing.value === group.group_id) edit() }
  catch (error) { emit('failure', error) } finally { busy.value = '' }
}
async function exportRules() {
  busy.value = 'rules'
  try { rules.value = await request<EgressRules>('/egress-groups/rules') }
  catch (error) { emit('failure', error) } finally { busy.value = '' }
}
</script>

<template>
  <div class="page-heading"><div><p class="eyebrow">OUTBOUND GROUPS</p><h1>出口分组</h1><p>每组使用独立执行进程，通过 Clash TUN 分配网络出口。</p></div><button class="button secondary" :disabled="!!busy || !groups.length" @click="exportRules">生成 Clash 规则</button></div>
  <div class="notice"><strong>设置顺序</strong><p>创建分组 → 将生成的进程规则合并到 Clash 并生效 → 检测出口 → 编辑分组确认规则 → 在账号管理中分配账号。</p><p class="help">策略名称填写 Clash 中已有的节点或策略组。不同分组应使用不同出口 IP；所有组共用后台 100 个在线名额。检测仅访问公网 IP 服务，不登录游戏。</p></div>
  <section v-if="rules" class="panel rules-panel"><div class="panel-heading"><h2>Clash 规则片段</h2><button class="text-button" @click="rules = null">收起</button></div><p>{{ rules.instructions }}</p><textarea :value="rules.yaml" readonly rows="8" aria-label="Clash 规则片段" spellcheck="false"></textarea><p class="help">保持 TUN 开启并使用规则模式。导入到 Clash Verge 的持久覆写配置，避免订阅更新覆盖。此处不会自动修改 Clash。</p></section>
  <section class="panel"><div class="panel-heading"><h2>{{ editing ? '编辑分组' : '创建分组' }}</h2><button v-if="editing" class="text-button" :disabled="!!busy" @click="edit()">取消编辑</button></div><form class="group-form" @submit.prevent="save">
    <div class="field-row"><label class="field">分组名称<input v-model="form.name" required maxlength="80" placeholder="例如：出口一" /></label><label class="field">Clash 节点或策略名称<input v-model="form.clash_policy" required maxlength="120" placeholder="与 Clash 中名称完全一致" /></label><label class="field capacity">本组在线上限<input v-model.number="form.max_online" required type="number" min="1" max="100" /></label></div>
    <label v-if="editing" class="confirmation"><input v-model="form.routing_confirmed" type="checkbox" />已在 Clash 中启用本组进程规则，并核对出口</label><p v-if="editing" class="help">修改策略名称后，需要更新规则并重新确认。在线或任务占用中的分组不可修改。</p>
    <button class="button primary" :disabled="!!busy">{{ busy === 'save' ? '保存中…' : editing ? '保存分组' : '创建分组' }}</button>
  </form></section>
  <section class="panel"><div class="panel-heading"><h2>全部出口</h2><span class="muted">{{ groups.length }} 个分组</span></div>
    <div v-if="!groups.length" class="empty"><h3>尚未创建出口分组</h3><p>未分组账号继续使用默认系统出口。</p></div>
    <article v-for="group in groups" :key="group.group_id" class="group-item">
      <div class="group-title"><div><h3>{{ group.name }} <span class="badge" :class="group.routing_confirmed ? 'success' : 'neutral'">{{ group.routing_confirmed ? '规则已确认' : '等待确认规则' }}</span></h3><p class="muted">{{ group.clash_policy }} · 在线 / 连接中 {{ group.online_count }} / {{ group.max_online }} · 预留 {{ group.reserved_count }}</p></div><div class="button-row"><button class="button secondary small" :disabled="!!busy" @click="probe(group)">{{ busy === group.group_id ? '处理中…' : '检测出口' }}</button><button class="text-button" :disabled="!!busy" @click="edit(group)">编辑</button><button class="text-button danger-text" :disabled="!!busy" @click="remove(group)">删除</button></div></div>
      <p>出口 IP：<strong>{{ group.exit_ip || '尚未检测' }}</strong><span class="muted"> · {{ time(group.exit_checked_at ?? undefined) }}</span></p><p v-if="group.exit_ip && duplicateIps.has(group.exit_ip)" class="danger-text">多个分组检测到相同出口 IP，请核对 Clash 规则与策略选择。</p>
      <p class="help">{{ state(group.worker_state) }}<span v-if="group.worker_pid"> · 进程 {{ group.worker_pid }}</span></p><code class="worker-path">{{ group.worker_path }}</code><p v-if="group.worker_error" class="danger-text">{{ group.worker_error }}</p>
    </article>
  </section>
</template>

<style scoped>
.group-form,.rules-panel>p,.rules-panel>textarea{margin:20px 28px}.group-form .field-row{align-items:start;grid-template-columns:minmax(0,1fr) minmax(0,1fr) 150px}.capacity{max-width:150px}.confirmation{display:flex;gap:10px;align-items:center;margin-bottom:12px}.group-form>.button{margin-top:12px}.group-item{padding:24px 28px;border-top:1px solid var(--line,#e7ebf3)}.group-title{display:flex;justify-content:space-between;gap:16px}.group-title h3{margin:0}.group-item p{margin:10px 0}.worker-path{display:block;overflow-wrap:anywhere;font-size:12px;color:#71809c}.rules-panel>textarea{width:calc(100% - 56px);font-family:monospace;resize:vertical}.group-title .badge{margin-left:8px}.panel{margin-bottom:20px}@media(max-width:800px){.group-title{flex-direction:column}.capacity{max-width:none}.group-form .field-row{grid-template-columns:1fr}}
</style>
