import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { LocalOverlayRoot } from '../components/overlayRoot'

/**
 * While "Optimize prompt" runs, the composer is read-only and Send is disabled
 * until the request settles. A mis-tap on the optimize button (it sits next to
 * Send on a phone) used to leave no way out short of waiting: the overlay gave
 * no control, so a slow or hung optimizer held the draft hostage. The overlay
 * now carries a Cancel button that aborts the request and hands the draft back
 * untouched.
 */

const ORIGINAL = 'my prompt'

// The Lexical composer is a React.lazy chunk (chat-input/engine.tsx), so its
// first mount can outlast findBy's 1000 ms default on a loaded runner.
const LAZY_COMPOSER_MOUNT = { timeout: 5000 }

/** An optimizer request that never answers on its own, but honours abort. */
const hangingOptimizer = (signals: AbortSignal[]) =>
  vi.fn((url: string, init?: RequestInit) => {
    if (typeof url === 'string' && url.includes('/api/optimizer/optimize')) {
      return new Promise((_resolve, reject) => {
        const signal = init?.signal
        if (signal) {
          signals.push(signal)
          signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
        }
      })
    }
    return Promise.resolve({ ok: true, json: async () => [] })
  })

const clickOptimize = () =>
  fireEvent.click(screen.getByRole('button', { name: 'Optimize prompt' }))

describe('ChatInput optimize: the in-flight overlay can be cancelled', () => {
  beforeEach(() => {
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      cb(0)
      return 0
    })
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('aborts the request and returns the draft, editable, with no error', async () => {
    const signals: AbortSignal[] = []
    const onChange = vi.fn()
    vi.stubGlobal('fetch', hangingOptimizer(signals))

    renderWithProviders(
      <ChatInput value={ORIGINAL} onChange={onChange} onSend={vi.fn()} connected={true} />,
    )
    clickOptimize()

    const textbox = screen.getByRole('textbox') as HTMLTextAreaElement
    await vi.waitFor(() => expect(textbox.readOnly).toBe(true))

    const cancel = await screen.findByRole('button', { name: 'Cancel optimizing' })
    await act(async () => { fireEvent.click(cancel) })

    expect(signals).toHaveLength(1)
    expect(signals[0].aborted).toBe(true)
    await vi.waitFor(() => expect(screen.queryByRole('button', { name: 'Cancel optimizing' })).toBeNull())
    expect(textbox.readOnly).toBe(false)
    expect(screen.queryByTestId('optimize-error')).toBeNull()
    // The draft is the user's own text: a cancel writes nothing back.
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Optimize prompt' })).not.toBeDisabled()
  })

  it('puts focus back in the composer so the keyboard returns without a second tap', async () => {
    vi.stubGlobal('fetch', hangingOptimizer([]))
    renderWithProviders(
      <ChatInput value={ORIGINAL} onChange={vi.fn()} onSend={vi.fn()} connected={true} />,
    )
    clickOptimize()
    const cancel = await screen.findByRole('button', { name: 'Cancel optimizing' })
    cancel.focus()
    await act(async () => { fireEvent.click(cancel) })

    const textbox = screen.getByRole('textbox') as HTMLTextAreaElement
    expect(document.activeElement).toBe(textbox)
    expect(textbox.selectionStart).toBe(ORIGINAL.length)
  })

  it('makes the Lexical composer editable inside the tap, so a phone raises its keyboard', async () => {
    vi.stubGlobal('fetch', hangingOptimizer([]))
    renderWithProviders(
      <ChatInput value={ORIGINAL} onChange={vi.fn()} onSend={vi.fn()} connected={true} lexicalComposer />,
    )
    const root = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
    clickOptimize()
    await vi.waitFor(() => expect(root.getAttribute('contenteditable')).toBe('false'))
    const cancel = await screen.findByRole('button', { name: 'Cancel optimizing' })

    // Record the attribute at the moment focus() is called: a mobile browser
    // only raises the keyboard for a focus() on an element that is already
    // editable while the user's gesture is still running, before React
    // re-renders the composer out of read-only.
    let editableAtFocus: string | null = null
    const realFocus = root.focus.bind(root)
    vi.spyOn(root, 'focus').mockImplementation((opts?: FocusOptions) => {
      editableAtFocus ??= root.getAttribute('contenteditable')
      realFocus(opts)
    })
    await act(async () => { fireEvent.click(cancel) })

    expect(editableAtFocus).toBe('true')
    expect(document.activeElement).toBe(root)
  })

  it('leaves the Lexical composer non-editable when the host disabled it during the optimize', async () => {
    vi.stubGlobal('fetch', hangingOptimizer([]))
    const props = { value: ORIGINAL, onChange: vi.fn(), onSend: vi.fn(), connected: true, lexicalComposer: true }
    const { rerender } = renderWithProviders(<ChatInput {...props} />)
    const root = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
    clickOptimize()
    await screen.findByRole('button', { name: 'Cancel optimizing' })
    rerender(<ChatInput {...props} disabled />)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Cancel optimizing' })) })

    // Lexical's setEditable(false) is a no-op on an editor that is already
    // non-editable, so a DOM write here would never be undone.
    expect(root.getAttribute('contenteditable')).toBe('false')
  })

  it('leaves the textarea read-only when the host disabled it during the optimize', async () => {
    vi.stubGlobal('fetch', hangingOptimizer([]))
    const props = { value: ORIGINAL, onChange: vi.fn(), onSend: vi.fn(), connected: true }
    const { rerender } = renderWithProviders(<ChatInput {...props} />)
    clickOptimize()
    await screen.findByRole('button', { name: 'Cancel optimizing' })
    rerender(<ChatInput {...props} disabled />)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Cancel optimizing' })) })

    // readOnly={optimizing || disabled} stays true across the settle, so React
    // never rewrites the attribute and a DOM write here would never be undone.
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).readOnly).toBe(true)
  })

  it('cancels on Escape in the prompt box, and that Escape does not stop the running turn', async () => {
    const signals: AbortSignal[] = []
    const onStop = vi.fn()
    vi.stubGlobal('fetch', hangingOptimizer(signals))
    const props = { value: ORIGINAL, onChange: vi.fn(), onSend: vi.fn(), connected: true, onStop }
    const { rerender } = renderWithProviders(<ChatInput {...props} />)
    clickOptimize()
    await screen.findByRole('button', { name: 'Cancel optimizing' })
    // A turn starts on the slot (a queued send) while the optimize is in flight.
    rerender(<ChatInput {...props} isRunning />)
    const textbox = screen.getByRole('textbox') as HTMLTextAreaElement
    await act(async () => { fireEvent.keyDown(textbox, { key: 'Escape' }) })

    expect(signals).toHaveLength(1)
    expect(signals[0].aborted).toBe(true)
    expect(onStop).not.toHaveBeenCalled()
    await vi.waitFor(() => expect(screen.queryByRole('button', { name: 'Cancel optimizing' })).toBeNull())
    expect(textbox.readOnly).toBe(false)
  })

  describe('Escape while the rich-text composer is locked for an optimize', () => {
    const mountLexical = async (signals: AbortSignal[], extra: Record<string, unknown> = {}) => {
      vi.stubGlobal('fetch', hangingOptimizer(signals))
      const onStop = vi.fn()
      const props = { value: ORIGINAL, onChange: vi.fn(), onSend: vi.fn(), connected: true, lexicalComposer: true, onStop, ...extra }
      const view = renderWithProviders(<ChatInput {...props} />)
      const root = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
      // The user is working in this composer when the optimize starts.
      act(() => { root.focus() })
      clickOptimize()
      await vi.waitFor(() => expect(root.getAttribute('contenteditable')).toBe('false'))
      view.rerender(<ChatInput {...props} isRunning />)
      return { root, onStop }
    }

    it('cancels the optimize and does not stop the running turn', async () => {
      const signals: AbortSignal[] = []
      const { root, onStop } = await mountLexical(signals)
      await act(async () => { fireEvent.keyDown(root, { key: 'Escape' }) })

      expect(signals).toHaveLength(1)
      expect(signals[0].aborted).toBe(true)
      expect(onStop).not.toHaveBeenCalled()
      await vi.waitFor(() => expect(screen.queryByRole('button', { name: 'Cancel optimizing' })).toBeNull())
    })

    it('leaves the optimize running when an IME composition owns the Escape', async () => {
      const signals: AbortSignal[] = []
      const { root, onStop } = await mountLexical(signals)
      await act(async () => { fireEvent.keyDown(root, { key: 'Escape', isComposing: true }) })

      expect(signals[0].aborted).toBe(false)
      expect(onStop).not.toHaveBeenCalled()
      expect(screen.getByRole('button', { name: 'Cancel optimizing' })).toBeInTheDocument()
    })

    it('leaves the optimize running when a dialog owns the Escape', async () => {
      const signals: AbortSignal[] = []
      const { root } = await mountLexical(signals)
      const dialog = document.createElement('div')
      dialog.setAttribute('role', 'dialog')
      document.body.appendChild(dialog)
      try {
        await act(async () => { fireEvent.keyDown(root, { key: 'Escape' }) })
        expect(signals[0].aborted).toBe(false)
      } finally {
        dialog.remove()
      }
    })

    it('leaves the optimize running when the Escape is aimed outside this composer', async () => {
      const signals: AbortSignal[] = []
      await mountLexical(signals)
      // Another pane's control or a sidebar field holds focus.
      const elsewhere = document.createElement('input')
      document.body.appendChild(elsewhere)
      try {
        await act(async () => { fireEvent.keyDown(elsewhere, { key: 'Escape' }) })
        expect(signals[0].aborted).toBe(false)
      } finally {
        elsewhere.remove()
      }
    })

    it('cancels when the locked editor has dropped focus to the page body', async () => {
      const signals: AbortSignal[] = []
      await mountLexical(signals)
      await act(async () => { fireEvent.keyDown(document.body, { key: 'Escape' }) })
      expect(signals[0].aborted).toBe(true)
    })

    it('leaves a page-body Escape alone once another pane has taken focus', async () => {
      const signals: AbortSignal[] = []
      await mountLexical(signals)
      // Another pane's composer takes focus, then drops it to the page body.
      const otherPane = document.createElement('button')
      document.body.appendChild(otherPane)
      act(() => { otherPane.focus() })
      otherPane.remove()
      await act(async () => { fireEvent.keyDown(document.body, { key: 'Escape' }) })
      expect(signals[0].aborted).toBe(false)
    })

    it('marks the Escape spent before the window-level notification banner sees it', async () => {
      const signals: AbortSignal[] = []
      // The banner was listening (on window, like NotificationBanner) before the optimize started.
      const seenByBanner: boolean[] = []
      const banner = (e: KeyboardEvent) => { seenByBanner.push(e.defaultPrevented) }
      window.addEventListener('keydown', banner)
      try {
        await mountLexical(signals)
        const cancel = screen.getByRole('button', { name: 'Cancel optimizing' })
        await act(async () => { fireEvent.keyDown(cancel, { key: 'Escape' }) })
      } finally {
        window.removeEventListener('keydown', banner)
      }
      expect(signals[0].aborted).toBe(true)
      expect(seenByBanner).toEqual([true])
    })

    it('lets a capture-phase owner opened mid-optimize, such as the sidebar flyout, take the Escape', async () => {
      const signals: AbortSignal[] = []
      const { root } = await mountLexical(signals)
      // useHoverIntent's contract: claim Escape in the capture phase and stop it there.
      let flyoutClosed = false
      const flyout = (e: KeyboardEvent) => {
        if (e.key !== 'Escape') return
        e.preventDefault()
        e.stopPropagation()
        flyoutClosed = true
      }
      document.addEventListener('keydown', flyout, true)
      try {
        await act(async () => { fireEvent.keyDown(root, { key: 'Escape' }) })
      } finally {
        document.removeEventListener('keydown', flyout, true)
      }
      expect(flyoutClosed).toBe(true)
      expect(signals[0].aborted).toBe(false)
    })

    it('leaves a page-body Escape alone while a remote Crew is in front of this one', async () => {
      const signals: AbortSignal[] = []
      vi.stubGlobal('fetch', hangingOptimizer(signals))
      const props = { value: ORIGINAL, onChange: vi.fn(), onSend: vi.fn(), connected: true, lexicalComposer: true }
      const view = renderWithProviders(<LocalOverlayRoot hidden={false}><ChatInput {...props} /></LocalOverlayRoot>)
      const root = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
      act(() => { root.focus() })
      clickOptimize()
      await vi.waitFor(() => expect(root.getAttribute('contenteditable')).toBe('false'))
      // The user switches to a remote Crew while the optimize runs.
      view.rerender(<LocalOverlayRoot hidden><ChatInput {...props} /></LocalOverlayRoot>)
      await act(async () => { fireEvent.keyDown(document.body, { key: 'Escape' }) })
      expect(signals[0].aborted).toBe(false)
    })

    it('removes the listener once the optimize ends, so a later Escape reaches nothing', async () => {
      const signals: AbortSignal[] = []
      const { onStop } = await mountLexical(signals)
      await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Cancel optimizing' })) })
      await vi.waitFor(() => expect(screen.queryByRole('button', { name: 'Cancel optimizing' })).toBeNull())
      await act(async () => { fireEvent.keyDown(document.body, { key: 'Escape' }) })

      expect(onStop).not.toHaveBeenCalled()
    })
  })

  it('cancels once on one Escape in the plain prompt box, never twice', async () => {
    const signals: AbortSignal[] = []
    const onStop = vi.fn()
    vi.stubGlobal('fetch', hangingOptimizer(signals))
    const props = { value: ORIGINAL, onChange: vi.fn(), onSend: vi.fn(), connected: true, onStop }
    const { rerender } = renderWithProviders(<ChatInput {...props} />)
    clickOptimize()
    await screen.findByRole('button', { name: 'Cancel optimizing' })
    rerender(<ChatInput {...props} isRunning />)
    const textbox = screen.getByRole('textbox') as HTMLTextAreaElement
    const seenByDocument: boolean[] = []
    const probe = (e: KeyboardEvent) => { seenByDocument.push(e.defaultPrevented) }
    document.addEventListener('keydown', probe)
    try {
      await act(async () => { fireEvent.keyDown(textbox, { key: 'Escape' }) })
    } finally {
      document.removeEventListener('keydown', probe)
    }

    // The textarea's own gate claimed the key, so the document listener skips it.
    expect(seenByDocument).toEqual([true])
    expect(signals).toHaveLength(1)
    expect(signals[0].aborted).toBe(true)
    expect(onStop).not.toHaveBeenCalled()
  })

  it('drops a rewrite whose body was already read when Cancel landed', async () => {
    const onChange = vi.fn()
    let answer: (v: unknown) => void = () => {}
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      if (typeof url === 'string' && url.includes('/api/optimizer/optimize')) {
        // The response is in hand; only its body read is still pending, and
        // that read ignores the abort signal.
        return Promise.resolve({
          ok: true,
          json: () => new Promise((resolve) => { answer = resolve }),
        })
      }
      return Promise.resolve({ ok: true, json: async () => [] })
    }))
    renderWithProviders(
      <ChatInput value={ORIGINAL} onChange={onChange} onSend={vi.fn()} connected={true} />,
    )
    clickOptimize()
    const cancel = await screen.findByRole('button', { name: 'Cancel optimizing' })
    await act(async () => { fireEvent.click(cancel) })
    await act(async () => { answer({ changed: true, optimized: 'a rewrite' }) })

    await vi.waitFor(() => expect(screen.queryByRole('button', { name: 'Cancel optimizing' })).toBeNull())
    expect(onChange).not.toHaveBeenCalled()
  })
})
