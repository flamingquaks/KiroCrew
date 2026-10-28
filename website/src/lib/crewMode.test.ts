import { afterEach, describe, expect, it } from 'vitest'
import { SHIPPED_CONDUCTORS,
  DEFAULT_CREW_MODE_AGENT, crewModeAgent, crewModeView, isConductorAgent, isDefaultAgentName, runsCrewMode,
  memoryModeAllowsCrewMode, runsConductor, resetCrewModeCatalog, setCrewModeCatalog, withoutConductors, agentCycleTarget,
  gatewayRunsCrewMode, CREW_APPROVAL_SURFACES, crewApprovalDecidableHere,
} from './crewMode'

afterEach(() => resetCrewModeCatalog())

describe('crewMode', () => {
  it('runs on the shipped conductor until the catalog says otherwise', () => {
    expect(DEFAULT_CREW_MODE_AGENT).toBe('kirocrew-conductor')
    expect(crewModeAgent()).toBe('kirocrew-conductor')
  })

  it('swaps the conductor to the one the catalog names', () => {
    setCrewModeCatalog('better-conductor', ['kirocrew-conductor', 'kirocrew-team-lead'])
    expect(crewModeAgent()).toBe('better-conductor')
    // The configured agent is a conductor even when the family list omits it.
    expect(isConductorAgent('better-conductor')).toBe(true)
    expect(isConductorAgent('kirocrew-team-lead')).toBe(true)
    expect(isConductorAgent('kirocrew')).toBe(false)
  })

  it('never treats a crewmate as a conductor, whatever its name', () => {
    setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor'])
    expect(runsConductor('kirocrew-conductor', 'template')).toBe(true)
    expect(runsConductor('kirocrew-conductor', undefined)).toBe(true)
    expect(runsConductor('kirocrew-conductor', 'member')).toBe(false)
  })

  it('drops conductor templates from a normal chat picker and keeps crewmates, whatever their name', () => {
    // The composer's Crew Mode switch is the way onto a conductor, so the picker
    // lists none of the catalog's conductor set; a crewmate row stays.
    setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor', 'kirocrew-pipeline-conductor'])
    const rows = [
      { name: 'kirocrew', selection_kind: 'template' },
      { name: 'kirocrew-conductor', selection_kind: 'template' },
      { name: 'kirocrew-pipeline-conductor', selection_kind: 'template' },
      { name: 'kirocrew-conductor', selection_kind: 'member' },
    ]
    expect(withoutConductors(rows)).toEqual([
      { name: 'kirocrew', selection_kind: 'template' },
      { name: 'kirocrew-conductor', selection_kind: 'member' },
    ])
  })

  it('keeps workers out of the person list and can show only roots', () => {
    const rows = [
      { key: 'a', crew_mode_root: true },
      { key: 'b', crew_root: 'a' },
      { key: 'c' },
    ]
    expect(crewModeView(rows).map(r => r.key)).toEqual(['a', 'c'])
    expect(crewModeView(rows, true).map(r => r.key)).toEqual(['a'])
  })

  it('treats the empty name and the configured default as the default agent', () => {
    expect(isDefaultAgentName('', 'default')).toBe(true)
    expect(isDefaultAgentName('default', 'default')).toBe(true)
    expect(isDefaultAgentName('custom-x', 'default')).toBe(false)
    // `kirocrew` is the default only while none is configured.
    expect(isDefaultAgentName('kirocrew', 'default')).toBe(false)
    expect(isDefaultAgentName('kirocrew', '')).toBe(true)
  })

  it('needs a Persistent chat, since an Incognito or Temporary one cannot open sessions', () => {
    expect(memoryModeAllowsCrewMode(undefined)).toBe(true)
    expect(memoryModeAllowsCrewMode('persistent')).toBe(true)
    expect(memoryModeAllowsCrewMode('incognito')).toBe(false)
    expect(memoryModeAllowsCrewMode('temporary')).toBe(false)
  })

  it('cycles a normal chat over the same list the picker shows, conductors left out', () => {
    setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor', 'kirocrew-team-lead'])
    const agents = [
      { name: 'kirocrew', selection_kind: 'template' },
      { name: 'kirocrew-conductor', selection_kind: 'template' },
      { name: 'research', selection_kind: 'template' },
      { name: 'kirocrew-team-lead', selection_kind: 'template' },
    ]
    // The cycle reads `withoutConductors`: the chord never lands on a conductor,
    // since the composer's Crew Mode switch is the way onto one.
    expect(agentCycleTarget(agents, 'kirocrew', 'template', 1)).toBe('research')
    expect(agentCycleTarget(agents, 'research', 'template', 1)).toBe('kirocrew')
    expect(agentCycleTarget(agents, 'kirocrew', 'template', -1)).toBe('research')
    // A chat already on a conductor has no picker to mirror (the server refuses
    // to move a Crew Mode root), so the chord does nothing there.
    expect(agentCycleTarget(agents, 'kirocrew-conductor', 'template', 1)).toBeNull()
    expect(agentCycleTarget([], 'kirocrew', 'template', 1)).toBeNull()
  })
})

describe('runsCrewMode', () => {
  it('is every conductor in the catalog list', () => {
    setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor', 'kirocrew-pipeline-conductor', 'kirocrew-ledger-conductor'])
    expect(runsCrewMode('kirocrew-conductor', 'template')).toBe(true)
    expect(runsCrewMode('kirocrew-ledger-conductor', 'template')).toBe(true)
    expect(runsCrewMode('kirocrew-pipeline-conductor', 'template')).toBe(true)
    expect(isConductorAgent('kirocrew-ledger-conductor')).toBe(true)
    expect(runsCrewMode('kirocrew-conductor', 'member')).toBe(false)
    expect(runsCrewMode('kirocrew', 'template')).toBe(false)
  })

  it('names no conductor before the catalog lands, so a restored conductor chat is an ordinary chat until then', () => {
    resetCrewModeCatalog()
    expect(gatewayRunsCrewMode()).toBe(false)
    for (const name of SHIPPED_CONDUCTORS) expect(runsCrewMode(name, 'template')).toBe(false)
    // The stamp a Crew Mode chat carries is recognized on its own.
    expect(runsCrewMode('kirocrew-conductor', 'template', 'kirocrew-conductor')).toBe(true)
  })

  it('falls back to the shipped conductors when a Crew Mode catalog names no list', () => {
    setCrewModeCatalog('kirocrew-conductor', undefined)
    expect(gatewayRunsCrewMode()).toBe(true)
    for (const name of SHIPPED_CONDUCTORS) expect(runsCrewMode(name, 'template')).toBe(true)
  })

  it('names no conductor on a gateway without Crew Mode, so nothing is hidden there', () => {
    // A catalog with neither Crew Mode field: the gateway predates Crew Mode.
    setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor'])
    setCrewModeCatalog(undefined, undefined)
    expect(gatewayRunsCrewMode()).toBe(false)
    for (const name of SHIPPED_CONDUCTORS) {
      expect(runsCrewMode(name, 'template')).toBe(false)
      expect(isConductorAgent(name)).toBe(false)
    }
    // The picker keeps every template it shows today, conductors included.
    const rows = SHIPPED_CONDUCTORS.map(name => ({ name, selection_kind: 'template' }))
    expect(withoutConductors(rows)).toEqual(rows)
    // The agent chord cycles over the whole list and lands on a conductor as today.
    expect(agentCycleTarget(rows, 'kirocrew-conductor', 'template', 1)).toBe('kirocrew-ledger-conductor')
    // The switch still knows which agent it would turn on.
    expect(crewModeAgent()).toBe(DEFAULT_CREW_MODE_AGENT)
  })

  it('keeps a stamped chat in Crew Mode until the gateway moves it', () => {
    setCrewModeCatalog('new-one', ['new-one'])
    expect(runsCrewMode('old-one', 'template', 'old-one')).toBe(true)
    expect(runsCrewMode('kirocrew', 'template', 'old-one')).toBe(false)
    expect(runsCrewMode('old-one', 'template', '')).toBe(false)
  })
})

describe('agentCycleTarget and the Crew Mode stamp', () => {
  it('does not cycle a chat that stays in Crew Mode on its stamped conductor', () => {
    setCrewModeCatalog('new-one', ['new-one'])
    const agents = [{ name: 'kirocrew' }, { name: 'old-one' }, { name: 'helper' }]
    expect(agentCycleTarget(agents, 'old-one', 'template', 1, 'old-one')).toBeNull()
    expect(agentCycleTarget(agents, 'old-one', 'template', -1, 'old-one')).toBeNull()
    // Without the stamp the same chat is an ordinary one and cycles.
    expect(agentCycleTarget(agents, 'old-one', 'template', 1)).not.toBeNull()
  })
})

describe('crewApprovalDecidableHere', () => {
  it('lets a crew member\'s approval be decided on its conductor\'s card alone', () => {
    const worker = { crew_root: 'chat-1-1' }
    for (const surface of CREW_APPROVAL_SURFACES) {
      expect(crewApprovalDecidableHere(worker, surface)).toBe(surface === 'conductor_card')
    }
  })

  it('leaves every other session its usual controls on every surface', () => {
    for (const surface of CREW_APPROVAL_SURFACES) {
      expect(crewApprovalDecidableHere({ crew_root: '' }, surface)).toBe(true)
      expect(crewApprovalDecidableHere({}, surface)).toBe(true)
      expect(crewApprovalDecidableHere(null, surface)).toBe(true)
    }
  })
})
