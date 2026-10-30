const labels: Record<string, string> = {
  offline: '离线', waiting_egress: '等待出口', connecting: '连接中', initializing: '初始化中', ready: '就绪', disconnecting: '断开中',
  queued: '等待中', running: '执行中', cancelling: '取消中', succeeded: '已完成', failed: '失败',
  cancelled: '已取消', interrupted: '已中断', pending: '等待中', skipped: '已跳过', unknown: '未知',
  verification_required: '需要人工验证', execute: '执行任务', connect: '连接账号', reconnect: '重连账号', disconnect: '断开账号',
  not_attempted: '未尝试', partial: '部分写入',
}
export const label = (value?: string) => value ? labels[value] ?? value : '—'
export const isActive = (state: string) => ['queued', 'running', 'cancelling'].includes(state)
export const tone = (state: string) => ['ready', 'succeeded'].includes(state) ? 'success'
  : ['failed', 'interrupted', 'verification_required'].includes(state) ? 'danger'
    : ['waiting_egress', 'queued', 'running', 'connecting', 'initializing', 'cancelling', 'disconnecting'].includes(state) ? 'working' : 'neutral'
export const time = (value?: string) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'

export function accountBody(input: { label: string; account: string; password: string; char_id: number; zone_index: number }, editing: boolean) {
  const { password, ...rest } = input
  return { ...rest, ...(!editing || password ? { password } : {}) }
}
