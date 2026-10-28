import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

/** A Crew Mode worker's finished or paused turn raises no chime or toast for the
 *  person: its conductor reads it (owner decisions A and B, edge review R1 S2/S4).
 *  Its tool approvals keep notifying here; the part that adds the conductor's
 *  approval card is the one that routes them there. */
const read = (name: string) => readFileSync(path.join(__dirname, name), 'utf-8')

describe('Crew Mode workers stay quiet for the person', () => {
  it('a worker turn raises no turn-done chime or toast', () => {
    const src = read('turnCompletion.ts')
    expect(src).toContain('const crewWorker = !!soundSlot?.crew_root')
    expect(src).toMatch(/&& !muteSuppresses && !crewWorker/)
  })
})
