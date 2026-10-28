/**
 * The picker follows the conductor, not only the roster. `lib/crewMode` keeps the
 * conductor set in a module variable and `useAgents` keeps an unchanged roster by
 * reference, so a catalog read that changes ONLY `agent.crew_mode_agent` must still
 * re-run the picker memos: the former conductor comes back as an ordinary template
 * and the new one leaves. Without the catalog identity in the memo deps this test
 * goes red (the picker keeps hiding the old conductor and offering the new one).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { waitFor } from '@testing-library/react'
import { renderHookWithProviders } from './helpers'
import { useAgents } from '../hooks/useAgents'
import { resetCrewModeCatalog } from '../lib/crewMode'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    agentCatalog: vi.fn(),
    kirocrewConfig: vi.fn(),
  },
}))

const TEMPLATES = [
  { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'main agent', source: 'kirocrew', selection_kind: 'template' },
  { name: 'lead-a', kiro_agent: 'lead-a', workspace: 'default', memory_store: 'default', description: 'a conductor', source: 'package', selection_kind: 'template' },
  { name: 'lead-b', kiro_agent: 'lead-b', workspace: 'default', memory_store: 'default', description: 'another', source: 'package', selection_kind: 'template' },
]

const agentsApi = vi.mocked(api.agentCatalog)
const configApi = vi.mocked(api.kirocrewConfig)

const names = (rows: { name: string }[]) => rows.map(r => r.name).sort()

describe('useAgents follows a conductor change with the roster unchanged', () => {
  beforeEach(() => {
    agentsApi.mockReset()
    configApi.mockReset()
    configApi.mockResolvedValue({} as never)
  })
  afterEach(() => resetCrewModeCatalog())

  it('keeps every template, the conductor too, on this part, and re-reads the picker when only the conductor moves', async () => {
    // `withoutConductors` is inert until the composer's Crew Mode switch lands,
    // so the picker keeps the conductor templates: they are still the way onto
    // one. The memo still re-runs on a conductor change (`crewCatalogKey`), which
    // is what makes the switch part's drop take effect without a reload.
    agentsApi.mockResolvedValue({ agents: TEMPLATES, default_agent: 'kirocrew', crew_mode_agent: 'lead-a', conductor_agents: ['lead-a'] } as never)
    let tick = 0
    const { result, rerender } = renderHookWithProviders(() => useAgents(tick))
    await waitFor(() => expect(names(result.current.choices)).toEqual(['kirocrew', 'lead-a', 'lead-b']))
    const before = result.current.choices

    // The same three templates, in the same order; only the conductor moved.
    agentsApi.mockResolvedValue({ agents: TEMPLATES, default_agent: 'kirocrew', crew_mode_agent: 'lead-b', conductor_agents: ['lead-b'] } as never)
    tick = 1
    rerender()
    await waitFor(() => expect(agentsApi).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(result.current.choices).not.toBe(before))
    expect(names(result.current.choices)).toEqual(['kirocrew', 'lead-a', 'lead-b'])
    expect(names(result.current.displayChoices)).toEqual(['kirocrew', 'lead-a', 'lead-b'])
  })
})
