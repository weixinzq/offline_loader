export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message) }
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    credentials: 'same-origin',
    headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
  })
  const data = response.status === 204 ? null : await response.json().catch(() => null)
  if (!response.ok) {
    const detail = data?.error ?? data?.detail
    const message = typeof detail === 'string' ? detail : detail?.message
    throw new ApiError(response.status, detail?.code ?? 'request_failed', message ?? `请求失败（${response.status}）`)
  }
  return data as T
}

export const send = <T>(path: string, body?: unknown, method = 'POST') =>
  request<T>(path, { method, ...(body === undefined ? {} : { body: JSON.stringify(body) }) })
