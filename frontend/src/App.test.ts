import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import App from './App.vue'
import TaskComposer from './components/TaskComposer.vue'
import TaskDetail from './components/TaskDetail.vue'
import type { TaskRecord } from './types'

const account = { account_id: 'a1', label: '测试账号', account: 'test-user', char_id: 0, zone_index: 0 }
const task: TaskRecord = {
  task_id: 't1', kind: 'execute', state: 'succeeded', error: null, spec: {},
  created_at: '2026-10-15T08:00:00Z', started_at: null, finished_at: null,
  account_results: [{ account_id: 'a1', state: 'succeeded', error: null, completed_repetitions: 1, attempted_repetitions: 1, skipped_repetitions: 0,
    network_write: { status: 'succeeded', count: 1 }, business_confirmation: 'unknown' }],
}
const reply = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status })
class MockEventSource {
  static instances: MockEventSource[] = []
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((event: MessageEvent<string>) => void) | null = null
  close = vi.fn()
  addEventListener = vi.fn()
  constructor(public url: string) { MockEventSource.instances.push(this) }
}

beforeEach(() => {
  MockEventSource.instances = []
  vi.stubGlobal('EventSource', MockEventSource)
})
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

function mockApi(authenticated = true) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === '/api/v1/auth/session') {
      if (init?.method === 'POST') { authenticated = true; return reply({ authenticated: true }) }
      return reply(authenticated ? { authenticated: true } : { error: { code: 'unauthorized', message: '请登录' } }, authenticated ? 200 : 401)
    }
    if (url === '/api/v1/accounts') return reply([account])
    if (url === '/api/v1/accounts/a1') return reply(account)
    if (url === '/api/v1/sessions') return reply([{ account_id: 'a1', label: '测试账号', state: 'ready', error: null, owner_task_id: null }])
    if (url === '/api/v1/scripts') return reply([{ script_id: 'sample', name: '示例脚本', description: '用于测试' }])
    if (url === '/api/v1/egress-groups') return reply([])
    if (url === '/api/v1/tasks') return reply(init?.method === 'POST' ? { task_id: 't1' } : [task])
    if (url === '/api/v1/tasks/t1') return reply(task)
    if (url.startsWith('/api/v1/tasks/t1/logs')) return reply([])
    if (url === '/api/v1/messages/parse') return reply({ valid: true, messages: [{ id: 1, cmd: '42', param: {} }], count: 1 })
    if (url === '/api/v1/tasks/validate') return reply({ valid: true })
    return reply({})
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}

describe('浏览器工作台', () => {
  it('将所选账号分配出口组，只有明确选择后才能提交', async () => {
    const fetch = mockApi()
    const wrapper = mount(App)
    await flushPromises()
    const button = () => wrapper.findAll('button').find(item => item.text() === '分配所选账号')!
    expect(button().attributes('disabled')).toBeDefined()
    await wrapper.get('input[aria-label="选择账号 测试账号"]').setValue(true)
    expect(button().attributes('disabled')).toBeDefined()
    await wrapper.get('select[aria-label="选择出口分组"]').setValue('')
    await button().trigger('click'); await flushPromises()
    const call = fetch.mock.calls.find(([url]) => url === '/api/v1/accounts/assign-egress')
    expect(JSON.parse(call![1]!.body as string)).toEqual({ account_ids: ['a1'], egress_group_id: null })
    expect(wrapper.text()).toContain('已为 1 个账号更新出口分组')
    wrapper.unmount()
  })
  it('未认证时显示登录，成功后初始化全部资源且清空凭据', async () => {
    const fetch = mockApi(false)
    const wrapper = mount(App)
    await flushPromises()
    expect(wrapper.text()).toContain('连接你的工作台')
    await wrapper.get('input[type=password]').setValue('local-secret')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('测试账号')
    expect(wrapper.find('input[type=password]').exists()).toBe(false)
    expect(localStorage.length).toBe(0)
    expect(fetch).toHaveBeenCalledWith('/api/v1/auth/session', expect.objectContaining({ body: '{"token":"local-secret"}' }))
    expect(MockEventSource.instances[0]?.url).toBe('/api/v1/events')
    wrapper.unmount()
  })
  it('编辑账号留空密码时不覆盖已有凭据', async () => {
    const fetch = mockApi()
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.findAll('button').find(b => b.text() === '编辑')!.trigger('click')
    const dialog = wrapper.get('[role=dialog]')
    expect((dialog.get('input[type=password]').element as HTMLInputElement).value).toBe('')
    await dialog.get('form').trigger('submit')
    await flushPromises()
    const call = fetch.mock.calls.find(([url, init]) => url.endsWith('/accounts/a1') && init?.method === 'PATCH')
    expect(JSON.parse(call![1]!.body as string)).toEqual({ label: '测试账号', account: 'test-user', char_id: 0, zone_index: 0 })
    wrapper.unmount()
  })
  it('断开浏览器组件只关闭事件流，不取消后台任务', async () => {
    const fetch = mockApi()
    const wrapper = mount(App)
    await flushPromises()
    wrapper.unmount()
    expect(MockEventSource.instances[0]?.close).toHaveBeenCalledOnce()
    expect(fetch.mock.calls.some(([url]) => url.endsWith('/cancel'))).toBe(false)
  })
  it('事件重连后重新读取状态', async () => {
    const fetch = mockApi()
    const wrapper = mount(App)
    await flushPromises()
    const stream = MockEventSource.instances[0]!
    stream.onerror?.()
    await flushPromises()
    expect(wrapper.text()).toContain('连接恢复中')
    const before = fetch.mock.calls.filter(([url]) => url.endsWith('/sessions')).length
    stream.onopen?.()
    await flushPromises()
    expect(fetch.mock.calls.filter(([url]) => url.endsWith('/sessions')).length).toBeGreaterThan(before)
    wrapper.unmount()
  })
  it('实时任务和会话每200毫秒合并刷新，日志突发不触发全量请求', async () => {
    const fetch = mockApi()
    const wrapper = mount(App)
    await flushPromises()
    vi.useFakeTimers()
    const before = fetch.mock.calls.length
    const stream = MockEventSource.instances[0]!
    stream.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ event: 'session', data: {
      account_id: 'a1', label: '测试账号', state: 'offline', error: '连接已关闭', owner_task_id: null,
    } }) }))
    stream.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ event: 'task', data: { ...task, state: 'running' } }) }))
    for (let index = 0; index < 100; index++) {
      stream.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ event: 'log', data: {
        id: index + 1, task_id: 't1', account_id: 'a1', timestamp: '2026-10-15T08:00:00Z', message: '进度',
      } }) }))
    }
    await vi.advanceTimersByTimeAsync(199)
    expect(wrapper.text()).not.toContain('连接已关闭')
    await vi.advanceTimersByTimeAsync(1)
    expect(wrapper.text()).toContain('离线')
    expect(wrapper.text()).toContain('连接已关闭')
    expect(fetch.mock.calls.length).toBe(before)
    await wrapper.findAll('nav button')[2]!.trigger('click')
    expect(wrapper.text()).toContain('执行中')
    wrapper.unmount()
  })
  it('操作返回401时清理账号并回到登录页', async () => {
    const fetch = mockApi()
    const wrapper = mount(App)
    await flushPromises()
    fetch.mockImplementation(async () => reply({ error: { code: 'unauthorized', message: '需要登录' } }, 401))
    await wrapper.get('input[type=checkbox]').setValue(true)
    await wrapper.findAll('button').find(b => b.text() === '连接')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('会话已失效')
    expect(wrapper.text()).not.toContain('测试账号')
    expect(MockEventSource.instances[0]?.close).toHaveBeenCalled()
    wrapper.unmount()
  })
  it('账号导入只提交读取的配置内容', async () => {
    const fetch = mockApi()
    const wrapper = mount(App)
    await flushPromises()
    const config = { accounts: [{ label: '旧账号', account: 'user', password: 'secret' }] }
    const input = wrapper.get('input[type=file]')
    Object.defineProperty(input.element, 'files', { configurable: true, value: [{ name: 'config.json', text: async () => JSON.stringify(config) }] })
    await input.trigger('change')
    await flushPromises()
    const call = fetch.mock.calls.find(([url]) => url === '/api/v1/accounts/import')
    expect(JSON.parse(call![1]!.body as string)).toEqual({ config })
    expect(call![1]!.body).not.toContain('config.json')
    wrapper.unmount()
  })
  it('2000个账号只渲染当前页，跨页选择和全选不触发业务请求', async () => {
    const fetch = mockApi()
    const fallback = fetch.getMockImplementation()!
    const manyAccounts = Array.from({ length: 2000 }, (_, index) => ({
      ...account, account_id: `a${index}`, label: `账号 ${index}`,
    }))
    const manySessions = manyAccounts.map(item => ({ account_id: item.account_id,
      label: item.label, state: 'offline', error: item.account_id === 'a50' ? '第51个账号的状态' : null,
      owner_task_id: null }))
    fetch.mockImplementation(async (url, init) => {
      if (url === '/api/v1/accounts') return reply(manyAccounts)
      if (url === '/api/v1/sessions') return reply(manySessions)
      return fallback(url, init)
    })
    const wrapper = mount(App)
    await flushPromises()
    const rows = () => wrapper.findAll('.accounts-panel tbody tr')
    expect(rows()).toHaveLength(50)
    const calls = fetch.mock.calls.length
    await rows()[0]!.get('input').setValue(true)
    const pager = wrapper.get('nav[aria-label="账号分页"]')
    await pager.findAll('button')[1]!.trigger('click')
    expect(pager.text()).toContain('第 2 / 40 页')
    expect(rows()[0]!.text()).toContain('第51个账号的状态')
    await rows()[0]!.get('input').setValue(true)
    expect(wrapper.text()).toContain('已选 2 个')
    await pager.findAll('button')[0]!.trigger('click')
    expect((rows()[0]!.get('input').element as HTMLInputElement).checked).toBe(true)
    await wrapper.get('input[aria-label="选择全部账号"]').setValue(true)
    expect(wrapper.text()).toContain('已选 2000 个')
    expect(rows()).toHaveLength(50)
    await rows()[0]!.get('input').setValue(false)
    expect(wrapper.text()).toContain('已选 1999 个')
    expect((wrapper.get('input[aria-label="选择全部账号"]').element as HTMLInputElement).checked).toBe(false)
    expect(fetch.mock.calls.length).toBe(calls)
    // Shrinking the dataset clamps the page and removes selections for deleted accounts.
    await pager.findAll('button')[1]!.trigger('click')
    manyAccounts.splice(1)
    await wrapper.findAll('button').find(button => button.text().includes('刷新'))!.trigger('click')
    await flushPromises()
    expect(rows()).toHaveLength(1)
    expect(wrapper.text()).toContain('已选 0 个')
    expect(wrapper.find('nav[aria-label="账号分页"]').exists()).toBe(false)
    wrapper.unmount()
  })
  it('任务日志按服务端游标分页，只渲染最近200条', async () => {
    const fetch = mockApi()
    const fallback = fetch.getMockImplementation()!
    fetch.mockImplementation(async (url, init) => {
      if (url.startsWith('/api/v1/tasks/t1/logs')) {
        const after = Number(new URL(url, 'http://localhost').searchParams.get('after'))
        const count = after ? 1 : 250
        return reply(Array.from({ length: count }, (_, index) => ({
          id: after + index + 1, task_id: 't1', account_id: 'a1', timestamp: '2026-10-15T08:00:00Z', message: `日志 ${after + index + 1}`,
        })))
      }
      return fallback(url, init)
    })
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.findAll('nav button')[2]!.trigger('click')
    await wrapper.findAll('button').find(b => b.text() === '查看详情 →')!.trigger('click')
    await flushPromises()
    await wrapper.findAll('button').find(b => b.text() === '加载更多')!.trigger('click')
    await flushPromises()
    expect(fetch.mock.calls.some(([url]) => url.endsWith('/logs?after=250&limit=250'))).toBe(true)
    expect(wrapper.get('[role=log]').text()).not.toContain('日志 1\n')
    expect(wrapper.get('[role=log]').text()).toContain('日志 251')
    expect(wrapper.findAll('.log-line')).toHaveLength(200)
    expect(wrapper.findAll('.log-line')[0]!.text()).toContain('日志 52')
    expect(wrapper.text()).toContain('已载入 251 条')
    wrapper.unmount()
  })
})

describe('实时更新节奏', () => {
  it('持续消息不会推迟刷新，同一账号与任务只保留最新状态', async () => {
    mockApi()
    const wrapper = mount(App)
    await flushPromises()
    vi.useFakeTimers()
    const stream = MockEventSource.instances[0]!
    const emit = (event: string, data: unknown) => stream.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ event, data }) }))
    emit('session', { account_id: 'a1', state: 'initializing', error: '中间状态' })
    emit('task', { ...task, state: 'running' })
    await vi.advanceTimersByTimeAsync(150)
    emit('session', { account_id: 'a1', state: 'offline', error: '最新连接状态' })
    emit('task', { ...task, state: 'failed' })
    await vi.advanceTimersByTimeAsync(50)
    expect(wrapper.text()).toContain('最新连接状态')
    expect(wrapper.text()).not.toContain('中间状态')
    await wrapper.findAll('nav button')[2]!.trigger('click')
    expect(wrapper.text()).toContain('失败')
    expect(wrapper.text()).not.toContain('执行中')
    wrapper.unmount()
  })
  it('日志按200毫秒合并、去重并限制渲染行数，关闭详情后不串入其他任务', async () => {
    const fetch = mockApi()
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.findAll('nav button')[2]!.trigger('click')
    await wrapper.findAll('button').find(button => button.text() === '查看详情 →')!.trigger('click')
    await flushPromises()
    vi.useFakeTimers()
    const before = fetch.mock.calls.length
    const stream = MockEventSource.instances[0]!
    const emit = (id: number, taskId = 't1') => stream.onmessage?.(new MessageEvent('message', { data: JSON.stringify({
      event: 'log', data: { id, task_id: taskId, account_id: 'a1', timestamp: '2026-10-18T03:00:00Z', message: `进度 ${id}` },
    }) }))
    for (let id = 1; id <= 300; id++) emit(id)
    emit(300)
    emit(301, 'other-task')
    await vi.advanceTimersByTimeAsync(199)
    expect(wrapper.findAll('.log-line')).toHaveLength(0)
    await vi.advanceTimersByTimeAsync(1)
    expect(wrapper.findAll('.log-line')).toHaveLength(200)
    expect(wrapper.findAll('.log-line')[0]!.text()).toContain('进度 101')
    expect(wrapper.findAll('.log-line').at(-1)!.text()).toContain('进度 300')
    expect(wrapper.text()).toContain('已载入 300 条')
    expect(fetch.mock.calls.length).toBe(before)
    emit(302)
    await wrapper.get('button[aria-label="关闭任务详情"]').trigger('click')
    await vi.advanceTimersByTimeAsync(200)
    expect(wrapper.find('.task-detail').exists()).toBe(false)
    wrapper.unmount()
  })
  it('组件卸载会清理尚未刷新的事件，不发送任何任务请求', async () => {
    const fetch = mockApi()
    const wrapper = mount(App)
    await flushPromises()
    vi.useFakeTimers()
    MockEventSource.instances[0]!.onmessage?.(new MessageEvent('message', { data: JSON.stringify({
      event: 'task', data: { ...task, state: 'running' },
    }) }))
    const before = fetch.mock.calls.length
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(500)
    expect(fetch.mock.calls.length).toBe(before)
    expect(vi.getTimerCount()).toBe(0)
  })
})

describe('任务编排与结果', () => {
  it('一键选择跨页就绪账号，并使用已有会话提交', async () => {
    const fetch = mockApi()
    const accounts = Array.from({ length: 60 }, (_, index) => ({ ...account, account_id: `a${index}`, label: `账号 ${index}` }))
    const sessions = accounts.map((item, index) => ({ account_id: item.account_id, label: item.label,
      state: index === 0 || index === 55 ? 'ready' as const : 'offline' as const, error: null, owner_task_id: null }))
    const wrapper = mount(TaskComposer, { props: { accounts, sessions, scripts: [{ script_id: 'sample', name: '示例脚本', description: '' }], selectedIds: [] } })
    await wrapper.findAll('button').find(button => button.text() === '选择就绪账号')!.trigger('click')
    expect(wrapper.text()).toContain('已选 2 个')
    expect((wrapper.findAll('select')[0]!.element as HTMLSelectElement).value).toBe('existing')
    await wrapper.findAll('select').at(-1)!.setValue('sample')
    await wrapper.findAll('button').find(button => button.text() === '添加脚本步骤')!.trigger('click')
    await wrapper.findAll('button').find(button => button.text() === '提交任务')!.trigger('click')
    await flushPromises()
    const call = fetch.mock.calls.find(([url, init]) => url === '/api/v1/tasks' && init?.method === 'POST')!
    expect(JSON.parse(call[1]!.body as string)).toMatchObject({ account_ids: ['a0', 'a55'], session_policy: 'existing' })
    wrapper.unmount()
  })
  it('实时点名所选断线账号，保留原选择交给后台再次检查', async () => {
    const fetch = mockApi()
    const accounts = [account, { ...account, account_id: 'a2', label: '断线账号' }]
    const sessions = accounts.map(item => ({ account_id: item.account_id, label: item.label,
      state: 'ready' as const, error: null, owner_task_id: null }))
    const wrapper = mount(TaskComposer, { props: { accounts, sessions, scripts: [{ script_id: 'sample', name: '示例脚本', description: '' }], selectedIds: ['a1', 'a2'] } })
    await wrapper.setProps({ sessions: [sessions[0]!, { ...sessions[1]!, state: 'offline', error: '连接已关闭' }] })
    expect(wrapper.get('[role=status]').text()).toContain('断线账号（离线）')
    expect(wrapper.get('[role=status]').text()).toContain('将跳过')
    expect(wrapper.findAll('.account-choice')[1]!.text()).toContain('连接已关闭')
    await wrapper.findAll('select').at(-1)!.setValue('sample')
    await wrapper.findAll('button').find(button => button.text() === '添加脚本步骤')!.trigger('click')
    const fallback = fetch.getMockImplementation()!
    fetch.mockImplementation(async (url, init) => url === '/api/v1/tasks/validate'
      ? reply({ valid: true, account_count: 1, skipped_accounts: [{ account_id: 'a2', label: '断线账号', reason: '连接已断开，已跳过' }] }) : fallback(url, init))
    await wrapper.findAll('button').find(button => button.text() === '预检任务')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('可执行 1 个账号，跳过 1 个')
    const call = fetch.mock.calls.find(([url]) => url === '/api/v1/tasks/validate')!
    expect(JSON.parse(call[1]!.body as string).account_ids).toEqual(['a1', 'a2'])
    wrapper.unmount()
  })
  it('任务详情明确显示跳过的账号和原因', () => {
    const skippedTask: TaskRecord = { ...task, account_results: [{ ...task.account_results[0]!, state: 'skipped',
      error: { code: 'session_not_ready', message: '连接已断开，已跳过' }, network_write: { status: 'not_attempted', count: 0 } }] }
    const wrapper = mount(TaskDetail, { props: { task: skippedTask, logs: [], accounts: [account], cancelling: false } })
    expect(wrapper.get('[role=status]').text()).toContain('测试账号')
    expect(wrapper.text()).toContain('已跳过')
    expect(wrapper.text()).toContain('连接已断开')
    wrapper.unmount()
  })
  it('编排页分页保留跨页选择，预检包含全部所选账号', async () => {
    const fetch = mockApi()
    const manyAccounts = Array.from({ length: 2000 }, (_, index) => ({
      ...account, account_id: `account-${index}`, label: `账号 ${index}`,
    }))
    const wrapper = mount(TaskComposer, { props: {
      accounts: manyAccounts, scripts: [{ script_id: 'sample', name: '示例脚本', description: '' }],
      selectedIds: [],
    } })
    expect(wrapper.findAll('.account-choice')).toHaveLength(50)
    await wrapper.findAll('.account-choice input')[0]!.setValue(true)
    const pager = wrapper.get('nav[aria-label="账号分页"]')
    await pager.findAll('button')[1]!.trigger('click')
    await wrapper.findAll('.account-choice input')[0]!.setValue(true)
    await wrapper.findAll('select').at(-1)!.setValue('sample')
    await wrapper.findAll('button').find(button => button.text() === '添加脚本步骤')!.trigger('click')
    await wrapper.findAll('button').find(button => button.text() === '预检任务')!.trigger('click')
    await flushPromises()
    const call = fetch.mock.calls.find(([url]) => url === '/api/v1/tasks/validate')
    expect(JSON.parse(call![1]!.body as string).account_ids).toEqual(['account-0', 'account-50'])
    await wrapper.findAll('button').find(button => button.text() === '全选')!.trigger('click')
    await wrapper.findAll('button').find(button => button.text() === '预检任务')!.trigger('click')
    await flushPromises()
    const latest = fetch.mock.calls.filter(([url]) => url === '/api/v1/tasks/validate').at(-1)!
    expect(JSON.parse(latest[1]!.body as string).account_ids).toHaveLength(2000)
    expect(wrapper.findAll('.account-choice')).toHaveLength(50)
    wrapper.unmount()
  })
  it('解析消息后，以明确账号及临时会话策略提交内容快照', async () => {
    const fetch = mockApi()
    const wrapper = mount(TaskComposer, { props: { accounts: [account], scripts: [], selectedIds: ['a1'] } })
    await wrapper.findAll('select')[0]!.setValue('temporary')
    await wrapper.get('textarea').setValue('原始消息内容')
    await wrapper.findAll('button').find(b => b.text() === '解析并添加')!.trigger('click')
    await flushPromises()
    await wrapper.findAll('button').find(b => b.text() === '预检任务')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('预检未登录账号或发送游戏消息')
    await wrapper.findAll('button').find(b => b.text() === '提交任务')!.trigger('click')
    await flushPromises()
    const call = fetch.mock.calls.find(([url, init]) => url === '/api/v1/tasks' && init?.method === 'POST')
    expect(JSON.parse(call![1]!.body as string)).toMatchObject({
      kind: 'execute', account_ids: ['a1'], session_policy: 'temporary', mode: 'bounded',
      steps: [{ kind: 'messages', messages: [{ id: 1, cmd: '42', param: {} }] }],
    })
    expect(wrapper.emitted('created')).toEqual([['t1']])
    wrapper.unmount()
  })
  it('执行完成与网络写入不会被展示成服务器业务确认', () => {
    const wrapper = mount(TaskDetail, { props: { task, logs: [], accounts: [account], cancelling: false } })
    expect(wrapper.text()).toContain('已完成')
    expect(wrapper.text()).toContain('1 次')
    expect(wrapper.text()).toContain('未知')
    expect(wrapper.findAll('button').some(b => b.text() === '取消任务')).toBe(false)
    wrapper.unmount()
  })
  it('未知写入次数保持未知，并允许分页读取历史日志', async () => {
    const unknownTask: TaskRecord = { ...task, account_results: [{ ...task.account_results[0]!, network_write: { status: 'unknown', count: null } }] }
    const wrapper = mount(TaskDetail, { props: { task: unknownTask, logs: [], accounts: [account], cancelling: false, moreLogs: true } })
    expect(wrapper.text()).toContain('次数未知')
    await wrapper.findAll('button').find(b => b.text() === '加载更多')!.trigger('click')
    expect(wrapper.emitted('moreLogs')).toHaveLength(1)
    wrapper.unmount()
  })
  it('无效并发数阻止预检与提交', async () => {
    const fetch = mockApi()
    const wrapper = mount(TaskComposer, { props: { accounts: [account], scripts: [{ script_id: 'sample', name: '示例脚本', description: '' }], selectedIds: ['a1'] } })
    await wrapper.findAll('select').at(-1)!.setValue('sample')
    await wrapper.findAll('button').find(b => b.text() === '添加脚本步骤')!.trigger('click')
    await wrapper.findAll('input[type=number]')[0]!.setValue('0')
    expect(wrapper.text()).toContain('并发数需为 1–100')
    const submit = wrapper.findAll('button').find(b => b.text() === '提交任务')!
    expect(submit.attributes('disabled')).toBeDefined()
    await submit.trigger('click')
    expect(fetch).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('分批任务可以选择超过100个账号，同时在线容量独立控制', async () => {
    const fetch = mockApi()
    const manyAccounts = Array.from({ length: 101 }, (_, index) => ({ ...account, account_id: `account-${index}` }))
    const wrapper = mount(TaskComposer, { props: {
      accounts: manyAccounts, scripts: [{ script_id: 'sample', name: '示例脚本', description: '' }],
      selectedIds: manyAccounts.map(item => item.account_id),
    } })
    await wrapper.findAll('select').at(-1)!.setValue('sample')
    await wrapper.findAll('button').find(b => b.text() === '添加脚本步骤')!.trigger('click')
    await wrapper.findAll('button').find(b => b.text() === '提交任务')!.trigger('click')
    await flushPromises()
    const call = fetch.mock.calls.find(([url, init]) => url === '/api/v1/tasks' && init?.method === 'POST')
    const body = JSON.parse(call![1]!.body as string)
    expect(body.account_ids).toHaveLength(101)
    expect(body).toMatchObject({ session_policy: 'temporary', mode: 'bounded', concurrency: 5 })
    wrapper.unmount()
  })
})
