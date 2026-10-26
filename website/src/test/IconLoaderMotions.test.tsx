import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { ICON_MOTIONS, IconMotionLoader, pickIconMotion } from '../components/IconLoaderMotions'

const Mark = () => <i className="mark" />

describe('pickIconMotion', () => {
  it('can land on the carousel and on every motion, and stays in range', () => {
    const seen = new Set<string>()
    for (let i = 0; i < 100; i++) seen.add(pickIconMotion(() => i / 100))
    expect(seen).toEqual(new Set(['carousel', ...ICON_MOTIONS]))
    expect(pickIconMotion(() => 0.9999999)).toBe(ICON_MOTIONS[ICON_MOTIONS.length - 1])
  })
})

describe('IconMotionLoader', () => {
  it.each(ICON_MOTIONS)('%s renders one aria-hidden slot per pick, at most four', motion => {
    const { getByTestId } = render(<IconMotionLoader motion={motion} icons={[Mark, Mark, Mark, Mark, Mark]} picks={[4, 0, 2, 1, 3]} />)
    const root = getByTestId('loader-motion')
    expect(root).toHaveAttribute('data-motion', motion)
    expect(root).toHaveAttribute('aria-hidden', 'true')
    expect(root.querySelectorAll('.ilm-slot .mark')).toHaveLength(4)
    expect(root.querySelector('.ilm-dot') !== null).toBe(motion === 'spotlight')
  })

  it('wraps picks from a larger pool instead of rendering undefined icons', () => {
    const { getByTestId } = render(<IconMotionLoader motion="drop" icons={[Mark, Mark]} picks={[5, 6, 7, 8]} />)
    expect(getByTestId('loader-motion').querySelectorAll('.mark')).toHaveLength(4)
  })

  it('renders nothing for an empty pool', () => {
    const { container } = render(<IconMotionLoader motion="shuffle" icons={[]} picks={[0, 1]} />)
    expect(container.innerHTML).toBe('')
  })
})
