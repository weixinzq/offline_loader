import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import TaskDetail from './TaskDetail.vue'
import type { TaskRecord } from '../types'

describe('recovered battle rounds', () => {
  const task: TaskRecord = {
    task_id: 'recovered', kind: 'execute', state: 'succeeded', error: null,
    created_at: '2026-10-19T12:00:00Z', started_at: null, finished_at: null, spec: {},
    account_results: [{ account_id: 'a', state: 'succeeded', error: null,
      completed_repetitions: 2, attempted_repetitions: 5, skipped_repetitions: 3,
      network_write: { status: 'succeeded', count: 9 }, business_confirmation: 'unknown' }],
  }

  it('shows attempted, normal and skipped rounds without calling skips normal success', () => {
    const wrapper = mount(TaskDetail, { props: { task, accounts: [], logs: [], cancelling: false } })
    expect(wrapper.text()).toContain('完成，含跳过')
    expect(wrapper.text()).toContain('5 / 2 / 3')
    expect(wrapper.find('.badge').classes()).toContain('working')
    wrapper.unmount()
  })

  it('uses normal completion for older cached rows without new counts', () => {
    const old = JSON.parse(JSON.stringify(task))
    delete old.account_results[0].attempted_repetitions
    delete old.account_results[0].skipped_repetitions
    const wrapper = mount(TaskDetail, { props: { task: old, accounts: [], logs: [], cancelling: false } })
    expect(wrapper.text()).toContain('2 / 2 / 0')
    expect(wrapper.text()).not.toContain('完成，含跳过')
    wrapper.unmount()
  })
})
