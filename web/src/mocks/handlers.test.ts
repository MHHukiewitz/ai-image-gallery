// @vitest-environment node
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import type { components } from "../lib/api-types";
import { hexToLab, deltaE } from "./colors";
import { ANALYSIS_DONE_AFTER_MS, MockStore, PROCESSING_AFTER_MS } from "./data";
import { createHandlers } from "./handlers";

type ImageOut = components["schemas"]["ImageOut"];
type ImagePage = components["schemas"]["ImagePage"];
type ErrorResponse = components["schemas"]["ErrorResponse"];

const BASE = "http://localhost";
let store = new MockStore();
const server = setupServer();

beforeAll(() => server.listen({ onUnhandledFrame: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

/** A fresh store per test, no fake latency. */
function useFreshStore() {
  store = new MockStore();
  server.use(...createHandlers({ store, latencyMs: 0, baseUrl: BASE }));
}

async function listPage(search = ""): Promise<{ response: Response; page: ImagePage }> {
  const response = await fetch(`${BASE}/api/v1/images${search}`);
  return { response, page: (await response.json()) as ImagePage };
}

function uploadForm(name: string, type: string, size = 10): RequestInit {
  const form = new FormData();
  form.append("file", new File([new Uint8Array(size)], name, { type }));
  return { method: "POST", body: form };
}

describe("list", () => {
  it("serves 45 seeded images in pages of 20, newest first", async () => {
    useFreshStore();

    const first = await listPage();
    const second = await listPage("?offset=20");
    const third = await listPage("?offset=40");

    expect([first, second, third].map(({ page }) => page.items.length)).toEqual([20, 20, 5]);
    expect([first, second, third].map(({ page }) => page.next_offset)).toEqual([20, 40, null]);
    const ids = [first, second, third].flatMap(({ page }) => page.items.map((item) => item.id));
    expect(ids).toEqual(Array.from({ length: 45 }, (_, index) => 45 - index));
  });

  it("gives every image a thumbnail data URL, tags, a description and 3 colors", async () => {
    useFreshStore();

    const { page } = await listPage();

    for (const image of page.items) {
      expect(image.thumbnail_url).toMatch(/^data:image\/svg\+xml,/);
      expect(image.analysis).toMatchObject({ status: "done", error: null });
      expect(image.analysis.tags.length).toBeGreaterThanOrEqual(5);
      expect(image.analysis.description).toBeTruthy();
      expect(image.analysis.colors).toHaveLength(3);
      expect(image).not.toHaveProperty("score");
      expect(image).not.toHaveProperty("original_url");
    }
  });

  it("rejects more than one filter, and bad parameters, with the error shape", async () => {
    useFreshStore();

    for (const search of ["?q=a&color=%23aabbcc", "?color=red", "?limit=51", "?offset=-1", "?q="]) {
      const { response, page } = await listPage(search);

      expect(response.status, search).toBe(422);
      expect((page as unknown as ErrorResponse).error.code).toBe("validation_error");
    }
  });
});

describe("search modes", () => {
  it("text search matches tag prefixes and ranks exact tags first", async () => {
    useFreshStore();

    const { page } = await listPage("?q=beac");

    expect(page.items.length).toBeGreaterThan(0);
    for (const image of page.items) {
      const searchable = [...image.analysis.tags, image.analysis.description ?? ""].join(" ");
      expect(searchable).toMatch(/\bbeac/);
      expect(image.score).toBeGreaterThan(0);
    }
    expect(page.items.map((image) => image.score)).toEqual(
      [...page.items.map((image) => image.score)].sort((a, b) => (b ?? 0) - (a ?? 0)),
    );
  });

  it("text search finds nothing for a word nobody has", async () => {
    useFreshStore();

    const { page } = await listPage("?q=zebra");

    expect(page).toEqual({ items: [], next_offset: null });
  });

  it("similar_to excludes the target and ranks the closest first", async () => {
    useFreshStore();
    const target = store.get(1) as ImageOut;

    const { page } = await listPage("?similar_to=1");

    expect(page.items.length).toBeGreaterThan(0);
    expect(page.items.map((image) => image.id)).not.toContain(1);
    const scores = page.items.map((image) => image.score ?? 0);
    expect(scores.every((score) => score > 0.1)).toBe(true);
    expect(scores).toEqual([...scores].sort((a, b) => b - a));
    const shared = (image: ImageOut) =>
      image.analysis.tags.filter((tag) => target.analysis.tags.includes(tag)).length;
    expect(shared(page.items[0])).toBeGreaterThan(0);
  });

  it("color search keeps images with a color near the one asked for", async () => {
    useFreshStore();
    const wanted = (store.get(3) as ImageOut).analysis.colors[0];

    const { page } = await listPage(`?color=${encodeURIComponent(wanted)}`);

    expect(page.items.map((image) => image.id)).toContain(3);
    for (const image of page.items) {
      const nearest = Math.min(
        ...image.analysis.colors.map((hex) => deltaE(hexToLab(wanted), hexToLab(hex))),
      );
      expect(nearest).toBeLessThanOrEqual(25);
    }
  });
});

describe("detail, delete and reanalyze", () => {
  it("returns the original_url on the detail endpoint", async () => {
    useFreshStore();

    const response = await fetch(`${BASE}/api/v1/images/5`);
    const image = (await response.json()) as ImageOut;

    expect(response.status).toBe(200);
    expect(image.id).toBe(5);
    expect(image.original_url).toMatch(/^data:image\/svg\+xml,/);
  });

  it("answers 404 in the error shape for an unknown image", async () => {
    useFreshStore();

    const response = await fetch(`${BASE}/api/v1/images/999`);

    expect(response.status).toBe(404);
    expect(((await response.json()) as ErrorResponse).error.code).toBe("not_found");
  });

  it("deletes an image: 204 once, then 404", async () => {
    useFreshStore();

    const first = await fetch(`${BASE}/api/v1/images/5`, { method: "DELETE" });
    const second = await fetch(`${BASE}/api/v1/images/5`, { method: "DELETE" });
    const { page } = await listPage("?limit=50");

    expect(first.status).toBe(204);
    expect(second.status).toBe(404);
    expect(page.items).toHaveLength(44);
  });

  it("queues a reanalysis with 202 and no body", async () => {
    useFreshStore();

    const response = await fetch(`${BASE}/api/v1/images/5/reanalyze`, { method: "POST" });
    const image = (await (await fetch(`${BASE}/api/v1/images/5`)).json()) as ImageOut;

    expect(response.status).toBe(202);
    expect(await response.text()).toBe("");
    expect(image.analysis.status).toBe("pending");
  });
});

describe("upload", () => {
  it("creates a pending image at the top of the list", async () => {
    useFreshStore();

    const response = await fetch(`${BASE}/api/v1/images`, uploadForm("sunset.jpg", "image/jpeg"));
    const created = (await response.json()) as ImageOut;
    const { page } = await listPage();

    expect(response.status).toBe(201);
    expect(created).toMatchObject({ id: 46, filename: "sunset.jpg" });
    expect(created.analysis).toMatchObject({ status: "pending", tags: [], colors: [] });
    expect(page.items[0].id).toBe(46);
  });

  it("rejects a GIF with 415 and an oversized file with 413", async () => {
    useFreshStore();

    const gif = await fetch(`${BASE}/api/v1/images`, uploadForm("fun.gif", "image/gif"));
    const huge = await fetch(
      `${BASE}/api/v1/images`,
      uploadForm("big.png", "image/png", 11 * 1024 * 1024),
    );

    expect(gif.status).toBe(415);
    expect(((await gif.json()) as ErrorResponse).error.code).toBe("unsupported_type");
    expect(huge.status).toBe(413);
  });

  it("422s a request with no file", async () => {
    useFreshStore();

    const response = await fetch(`${BASE}/api/v1/images`, { method: "POST", body: new FormData() });

    expect(response.status).toBe(422);
  });
});

describe("MockStore analysis timeline", () => {
  const T0 = 1_000_000;

  it("goes pending, then processing, then done after 3 seconds", () => {
    const mock = new MockStore(T0);
    const { id } = mock.add("ok.jpg", T0);
    const statusAt = (offsetMs: number) => mock.get(id, T0 + offsetMs)?.analysis.status;

    expect(statusAt(0)).toBe("pending");
    expect(statusAt(PROCESSING_AFTER_MS - 1)).toBe("pending");
    expect(statusAt(PROCESSING_AFTER_MS)).toBe("processing");
    expect(statusAt(ANALYSIS_DONE_AFTER_MS - 1)).toBe("processing");
    expect(statusAt(ANALYSIS_DONE_AFTER_MS)).toBe("done");
    expect(mock.get(id, T0 + ANALYSIS_DONE_AFTER_MS)?.analysis.tags.length).toBeGreaterThan(0);
  });

  it("ends as failed for a file named like 'fail', and a retry then succeeds", () => {
    const mock = new MockStore(T0);
    const { id } = mock.add("will-fail.jpg", T0);

    const failed = mock.get(id, T0 + ANALYSIS_DONE_AFTER_MS)?.analysis;
    expect(failed).toMatchObject({ status: "failed", tags: [] });
    expect(failed?.error).toBeTruthy();
    expect(failed?.colors).toHaveLength(3);

    mock.reanalyze(id, T0 + 10_000);
    expect(mock.get(id, T0 + 10_000)?.analysis.status).toBe("pending");
    expect(mock.get(id, T0 + 10_000 + ANALYSIS_DONE_AFTER_MS)?.analysis.status).toBe("done");
  });

  it("is the same gallery every time", () => {
    const first = new MockStore(T0).list({ kind: "all" }, T0);
    const second = new MockStore(T0).list({ kind: "all" }, T0);

    expect(second).toEqual(first);
  });
});
