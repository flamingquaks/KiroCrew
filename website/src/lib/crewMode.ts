/** Crew Mode: a UI over a conductor agent.
 *
 *  Every conductor is Crew Mode: a chat is in Crew Mode exactly when it runs a
 *  conductor TEMPLATE (or the agent its Crew Mode stamp names), so the composer
 *  switch is an ordinary agent switch from the default agent to the conductor.
 *  WHICH conductor is the gateway's `agent.crew_mode_agent`; the gateway moves
 *  every Crew Mode chat onto it when it changes, and refuses any other switch of
 *  a Crew Mode chat (`409 crew_mode_locked`). The conductor list is the gateway's
 *  one set (`kiro_crew.crew_mode.conductor_agents`);
 *  it arrives with the agent catalog (`GET /api/agents/catalog`, read by
 *  `useAgents`), which records them here. Until a catalog lands, and on a gateway
 *  whose catalog carries no Crew Mode fields, no template is a conductor.
 */

/** The conductor Crew Mode runs while the gateway has not said otherwise. */
export const DEFAULT_CREW_MODE_AGENT = 'kirocrew-conductor'

/** The longest steer a person may type into a worker: the server's
 *  `MAX_SEND_MESSAGE_CHARS`, which the steer route and the worker's own composer
 *  send both refuse past (`message_too_long`). */
export const CREW_STEER_MAX_CHARS = 50_000

/** The shipped conductors: what a Crew Mode catalog that names no conductor list
 *  falls back to. */
export const SHIPPED_CONDUCTORS: readonly string[] = [
  DEFAULT_CREW_MODE_AGENT,
  'kirocrew-ledger-conductor',
  'kirocrew-pipeline-conductor',
  'kirocrew-security-conductor',
  'kirocrew-team-lead',
]

let configuredAgent = DEFAULT_CREW_MODE_AGENT
// Empty until a catalog says the gateway runs Crew Mode: a chat restored on a
// conductor template before that read lands, or on a gateway without Crew Mode,
// is an ordinary chat, so no picker row, chord or memory switch treats it otherwise.
let conductors: ReadonlySet<string> = new Set()
let gatewayHasCrewMode = false

/** Record the catalog's Crew Mode facts. Called by `useAgents` on every load.
 *
 *  A catalog that carries NEITHER field comes from a gateway without Crew Mode
 *  (one that predates it). No template is a conductor there: the picker, the
 *  agent chords and the memory switch keep every row and every behaviour they
 *  have today, since there is no Crew Mode for a hidden conductor to be reached
 *  through. A Crew Mode gateway always sends both fields; a list the catalog
 *  leaves out falls back to the shipped conductors. */
export function setCrewModeCatalog(
  agent: string | undefined,
  conductorAgents: readonly string[] | undefined,
): void {
  configuredAgent = agent || DEFAULT_CREW_MODE_AGENT
  if (agent === undefined && conductorAgents === undefined) {
    conductors = new Set()
    gatewayHasCrewMode = false
    return
  }
  gatewayHasCrewMode = true
  conductors = new Set([...(conductorAgents ?? SHIPPED_CONDUCTORS), configuredAgent])
}

/** Whether the gateway has said it runs Crew Mode (its catalog carried the Crew
 *  Mode fields). `false` before the first catalog read and on a gateway without
 *  Crew Mode, where every read that would narrow the person's view to Crew Mode's
 *  rules is left as it is today. */
export function gatewayRunsCrewMode(): boolean {
  return gatewayHasCrewMode
}

/** Back to the state before any catalog landed. For tests, and for nothing else. */
export function resetCrewModeCatalog(): void {
  configuredAgent = DEFAULT_CREW_MODE_AGENT
  conductors = new Set()
  gatewayHasCrewMode = false
}

/** The agent a chat switches to when Crew Mode is turned on. */
export function crewModeAgent(): string {
  return configuredAgent
}

/** Whether `name` is one of the gateway's conductors. */
export function isConductorAgent(name: string | null | undefined): boolean {
  return !!name && conductors.has(name)
}

/** Whether a session on `agent` in namespace `kind` runs a conductor TEMPLATE. A
 *  crewmate that carries a conductor's name is that crewmate, not a conductor. */
export function runsConductor(agent: string | null | undefined, kind: string | null | undefined): boolean {
  return kind !== 'member' && isConductorAgent(agent)
}

/** Whether a session on `agent` in namespace `kind` is in Crew Mode: on any
 *  conductor, or on the agent its `stamp` names (a custom conductor the config no
 *  longer names, until the gateway moves the chat onto the configured one). */
export function runsCrewMode(agent: string | null | undefined, kind: string | null | undefined, stamp?: string | null): boolean {
  if (kind === 'member' || !agent) return false
  return conductors.has(agent) || (!!stamp && stamp === agent)
}

/** Whether `name` is the default agent: the switch is offered only there. The
 *  literal `kirocrew` counts only while no default agent is configured; with
 *  one configured, a chat on the `kirocrew` template is that template. */
export function isDefaultAgentName(name: string, defaultAgent: string | undefined): boolean {
  if (name === '') return true
  return defaultAgent ? name === defaultAgent : name === 'kirocrew'
}

/** Crew Mode needs a Persistent chat: an Incognito or Temporary session may not
 *  open sessions, so a conductor there could not conduct. */
export function memoryModeAllowsCrewMode(memoryMode: string | undefined): boolean {
  return (memoryMode ?? 'persistent') === 'persistent'
}

/** Rows of a normal chat's agent picker: every conductor left out, since the
 *  composer's Crew Mode switch (this part) is the way onto one; crewmates kept
 *  whatever their name. The picker, the Split View pane picker and the keyboard
 *  cycle (`agentCycleTarget`) all read through this one function. On a gateway
 *  without Crew Mode no template is a conductor, so every row passes. */
export function withoutConductors<T extends { name: string; selection_kind?: string }>(choices: T[]): T[] {
  return choices.filter(c => c.selection_kind === 'member' || !isConductorAgent(c.name))
}

/** Every surface that can show a session's pending tool approval. Listed so one
 *  test walks them all; a new surface joins the list and the rule below. */
export const CREW_APPROVAL_SURFACES = [
  'conductor_card',       // the conductor's approval card stack (CrewApprovalCards)
  'worker_main_pane',     // the worker's own composer in the main pane
  'worker_split_pane',    // the worker's composer in a Split View pane
  'conductor_side_panel', // the conductor side panel's Approvals and attention tab
  'session_dashboards',   // All Dashboards' Needs you section
] as const
export type CrewApprovalSurface = typeof CREW_APPROVAL_SURFACES[number]

/** Whether *surface* may draw decision buttons (Approve, Reject, Trust) for
 *  *slot*'s pending tool approval. The one rule, for every surface: a Crew Mode
 *  worker's approval (a row with `crew_root`) is decided ONLY on its conductor's
 *  approval card; every other surface shows it as info with a way to that card.
 *  A session that is no worker keeps its usual controls everywhere. */
export function crewApprovalDecidableHere(
  slot: { crew_root?: string | null } | null | undefined, surface: CrewApprovalSurface,
): boolean {
  if (!slot?.crew_root) return true
  return surface === 'conductor_card'
}

/** The person's own session list: a Crew Mode root's workers are left out, since
 *  the root's Crew panel (the side panel's Crew tab) lists them; the part before
 *  this one wired every person-facing list through here with the drop inert, and
 *  this part, which adds that panel, turns it on. `rootsOnly` keeps just the
 *  roots, for the session filter's Crew Mode chip. */
export function crewModeView<T extends { crew_root?: string; crew_mode_root?: boolean }>(rows: T[], rootsOnly = false): T[] {
  return rows.filter(r => (rootsOnly ? !!r.crew_mode_root : !r.crew_root))
}

/** The agent the keyboard cycle lands on from `current`, `step` places along
 *  (`1` next, `-1` previous), over the same conductor-free list the chat picker
 *  shows, so a chord can never move a normal chat onto a conductor. `null` when
 *  there is nothing to cycle: an empty list, or a chat that already runs a
 *  conductor template, which has no agent picker to mirror. */
export function agentCycleTarget<T extends { name: string; selection_kind?: string }>(
  agents: T[], current: string, currentKind: string | undefined, step: 1 | -1, crewStamp?: string,
): string | null {
  // A Crew Mode chat (every conductor, stamp included) has no picker to mirror.
  if (runsCrewMode(current, currentKind, crewStamp)) return null
  const rows = withoutConductors(agents)
  if (rows.length === 0) return null
  const idx = rows.findIndex(a => a.name === current)
  const next = idx === -1 ? (step === 1 ? 0 : rows.length - 1) : (idx + step + rows.length) % rows.length
  return rows[next].name
}
