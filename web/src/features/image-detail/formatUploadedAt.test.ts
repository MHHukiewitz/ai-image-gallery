import { describe, expect, it } from "vitest";

import { formatUploadedAt } from "./formatUploadedAt";

const NOW = new Date("2026-10-20T12:00:00Z");
const SECOND = 1_000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** The upload time, `ms` before `NOW`. */
function ago(ms: number): string {
  return new Date(NOW.getTime() - ms).toISOString();
}

describe("formatUploadedAt", () => {
  it.each([
    ["under a minute", 30 * SECOND, "just now"],
    ["a few minutes", 5 * MINUTE, "5 minutes ago"],
    ["a few hours", 2 * HOUR, "2 hours ago"],
    ["yesterday", DAY + HOUR, "yesterday"],
    ["a few days", 3 * DAY, "3 days ago"],
    ["a few weeks", 2 * 7 * DAY, "2 weeks ago"],
    ["last month", 45 * DAY, "last month"],
    ["last year", 400 * DAY, "last year"],
  ])("describes %s as %j", (_, elapsed, expected) => {
    expect(formatUploadedAt(ago(elapsed), NOW, "en-US").relative).toBe(expected);
  });

  it("formats the absolute time in the given locale", () => {
    const { absolute } = formatUploadedAt(ago(3 * DAY), NOW, "en-US");

    expect(absolute).toBe(
      new Intl.DateTimeFormat("en-US", { dateStyle: "medium", timeStyle: "short" }).format(
        new Date(ago(3 * DAY)),
      ),
    );
    expect(absolute).toContain("2026");
  });

  it("uses the current time when none is given", () => {
    const uploadedAt = new Date(Date.now() - 2 * HOUR).toISOString();

    expect(formatUploadedAt(uploadedAt, undefined, "en-US").relative).toBe("2 hours ago");
  });
});
