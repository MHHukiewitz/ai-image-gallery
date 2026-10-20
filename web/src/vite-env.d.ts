/// <reference types="vite/client" />

/** Build-time, public variables. See `# web` in the root .env.example. Vite exposes them as strings. */
interface ImportMetaEnv {
  readonly VITE_SUPABASE_URL: string;
  readonly VITE_SUPABASE_PUBLISHABLE_KEY: string;
  /** Where the API lives. Defaults to "/api" (same-origin) when unset. */
  readonly VITE_API_BASE?: string;
  /** "true" serves /api from MSW mocks, so the app runs without a backend. */
  readonly VITE_USE_MOCKS?: string;
  /** Client-side upload limit in MB. Keep in step with the API's MAX_UPLOAD_MB. */
  readonly VITE_MAX_UPLOAD_MB?: string;
  /** Empty or unset disables Sentry. */
  readonly VITE_SENTRY_DSN?: string;
  readonly VITE_SENTRY_ENVIRONMENT?: string;
  readonly VITE_RELEASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
