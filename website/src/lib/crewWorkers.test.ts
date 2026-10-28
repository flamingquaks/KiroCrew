import { describe, expect, it } from 'vitest'
import { CREW_GROUP_LABEL_KEY, CREW_GROUP_ORDER, CREW_GROUP_TONE, crewSubDispatcher, crewWorkerGroup, crewWorkerGroupOfRow, groupCrewWorkers, orderUnderDispatcher, summarizeCrewWorkers } from './crewWorkers'
import type { CrewWorker } from '../types'

describe('crewWorkers', () => {
  it('a live row alone never says done: replying is not finishing', () => {
    expect(crewWorkerGroupOfRow({ pending_approval: true, running: true })).toBe('approval')
    // A worker's question is its conductor's (edge review R1 S2, owner decision A).
    expect(crewWorkerGroupOfRow({ needs_input: true })).toBe('with_conductor')
    expect(crewWorkerGroupOfRow({ running: true })).toBe('working')
    expect(crewWorkerGroupOfRow({ messages: 3 })).toBe('waiting')
    expect(crewWorkerGroupOfRow({ messages: 0 })).toBe('waiting')
  })

  it('takes the group the API row carries', () => {
    const w = (over: Partial<CrewWorker>) => ({ key: 'k', title: '', agent: '', status: 'idle', group: 'waiting', last_activity: '', ledger_item: '', created_by: '', ...over }) as CrewWorker
    expect(crewWorkerGroup(w({ group: 'done', status: 'idle' }))).toBe('done')
    expect(groupCrewWorkers([w({ key: 'a', group: 'done' }), w({ key: 'b', group: 'approval' }), w({ key: 'c', group: 'with_conductor' })]).map(g => g.group))
      .toEqual(['approval', 'with_conductor', 'done'])
  })

  it('has no group that says a worker needs the person', () => {
    expect(CREW_GROUP_ORDER).not.toContain('needs_you')
  })

  // The server's table (test_crew_mode_workers.py: status, messages -> group), restated
  // as live-row flags, so the client mirror cannot drift from it.
  it.each([
    [{ pending_approval: true, messages: 4 }, 'approval'],
    [{ needs_input: true, messages: 4 }, 'with_conductor'],
    [{ running: true, messages: 0 }, 'working'],
    // The server says done only from a closed tab or the ledger; a row has neither.
    [{ messages: 2 }, 'waiting'],
    [{ messages: 0 }, 'waiting'],
  ] as const)('a live row %o groups as %s', (row, group) => {
    expect(crewWorkerGroupOfRow(row)).toBe(group)
  })

  describe('sub-conductors', () => {
    const w = (key: string, dispatched_by?: string) => ({ key, title: '', agent: '', status: 'idle', group: 'working', last_activity: '', ledger_item: '', created_by: '', dispatched_by }) as CrewWorker

    it('names a worker\'s dispatcher only when it is neither the root nor itself', () => {
      expect(crewSubDispatcher(w('a', 'root'), 'root')).toBe('')
      expect(crewSubDispatcher(w('a', 'a'), 'root')).toBe('')
      expect(crewSubDispatcher(w('a'), 'root')).toBe('')
      expect(crewSubDispatcher(w('a', 'sub'), 'root')).toBe('sub')
    })

    it('puts a sub-conductor\'s crew right under it and keeps the server order otherwise', () => {
      const rows = [w('x', 'root'), w('c1', 'sub'), w('sub', 'root'), w('c2', 'sub'), w('y')]
      expect(orderUnderDispatcher(rows, 'root').map(r => r.key)).toEqual(['x', 'sub', 'c1', 'c2', 'y'])
    })

    it('a worker whose dispatcher is not in the group stays a top row', () => {
      const rows = [w('c1', 'elsewhere'), w('a', 'root')]
      expect(orderUnderDispatcher(rows, 'root').map(r => r.key)).toEqual(['c1', 'a'])
    })

    it('keeps every row through a dispatcher cycle', () => {
      const rows = [w('p', 'q'), w('q', 'p'), w('z', 'root')]
      expect(orderUnderDispatcher(rows, 'root').map(r => r.key).sort()).toEqual(['p', 'q', 'z'])
      expect(orderUnderDispatcher(rows, 'root')).toHaveLength(3)
    })

    it('every tone and label key is set for every group', () => {
      for (const g of CREW_GROUP_ORDER) {
        expect(CREW_GROUP_TONE[g]).toMatch(/^crew-tone-/)
        expect(CREW_GROUP_LABEL_KEY[g]).toMatch(/^commandCenter\.worker_group_/)
      }
    })
  })

  it('summarizes the API groups, approval first, and counts only approvals for the person', () => {
    const w = (group: CrewWorker['group']) => ({ key: group, title: '', agent: '', status: 'idle', group, last_activity: '', ledger_item: '', created_by: '' }) as CrewWorker
    expect(summarizeCrewWorkers([w('done'), w('with_conductor'), w('approval'), w('waiting')]))
      .toEqual({ groups: ['approval', 'with_conductor', 'waiting', 'done'], approval: 1, done: 1, total: 4 })
  })
})
