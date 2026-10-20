import { render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  AppErrorBoundary,
  initObservability,
  scrubBreadcrumb,
  scrubEvent,
  scrubUrl,
} from "./observability";

// Only observability.tsx may import Sentry, so the test reaches the SDK through the mock.
const sentry = vi.hoisted(() => ({ init: vi.fn() }));
vi.mock("@sentry/react", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@sentry/react")>()),
  init: sentry.init,
}));

type ErrorEvent = Parameters<typeof scrubEvent>[0];
type Breadcrumb = Parameters<typeof scrubBreadcrumb>[0];

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
  sentry.init.mockClear();
});

const SIGNED_URL =
  "https://abc.supabase.co/storage/v1/object/sign/images/u1/p1/thumb.webp?token=eyJhbGciOi.payload.sig";

describe("scrubUrl", () => {
  it("redacts the token and keeps the path and other parameters", () => {
    expect(scrubUrl(`${SIGNED_URL}&download=1`)).toBe(
      "https://abc.supabase.co/storage/v1/object/sign/images/u1/p1/thumb.webp?token=[redacted]&download=1",
    );
  });

  it("redacts a token that isn't the first parameter", () => {
    expect(scrubUrl("/x?a=1&token=secret&b=2")).toBe("/x?a=1&token=[redacted]&b=2");
  });

  it("redacts every token in a string", () => {
    expect(scrubUrl("GET /a?token=one failed, then /b?token=two")).toBe(
      "GET /a?token=[redacted] failed, then /b?token=[redacted]",
    );
  });

  it("redacts the access and refresh tokens Supabase puts in a URL fragment", () => {
    expect(scrubUrl("/#access_token=abc&refresh_token=def&type=signup")).toBe(
      "/#access_token=[redacted]&refresh_token=[redacted]&type=signup",
    );
  });

  it("leaves text without a token unchanged", () => {
    expect(scrubUrl("/?q=token+ring&similar_to=4")).toBe("/?q=token+ring&similar_to=4");
  });
});

describe("scrubEvent", () => {
  const event = {
    type: undefined,
    message: `Image failed to load: ${SIGNED_URL}`,
    request: {
      url: `https://gallery.example.com/?image=3`,
      headers: { Authorization: "Bearer test-key", cookie: "sb=1", Accept: "application/json" },
    },
    breadcrumbs: [{ category: "xhr", data: { url: SIGNED_URL, status_code: 200 } }],
    extra: { urls: [SIGNED_URL] },
  } as unknown as ErrorEvent;

  it("redacts tokens anywhere in the event", () => {
    const scrubbed = JSON.stringify(scrubEvent(event));

    expect(scrubbed).not.toContain("eyJhbGciOi");
    expect(scrubbed.match(/token=\[redacted\]/g)).toHaveLength(3);
    expect(scrubbed).toContain("status_code");
  });

  it("drops the Authorization and Cookie headers and keeps the rest", () => {
    const { request } = scrubEvent(event);

    expect(request?.headers).toEqual({ Accept: "application/json" });
    expect(request?.url).toBe("https://gallery.example.com/?image=3");
  });

  it("returns a copy and leaves the original alone", () => {
    scrubEvent(event);

    expect(event.request?.headers).toHaveProperty("Authorization");
    expect(event.message).toContain("eyJhbGciOi");
  });

  it("copes with a cycle", () => {
    const cyclic: Record<string, unknown> = { name: "loop" };
    cyclic.self = cyclic;

    expect(() => scrubEvent({ extra: cyclic } as unknown as ErrorEvent)).not.toThrow();
  });
});

describe("scrubBreadcrumb", () => {
  it("redacts the URL in a fetch breadcrumb", () => {
    const breadcrumb: Breadcrumb = { category: "fetch", data: { url: SIGNED_URL, method: "GET" } };

    const scrubbed = scrubBreadcrumb(breadcrumb);

    expect(scrubbed.data).toEqual({
      url: expect.stringContaining("token=[redacted]"),
      method: "GET",
    });
  });
});

describe("initObservability", () => {
  it("does nothing without a DSN", () => {
    vi.stubEnv("VITE_SENTRY_DSN", "");

    initObservability();

    expect(sentry.init).not.toHaveBeenCalled();
  });

  it("starts Sentry with privacy-preserving options when there is a DSN", () => {
    vi.stubEnv("VITE_SENTRY_DSN", "https://key@o0.ingest.de.sentry.io/1");
    vi.stubEnv("VITE_SENTRY_ENVIRONMENT", "production");
    vi.stubEnv("VITE_RELEASE", "abc123");

    initObservability();

    expect(sentry.init).toHaveBeenCalledOnce();
    const options = sentry.init.mock.calls[0][0];
    expect(options).toMatchObject({
      dsn: "https://key@o0.ingest.de.sentry.io/1",
      environment: "production",
      release: "abc123",
      dataCollection: { userInfo: false, cookies: false, httpHeaders: false, httpBodies: [] },
      beforeSend: scrubEvent,
      beforeBreadcrumb: scrubBreadcrumb,
    });
    // A replay would record users' private photos, and tracing isn't wanted either.
    expect(options).not.toHaveProperty("integrations");
    expect(options).not.toHaveProperty("tracesSampleRate");
    expect(options).not.toHaveProperty("replaysSessionSampleRate");
    expect(options).not.toHaveProperty("replaysOnErrorSampleRate");
  });
});

describe("AppErrorBoundary", () => {
  function Broken(): never {
    throw new Error("render failed");
  }

  it("shows a reload prompt instead of a blank page when a child throws", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);

    render(createElement(AppErrorBoundary, null, createElement(Broken)));

    expect(screen.getByRole("alert")).toHaveTextContent("Something went wrong");
    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
  });

  it("renders its children when nothing throws", () => {
    render(createElement(AppErrorBoundary, null, createElement("p", null, "all good")));

    expect(screen.getByText("all good")).toBeInTheDocument();
  });
});
