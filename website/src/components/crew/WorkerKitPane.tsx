/**
 * WorkerKitPane — the crewmate editor's "Add abilities" flow.
 *
 * A crewmate's abilities are the sum of its worker's source base plus whatever
 * has been added to its own config. This pane adds more: pick another agent and
 * its connections, skills, resources and auto-approvals are copied in — purely
 * additively. Anything the crewmate already has is left alone and skipped; its
 * prompt is never copied. The add is previewed first and written only on
 * confirm.
 */
import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, MinusCircle, Plus, type LucideIcon } from 'lucide-react'
import { Btn, PanelSectionHeader } from '../ui'
import SimpleSelect from '../SimpleSelect'
import ErrorNotice from '../ErrorNotice'
import { i18nT } from '../../i18n/t'
import { templateDisplayName } from './builtFromName'
import { changeLabel } from './workerKitLabels'
import { workerKitErrorText } from './workerKitErrors'
import { groupWorkerKitChanges } from './workerKitGroups'
import { apiErrorCode } from '../../api/apiError'
import {
  crewCapabilitiesKey,
  crewWorkerKitApi,
  crewWorkerKitKey,
  type WorkerKitChange,
  type WorkerKitPreview,
} from '../../api/crewCapabilities'

// Each change's word as a badge tinted by its hue, with a hue-coloured glyph.
// The word is painted in `text-text`, not the hue: a plain status hue on its
// own tint falls under 4.5:1 in some dark palettes, while the glyph only needs
// the 3:1 non-text floor. The word stays, so the colour is never the only cue.
const CHANGE_BADGES: Record<WorkerKitChange['change'], { label: () => string; tint: string; Icon: LucideIcon; ink: string }> = {
  added: { label: () => i18nT('crewWorkerKit.change_added'), tint: 'bg-ok-subtle', Icon: Plus, ink: 'text-ok-text' },
  skipped: { label: () => i18nT('crewWorkerKit.change_skipped'), tint: 'bg-bg', Icon: MinusCircle, ink: 'text-muted' },
}

function ChangeBadge({ change }: { change: WorkerKitChange['change'] }) {
  const { label, tint, Icon, ink } = CHANGE_BADGES[change]
  return (
    <span
      className={`inline-flex min-w-[5rem] shrink-0 items-center gap-1 rounded-full px-2 py-[2px] text-[11px] font-medium text-text ${tint}`}
      data-testid="worker-kit-change-badge"
      data-change={change}
    >
      <Icon size={12} className={`lucide-inline shrink-0 ${ink}`} aria-hidden="true" />
      {label()}
    </span>
  )
}

export default function WorkerKitPane({
  member,
  name,
  templates,
}: {
  /** The crewmate's key, for the API only — never rendered. */
  member: string
  /** The crewmate's display name. */
  name: string
  /** Installed agent templates whose abilities can be added. */
  templates: string[]
}) {
  const queryClient = useQueryClient()
  // The view only confirms the binding owns a file (and surfaces a load error);
  // there is no recorded template or "additions since" to read anymore.
  const kitQuery = useQuery({
    queryKey: crewWorkerKitKey(member),
    queryFn: () => crewWorkerKitApi.get(member),
    retry: false,
  })
  const kit = kitQuery.data
  const [template, setTemplate] = useState('')
  const [preview, setPreview] = useState<WorkerKitPreview | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<unknown>(null)

  const clear = () => { setTemplate(''); setPreview(null); setErr(null) }

  // Auto-preview the moment an agent is picked: an add is additive, so there is
  // nothing to gate behind an explicit button. `pick` is passed in because the
  // `template` state has not flushed yet when onChange fires. `notice` stays on
  // screen above a successful preview (why it was re-made); a failed preview
  // replaces it with its own error.
  const runPreview = async (pick: string = template, notice: unknown = null) => {
    if (!pick) return
    setBusy(true)
    setErr(notice)
    setPreview(null)
    try {
      setPreview(await crewWorkerKitApi.preview(member, { template: pick }))
    } catch (e) {
      setErr(e)
    } finally {
      setBusy(false)
    }
  }

  const onPick = (v: string) => {
    setTemplate(v)
    setErr(null)
    if (v) void runPreview(v)
    else setPreview(null)
  }

  const apply = async () => {
    if (!preview) return
    setBusy(true)
    setErr(null)
    try {
      await crewWorkerKitApi.apply(member, { template: preview.template, revision: preview.revision })
      setPreview(null)
      setTemplate('')
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: crewWorkerKitKey(member) }),
        queryClient.invalidateQueries({ queryKey: crewCapabilitiesKey(member) }),
      ])
    } catch (e) {
      // The preview no longer matches the file or the template, so it cannot
      // be applied. Re-picking the same agent fires no change, so the pane
      // makes the fresh preview itself and keeps the reason on screen.
      const code = apiErrorCode(e)
      if (code === 'stale_revision' || code === 'revision_required') {
        await runPreview(preview.template, e)
        return
      }
      setErr(e)
    } finally {
      setBusy(false)
    }
  }

  if (kitQuery.isLoading) {
    return (
      <div className="flex items-center gap-2 text-[12px] text-muted">
        <Loader2 size={14} className="lucide-inline animate-spin" />
        {i18nT('crewWorkerKit.loading')}
      </div>
    )
  }

  const added = preview ? preview.changes.filter(c => c.change === 'added') : []

  return (
    <div className="flex flex-col gap-4" style={{ maxWidth: 560 }} data-testid="next-pane-advanced">
      <div className="flex flex-col gap-1">
        <span className="text-[12.5px] font-medium text-text">{i18nT('crewWorkerKit.title')}</span>
        <p className="m-0 text-[12px] leading-relaxed text-muted">{i18nT('crewWorkerKit.body', { name })}</p>
      </div>

      {/* No hand-off: the pane holds an unconfirmed preview; the hand-off would
          navigate away from the open editor and lose it. */}
      <ErrorNotice
        message={workerKitErrorText(kitQuery.error) ?? workerKitErrorText(err)}
        variant="inline"
        askAgent={false}
        testId="worker-kit-error"
      />

      {kit && (
        <>
          <div className="flex flex-col gap-1.5">
            <span className="text-[12px] text-text">{i18nT('crewWorkerKit.template_label')}</span>
            <SimpleSelect
              options={templates}
              optionLabels={templates.map(templateDisplayName)}
              value={template}
              onChange={onPick}
              disabled={busy}
              triggerFallback={i18nT('crewWorkerKit.pick_template')}
              aria-label={i18nT('crewWorkerKit.template_label')}
            />
          </div>

          {busy && !preview && (
            <div className="flex items-center gap-2 text-[12px] text-muted">
              <Loader2 size={14} className="lucide-inline animate-spin" />
              {i18nT('crewWorkerKit.loading')}
            </div>
          )}

          {/* A failed preview leaves the picker on the same agent, where
              re-picking it fires no change: Try again and Cancel are the way on. */}
          {template && !preview && !busy && err != null && (
            <div className="flex items-center gap-2">
              <Btn onClick={clear} data-testid="worker-kit-retry-cancel">
                {i18nT('crewWorkerKit.cancel')}
              </Btn>
              <Btn primary onClick={() => void runPreview()} data-testid="worker-kit-retry">
                {i18nT('crewWorkerKit.retry')}
              </Btn>
            </div>
          )}

          {preview && (
            <div className="flex flex-col gap-3 rounded-md border border-border bg-bg-elevated p-3" data-testid="worker-kit-changes">
              <span className="text-[12.5px] font-medium text-text">
                {added.length === 0
                  ? i18nT('crewWorkerKit.preview_nothing_new', { template: templateDisplayName(preview.template) })
                  : i18nT('crewWorkerKit.preview_adds', { count: added.length, template: templateDisplayName(preview.template) })}
              </span>
              {preview.changes.length === 0 ? (
                <span className="text-[12px] text-muted">{i18nT('crewWorkerKit.no_changes')}</span>
              ) : (
                groupWorkerKitChanges(preview.changes).map(group => (
                  <section key={group.key} className="flex flex-col gap-1.5" data-testid={`worker-kit-group-${group.key}`}>
                    <PanelSectionHeader label={group.heading} count={group.changes.length} />
                    <ul className="m-0 flex list-none flex-col gap-1 p-0">
                      {group.changes.map(c => (
                        <li key={`${c.section}:${c.id}`} className="flex items-center gap-2 text-[12px]">
                          <ChangeBadge change={c.change} />
                          {changeLabel(c) !== null && (
                            <span
                              className={`min-w-0 truncate ${c.change === 'skipped' ? 'text-muted' : 'text-text'}`}
                              title={c.id}
                              data-testid="worker-kit-change-name"
                            >
                              {changeLabel(c)}
                            </span>
                          )}
                        </li>
                      ))}
                    </ul>
                  </section>
                ))
              )}
              <div className="flex items-center gap-2">
                <Btn onClick={clear} disabled={busy} data-testid="worker-kit-cancel">
                  {i18nT('crewWorkerKit.cancel')}
                </Btn>
                <Btn primary onClick={apply} disabled={busy || added.length === 0} data-testid="worker-kit-apply">
                  {added.length === 0 ? i18nT('crewWorkerKit.apply') : i18nT('crewWorkerKit.apply_n', { count: added.length })}
                </Btn>
              </div>
              <p className="m-0 text-[11px] text-muted" data-testid="worker-kit-approvals-hint">
                {preview.runs_unasked || added.some(c => c.section === 'allowedTools')
                  ? i18nT('crewWorkerKit.approvals_hint_granted')
                  : i18nT('crewWorkerKit.approvals_hint')}
              </p>
            </div>
          )}
        </>
      )}
    </div>
  )
}
