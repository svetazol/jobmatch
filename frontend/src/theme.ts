import { ref } from 'vue'
import Aura from '@primevue/themes/aura'
import { definePreset } from '@primevue/themes'

/**
 * The ramps below are the only colour decisions in the app, and they are not
 * taste: they were run through the OKLab/Machado colourblind + WCAG validator
 * and pass as ordinal ramps (monotone lightness, ΔL >= .06 per step, light end
 * clears the surface, single hue) in BOTH modes. Do not hand-edit a step
 * without re-validating the set.
 *
 * Fit is the only thing that gets colour: one hue, light -> dark. Pitch
 * identity is carried by its label, never by hue — four categorical hues
 * beside a five-step ramp is how a chart becomes unreadable.
 */
export const FIT_RAMP = {
  light: ['#86b6ef', '#5598e7', '#2a78d6', '#1c5cab', '#104281'],
  dark: ['#184f95', '#256abf', '#3987e5', '#6da7ec', '#9ec5f4'],
} as const

/** Everything that is not a fit level. */
export const NEUTRAL = {
  light: { dim: '#b9b8b1', rule: '#e1e0d9', surface: '#fcfcfb' },
  dark: { dim: '#4a4a47', rule: '#2c2c2a', surface: '#1a1a19' },
} as const

/**
 * Which mode is on, as a ref rather than a read of the DOM class.
 *
 * `ramp()` and `neutral()` are called inside the charts' `computed()`s, and a
 * computed only re-runs when something reactive it read has changed. Reading
 * `classList` gave it nothing to depend on, so toggling the theme left every
 * chart holding the other mode's colours until an unrelated re-render — the
 * light ramp runs light to dark and the dark one runs the other way, so the
 * bars came out inverted, with near-white segment borders across them.
 */
export const dark = ref(
  typeof document !== 'undefined'
  && document.documentElement.classList.contains('dark'),
)

/** The one writer. Owns the class, the ref and the stored preference. */
export function setDark(on: boolean): void {
  dark.value = on
  document.documentElement.classList.toggle('dark', on)
  try { localStorage.setItem('theme', on ? 'dark' : 'light') } catch { /* private mode */ }
}

export const isDark = () => dark.value

export const ramp = () => (isDark() ? FIT_RAMP.dark : FIT_RAMP.light)
export const neutral = () => (isDark() ? NEUTRAL.dark : NEUTRAL.light)

export default definePreset(Aura, {
  semantic: {
    primary: {
      50: '#eef4fc', 100: '#cde2fb', 200: '#9ec5f4', 300: '#6da7ec',
      400: '#3987e5', 500: '#2a78d6', 600: '#256abf', 700: '#1c5cab',
      800: '#184f95', 900: '#104281', 950: '#0d366b',
    },
    colorScheme: {
      light: {
        surface: {
          0: '#ffffff', 50: '#f9f9f7', 100: '#f0efec', 200: '#e1e0d9',
          300: '#c3c2b7', 400: '#b9b8b1', 500: '#898781', 600: '#6e6d68',
          700: '#52514e', 800: '#3a3936', 900: '#1f1f1d', 950: '#0b0b0b',
        },
      },
      dark: {
        surface: {
          0: '#1a1a19', 50: '#1f1f1d', 100: '#2c2c2a', 200: '#3a3936',
          300: '#4a4a47', 400: '#6e6d68', 500: '#898781', 600: '#a8a79f',
          700: '#c3c2b7', 800: '#dcdbd3', 900: '#f0efec', 950: '#ffffff',
        },
      },
    },
  },
  components: {
    card: { body: { padding: '1.25rem 1.375rem' } },
  },
})
