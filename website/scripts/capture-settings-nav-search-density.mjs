/**
 * Screenshot harness for the Settings search dropdown's row density in the
 * desktop nav rail (dock 'nav').
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * with SPA fallback and answers every /api/** call from fixtures via Playwright
 * route interception, like capture-settings-search.mjs. No gateway, no auth.
 *
 * For each scene it types a query, measures how many result rows are FULLY
 * visible inside the listbox without scrolling, the listbox height, and the
 * tallest row, prints them, and shoots the viewport. With `--expect-min N`
 * (default off) the run fails when the nav "tool" scene shows fewer than N
 * rows, so the evidence cannot photograph the cramped dropdown again.
 *
 * Scenes:
 *   nav-tool      desktop nav rail, query "tool" (the reported case)
 *   nav-long      desktop nav rail, query "approve" (long labels + long help)
 *   nav-longlabel desktop nav rail, query "redact credentials" (one of the longest labels)
 *   nav-nodesc    desktop nav rail, query "update channel" (an entry with no help text)
 *   nav-de        desktop nav rail, German UI, query "modell" (long non-English labels)
 *   nav-ja        desktop nav rail, Japanese UI, query "モデル"
 *   float-tool    narrow layout floating bottom capsule on the root list, query "tool"
 *                 (unchanged variant)
 *
 * Usage: node scripts/capture-settings-nav-search-density.mjs [outDir] [--expect-min N]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, existsSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { join, extname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const args = process.argv.slice(2)
const minIdx = args.indexOf('--expect-min')
const EXPECT_MIN = minIdx >= 0 ? Number(args[minIdx + 1]) : 0
const OUT = (minIdx === 0 ? undefined : args[0]) || '/tmp/settings-nav-search-density'
// fileURLToPath, not URL.pathname: on Windows .pathname yields "/C:/…".
const DIST = fileURLToPath(new URL('../dist/', import.meta.url))

mkdirSync(OUT, { recursive: true })

const MIME = {
  '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css',
  '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.woff2': 'font/woff2', '.woff': 'font/woff', '.ico': 'image/x-icon',
}

/** Static server with index.html fallback so /settings deep-links resolve. */
function serveDist() {
  return new Promise(resolve => {
    const srv = createServer((req, res) => {
      const rel = decodeURIComponent(new URL(req.url, 'http://x').pathname).replace(/^\/+/, '')
      let file = join(DIST, rel)
      // Loopback-only and ephemeral; an encoded ../ must still not escape dist.
      if (!file.startsWith(DIST) || !rel || !existsSync(file) || statSync(file).isDirectory()) {
        file = join(DIST, 'index.html')
      }
      res.writeHead(200, { 'Content-Type': MIME[extname(file)] || 'application/octet-stream' })
      res.end(readFileSync(file))
    })
    srv.listen(0, '127.0.0.1', () => resolve({ srv, base: `http://127.0.0.1:${srv.address().port}` }))
  })
}

const PROJECT = '/home/user/project'
const fixedApi = makeFixedApi(PROJECT)

const SCENES = [
  { name: 'nav-tool', query: 'tool', viewport: { width: 1400, height: 900 } },
  { name: 'nav-long', query: 'approve', viewport: { width: 1400, height: 900 } },
  { name: 'nav-longlabel', query: 'redact credentials', viewport: { width: 1400, height: 900 } },
  { name: 'nav-nodesc', query: 'update channel', viewport: { width: 1400, height: 900 } },
  { name: 'nav-de', query: 'modell', lang: 'de', viewport: { width: 1400, height: 900 } },
  { name: 'nav-ja', query: 'モデル', lang: 'ja', viewport: { width: 1400, height: 900 } },
  { name: 'float-tool', query: 'tool', path: '/settings', viewport: { width: 390, height: 844 } },
]

const { srv, base } = await serveDist()
const browser = await chromium.launch()
let failed = false
try {
  for (const scene of SCENES) {
    const context = await browser.newContext({ viewport: scene.viewport, deviceScaleFactor: 2 })
    const page = await context.newPage()
    await page.routeWebSocket(/\/api\/ws/, () => {})
    await page.route('**/api/**', route => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/chat/slots') return json(route, [])
      return handleBootRoute(route, path, { project: PROJECT, fixedApi })
    })
    await page.addInitScript(lang => {
      localStorage.clear()
      localStorage.setItem('mc-theme', 'dark')
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-import-onboarded', '1')
      localStorage.setItem('mc-privacy-acked', '1')
      if (lang) localStorage.setItem('mc-lang', lang)
    }, scene.lang ?? null)

    await page.goto(`${base}${scene.path ?? '/settings?tab=chat'}`, { waitUntil: 'domcontentloaded' })
    // The panels' own selects are comboboxes too; the search is the one that
    // controls the results listbox.
    const input = page.locator('input[aria-controls="settings-search-results"]:visible').first()
    try {
      await input.waitFor({ state: 'visible', timeout: 15_000 })
    } catch (err) {
      await page.screenshot({ path: join(OUT, `${scene.name}-FAILED.png`) })
      console.error(`${scene.name}: search input never appeared (${page.url()}); see ${scene.name}-FAILED.png`)
      throw err
    }
    await page.waitForTimeout(600)
    await input.fill(scene.query)
    const listbox = page.locator('#settings-search-results')
    await listbox.waitFor({ state: 'visible', timeout: 5_000 })

    const stats = await listbox.evaluate(lb => {
      const box = lb.getBoundingClientRect()
      const rows = Array.from(lb.querySelectorAll('[role="option"]'))
      const heights = rows.map(r => r.getBoundingClientRect().height)
      const visible = rows.filter(r => r.getBoundingClientRect().bottom <= box.bottom + 0.5).length
      return {
        total: rows.length,
        visible,
        listboxHeight: Math.round(box.height),
        listboxWidth: Math.round(box.width),
        tallestRow: Math.round(Math.max(0, ...heights)),
      }
    })
    const file = join(OUT, `${scene.name}.png`)
    await page.screenshot({ path: file, fullPage: false })
    console.log(`${scene.name}: ${JSON.stringify(stats)} -> ${file}`)
    if (scene.name === 'nav-tool' && EXPECT_MIN && stats.visible < EXPECT_MIN) {
      console.error(`nav-tool shows ${stats.visible} fully visible rows, expected >= ${EXPECT_MIN}`)
      failed = true
    }
    await context.close()
  }
} finally {
  await browser.close()
  srv.close()
}
if (failed) process.exit(1)
