/// <reference types="vite/client" />

// No `declare module '*.vue'` here on purpose: a wildcard declaration shadows
// the per-component types vue-tsc generates, so every SFC import becomes
// `any` and a wrong or missing prop stops failing the build — which is most
// of what `strict` in tsconfig.json was turned on for.

interface ImportMetaEnv {
  readonly VITE_USE_MOCK?: string
}
interface ImportMeta {
  readonly env: ImportMetaEnv
}
