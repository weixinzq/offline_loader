<script setup lang="ts">
import { computed, ref } from 'vue'
import { request, send } from '../api'
import { time } from '../presentation'
import type { EgressGroup, EgressRules } from '../types'
const props = withDefaults(defineProps<{ groups: EgressGroup[]; waitingCount?: number }>(), { waitingCount: 0 })
const emit = defineEmits<{ changed: []; failure: [error: unknown] }>()
const editing = ref<string | null>(null)
const direct = ref('proxy')
const form = ref({ name: '', clash_policy: '', routing_confirmed: false, enabled: false })
const busy = ref('')
const rules = ref<EgressRules | null>(null)
const capacity = computed(() => props.groups.filter(g => g.active).length * 25)
const occupied = computed(() => props.groups.reduce((sum, g) => sum + g.online_count, 0))
function edit(group?: EgressGroup) {
  editing.value = group?.group_id ?? null; direct.value = group?.clash_policy === 'DIRECT' ? 'direct' : 'proxy'
  form.value = group ? { name: group.name, clash_policy: group.clash_policy, routing_confirmed: group.routing_confirmed, enabled: group.enabled }
    : { name: '', clash_policy: '', routing_confirmed: false, enabled: false }
}
async function save() {
  busy.value = 'save'
  try {
    await send(editing.value ? `/egress-groups/${editing.value}` : '/egress-groups', { ...form.value, clash_policy: direct.value === 'direct' ? 'DIRECT' : form.value.clash_policy }, editing.value ? 'PUT' : 'POST')
    edit(); emit('changed')
  } catch (cause) { emit('failure', cause) } finally { busy.value = '' }
}
async function operate(group: EgressGroup, operation: 'probe' | 'toggle' | 'delete') {
  busy.value = group.group_id
  try {
    if (operation === 'probe') await send(`/egress-groups/${group.group_id}/probe`)
    else if (operation === 'delete') await request(`/egress-groups/${group.group_id}`, { method: 'DELETE' })
    else await send(`/egress-groups/${group.group_id}`, { name: group.name, clash_policy: group.clash_policy, routing_confirmed: group.routing_confirmed, enabled: !group.enabled }, 'PUT')
    emit('changed')
  } catch (cause) { emit('failure', cause) } finally { busy.value = '' }
}
async function showRules() {
  try { rules.value = await request<EgressRules>('/egress-groups/rules') } catch (cause) { emit('failure', cause) }
}
</script>
<template>
  <div class="page-heading"><div><p class="eyebrow">IP POOL</p><h1>IP 池</h1><p>{{ occupied }} 个名额占用 · {{ capacity }} 个可用出口容量 · {{ waitingCount }} 个账号等待出口</p></div><button class="button secondary" @click="showRules">生成 Clash 规则</button></div>
  <div class="notice"><strong>添加出口</strong><p>在 Clash 配置节点 → 登记出口 → 部署进程规则 → 检测 → 确认规则 → 启用。直连也是一个出口，需要相应 DIRECT 规则。</p><p>每个公网 IP 最多 25 个账号。停用只停止新分配，现有连接继续保留。启用新出口后，等待账号自动继续。</p></div>
  <section v-if="rules" class="panel pool-form"><p>{{ rules.instructions }}</p><textarea :value="rules.yaml" readonly rows="8" aria-label="Clash 规则片段" /></section>
  <section class="panel"><form class="pool-form" @submit.prevent="save"><h2>{{ editing ? '编辑出口' : '添加出口' }}</h2>
    <div class="field-row"><label class="field">出口名称<input v-model="form.name" required maxlength="80" /></label><label class="field">出口类型<select v-model="direct" aria-label="出口类型"><option value="proxy">Clash 代理</option><option value="direct">服务器直连</option></select></label><label v-if="direct === 'proxy'" class="field">Clash 节点名称<input v-model="form.clash_policy" required maxlength="120" /></label></div>
    <label v-if="editing"><input v-model="form.routing_confirmed" type="checkbox" />已部署本出口进程规则并核对出口</label>
    <p class="help">新出口默认停用。修改路由前应先停用并等待所有连接断开，修改后重新检测、确认和启用。</p>
    <div class="button-row"><button class="button primary" :disabled="!!busy">{{ editing ? '保存出口' : '添加出口' }}</button><button v-if="editing" type="button" class="text-button" @click="edit()">取消编辑</button></div>
  </form></section>
  <section class="panel"><h2 class="pool-form">全部出口 · {{ groups.length }}</h2><p v-if="!groups.length" class="pool-form">尚未添加出口，连接请求将等待。</p>
    <article v-for="group in groups" :key="group.group_id" class="pool-item"><div class="pool-heading"><div><h3>{{ group.name }} <span class="badge" :class="group.active ? 'success' : 'neutral'">{{ group.active ? '参与分配' : group.enabled ? '等待重新检测' : '已停用' }}</span></h3><p>{{ group.clash_policy === 'DIRECT' ? '服务器直连' : group.clash_policy }} · 占用 {{ group.online_count }} / 25</p><p>公网 IP：<strong>{{ group.exit_ip || '尚未检测' }}</strong> · {{ time(group.exit_checked_at ?? undefined) }}</p></div>
      <div class="button-row"><button class="button secondary small" :disabled="!!busy || group.online_count > 0" @click="operate(group, 'probe')">检测出口</button><button class="button secondary small" :disabled="!!busy || (!group.enabled && (!group.exit_ip || !group.routing_confirmed))" @click="operate(group, 'toggle')">{{ group.enabled ? '停用' : '启用' }}</button><button class="text-button" :disabled="!!busy" @click="edit(group)">编辑</button><button class="text-button danger-text" :disabled="!!busy || group.online_count > 0" @click="operate(group, 'delete')">删除</button></div></div>
      <p v-if="group.worker_error" class="danger-text">{{ group.worker_error }}</p><code class="worker-path">{{ group.worker_path }}</code>
    </article>
  </section>
</template>
<style scoped>.panel{margin-bottom:20px}.pool-form,.pool-item{padding:24px 28px}.pool-item{border-top:1px solid var(--line,#e7ebf3)}.pool-heading{display:flex;justify-content:space-between;gap:16px}.pool-form textarea{width:100%;font-family:monospace}.pool-item p{margin:10px 0}.worker-path{display:block;overflow-wrap:anywhere;color:#71809c;font-size:12px}.pool-form .button-row{margin-top:18px}@media(max-width:800px){.pool-heading{flex-direction:column}}</style>
