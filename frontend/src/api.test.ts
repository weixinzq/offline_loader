import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, request, send } from './api'

afterEach(() => vi.unstubAllGlobals())

describe('HTTP 客户端', () => {
  it('使用同源 cookie，提交内容而非客户端路径', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ task_id: 't1' }), { status: 202 }))
    vi.stubGlobal('fetch', fetch)
    await send('/tasks', { account_ids: ['a1'], steps: [] })
    expect(fetch).toHaveBeenCalledWith('/api/v1/tasks', expect.objectContaining({
      credentials: 'same-origin', method: 'POST', body: '{"account_ids":["a1"],"steps":[]}',
    }))
  })
  it('保留认证失败和结构化业务错误', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code: 'account_busy', message: '账号正在执行任务' } }), { status: 409 })))
    await expect(request('/accounts')).rejects.toEqual(new ApiError(409, 'account_busy', '账号正在执行任务'))
  })
  it('支持无内容响应', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 204 })))
    await expect(request('/auth/session', { method: 'DELETE' })).resolves.toBeNull()
  })
})
