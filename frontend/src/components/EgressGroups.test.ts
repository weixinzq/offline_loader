import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import EgressGroups from './EgressGroups.vue'
import type { EgressGroup } from '../types'

const group: EgressGroup = { group_id: 'g1', name: '出口一', clash_policy: '节点一', max_online: 30,
  routing_confirmed: false, worker_state: 'offline', worker_pid: null, worker_path: 'D:/test/AolaWorker.exe',
  worker_error: null, exit_ip: null, exit_checked_at: null, online_count: 0, reserved_count: 0 }
afterEach(() => vi.unstubAllGlobals())

describe('出口分组', () => {
  it('创建分组不默认确认尚未导入的规则', async () => {
    const fetch = vi.fn(async (_url: string, _init?: RequestInit) => new Response(JSON.stringify(group)))
    vi.stubGlobal('fetch', fetch)
    const wrapper = mount(EgressGroups, { props: { groups: [] } })
    const inputs = wrapper.findAll('input')
    await inputs[0]!.setValue('出口一'); await inputs[1]!.setValue('节点一')
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(JSON.parse(fetch.mock.calls[0]![1]!.body as string)).toEqual({ name: '出口一', clash_policy: '节点一', max_online: 30, routing_confirmed: false })
    expect(wrapper.emitted('changed')).toHaveLength(1)
  })
  it('检测出口只调用探测接口，并提示重复的出口 IP', async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify(group)))
    vi.stubGlobal('fetch', fetch)
    const wrapper = mount(EgressGroups, { props: { groups: [{ ...group, exit_ip: '192.0.2.1' }, { ...group, group_id: 'g2', exit_ip: '192.0.2.1' }] } })
    expect(wrapper.text()).toContain('多个分组检测到相同出口 IP')
    await wrapper.findAll('button').find(button => button.text() === '检测出口')!.trigger('click')
    await flushPromises()
    expect(fetch).toHaveBeenCalledWith('/api/v1/egress-groups/g1/probe', expect.objectContaining({ method: 'POST' }))
    expect(fetch).toHaveBeenCalledTimes(1)
  })
  it('规则作为可复制文本显示，编辑时允许人工确认', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ yaml: 'rules:\n- PROCESS-PATH,D:/test/AolaWorker.exe,节点一', instructions: '合并至规则最前面' }))))
    const wrapper = mount(EgressGroups, { props: { groups: [group] } })
    await wrapper.findAll('button').find(button => button.text() === '生成 Clash 规则')!.trigger('click'); await flushPromises()
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toContain('PROCESS-PATH')
    await wrapper.findAll('button').find(button => button.text() === '编辑')!.trigger('click')
    expect(wrapper.find('input[type=checkbox]').exists()).toBe(true)
    expect(wrapper.text()).toContain('已在 Clash 中启用本组进程规则')
  })
})
