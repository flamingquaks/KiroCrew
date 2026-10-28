import { describe, expect, it } from 'vitest'
import { sameCompanyPortal } from './companySignIn'

const PORTAL_VARIANTS = [
  'example.awsapps.com',
  'https://example.awsapps.com',
  'https://example.awsapps.com/',
  'https://example.awsapps.com/start',
  ' HTTPS://EXAMPLE.awsapps.com/START/ ',
  'example.awsapps.com/start///',
]

describe('sameCompanyPortal', () => {
  it.each(PORTAL_VARIANTS)('matches equivalent portal forms for %s in both directions', portal => {
    for (const other of PORTAL_VARIANTS) {
      expect(sameCompanyPortal(portal, other)).toBe(true)
      expect(sameCompanyPortal(other, portal)).toBe(true)
    }
  })

  it.each([
    ['https://example.awsapps.com', 'https://other.awsapps.com/start'],
    ['https://example.awsapps.com/start', 'https://example.awsapps.com/custom'],
    ['', 'https://example.awsapps.com/start'],
    ['https://example.awsapps.com/start', ''],
    ['', ''],
    ['   ', '   '],
  ])('rejects distinct or empty portals: %s and %s', (a, b) => {
    expect(sameCompanyPortal(a, b)).toBe(false)
  })
})
