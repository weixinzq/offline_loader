import { computed, ref, watch, type Ref } from 'vue'
import type { Account } from './types'

export const ACCOUNT_PAGE_SIZE = 50

export function useAccountPagination(accounts: Ref<Account[]>) {
  const accountPage = ref(1)
  const accountPages = computed(() => Math.max(1, Math.ceil(accounts.value.length / ACCOUNT_PAGE_SIZE)))
  watch(accountPages, pages => { accountPage.value = Math.min(accountPage.value, pages) })
  const visibleAccounts = computed(() => accounts.value.slice(
    (accountPage.value - 1) * ACCOUNT_PAGE_SIZE, accountPage.value * ACCOUNT_PAGE_SIZE,
  ))
  return { accountPage, accountPages, visibleAccounts }
}
