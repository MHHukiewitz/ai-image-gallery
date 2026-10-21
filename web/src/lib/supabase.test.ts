import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { subscribeToAnalysisUpdates as SubscribeToAnalysisUpdates } from "./supabase";

/** A Realtime channel that keeps what the code under test registered, so a test can play the server. */
const fake = vi.hoisted(() => {
  type PostgresPayload = { new: Record<string, unknown> };

  class FakeChannel {
    filter: Record<string, unknown> | undefined;
    onChange: ((payload: PostgresPayload) => void) | undefined;
    onStatus: ((status: string) => void) | undefined;

    constructor(readonly topic: string) {}

    on(
      type: string,
      filter: Record<string, unknown>,
      onChange: (payload: PostgresPayload) => void,
    ): this {
      if (type !== "postgres_changes") throw new Error(`unexpected listener type ${type}`);
      this.filter = filter;
      this.onChange = onChange;
      return this;
    }

    subscribe(onStatus: (status: string) => void): this {
      this.onStatus = onStatus;
      return this;
    }
  }

  return {
    FakeChannel,
    channels: [] as FakeChannel[],
    removeChannel: vi.fn(async () => "ok"),
  };
});

vi.mock("@supabase/supabase-js", () => ({
  createClient: () => ({
    channel: (topic: string) => {
      const channel = new fake.FakeChannel(topic);
      fake.channels.push(channel);
      return channel;
    },
    removeChannel: fake.removeChannel,
  }),
}));

let subscribe: typeof SubscribeToAnalysisUpdates;

beforeEach(async () => {
  vi.stubEnv("VITE_SUPABASE_URL", "http://127.0.0.1:54321");
  vi.stubEnv("VITE_SUPABASE_PUBLISHABLE_KEY", "test-key");
  // supabase.ts reads the env and counts topics when it loads, so every test gets a fresh copy.
  vi.resetModules();
  ({ subscribeToAnalysisUpdates: subscribe } = await import("./supabase"));
});

afterEach(() => {
  vi.unstubAllEnvs();
  fake.channels.length = 0;
  fake.removeChannel.mockClear();
});

const USER_ID = "00000000-0000-4000-8000-0000000087a1";

function subscribeWithSpies(userId = USER_ID) {
  const onUpdate = vi.fn();
  const onLiveChange = vi.fn();
  const unsubscribe = subscribe(userId, onUpdate, onLiveChange);
  const channel = fake.channels.at(-1);
  if (!channel) throw new Error("no channel was created");
  return { channel, onUpdate, onLiveChange, unsubscribe };
}

describe("subscribeToAnalysisUpdates", () => {
  it("listens to UPDATEs of the user's own image_metadata rows", () => {
    const { channel } = subscribeWithSpies();

    expect(channel.filter).toEqual({
      event: "UPDATE",
      schema: "public",
      table: "image_metadata",
      filter: `user_id=eq.${USER_ID}`,
    });
    expect(channel.topic).toContain(USER_ID);
  });

  it("gives each subscription its own topic, so one can start while the last is closing", () => {
    subscribeWithSpies();
    subscribeWithSpies();

    const [first, second] = fake.channels;
    expect(first.topic).not.toBe(second.topic);
  });

  it("calls back with the image id and the new status", () => {
    const { channel, onUpdate } = subscribeWithSpies();

    channel.onChange?.({ new: { id: 3, image_id: 41, ai_processing_status: "done", tags: [] } });
    channel.onChange?.({ new: { id: 4, image_id: 42, ai_processing_status: "failed" } });

    expect(onUpdate.mock.calls).toEqual([
      [{ imageId: 41, status: "done" }],
      [{ imageId: 42, status: "failed" }],
    ]);
  });

  it("ignores a row that has no usable image id or status", () => {
    const { channel, onUpdate } = subscribeWithSpies();

    channel.onChange?.({ new: {} });
    channel.onChange?.({ new: { image_id: "41", ai_processing_status: "done" } });
    channel.onChange?.({ new: { image_id: 41, ai_processing_status: "archived" } });
    channel.onChange?.({ new: { image_id: 41 } });

    expect(onUpdate).not.toHaveBeenCalled();
  });

  it("reports live once joined, and not live whenever the channel drops", () => {
    const { channel, onLiveChange } = subscribeWithSpies();

    for (const status of ["SUBSCRIBED", "CHANNEL_ERROR", "SUBSCRIBED", "TIMED_OUT", "CLOSED"]) {
      channel.onStatus?.(status);
    }

    expect(onLiveChange.mock.calls.map(([live]) => live)).toEqual([
      true,
      false,
      true,
      false,
      false,
    ]);
  });

  it("removes the channel when unsubscribed, and stays quiet afterwards", () => {
    const { channel, onUpdate, onLiveChange, unsubscribe } = subscribeWithSpies();

    unsubscribe();
    // Closing a channel reports CLOSED, and an event can already be on its way.
    channel.onStatus?.("CLOSED");
    channel.onChange?.({ new: { image_id: 41, ai_processing_status: "done" } });

    expect(fake.removeChannel).toHaveBeenCalledExactlyOnceWith(channel);
    expect(onLiveChange).not.toHaveBeenCalled();
    expect(onUpdate).not.toHaveBeenCalled();
  });
});
