const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

/** Largest unit first. Months and years are fixed-length, which is close enough for "3 months ago". */
const UNITS: Array<[Intl.RelativeTimeFormatUnit, number]> = [
  ["year", 365 * DAY_MS],
  ["month", 30 * DAY_MS],
  ["week", 7 * DAY_MS],
  ["day", DAY_MS],
  ["hour", HOUR_MS],
  ["minute", MINUTE_MS],
];

export interface UploadedAt {
  /** For example "20 Oct 2026, 10:00", in the reader's locale and time zone. */
  absolute: string;
  /** For example "3 days ago" or "yesterday". Anything under a minute is "just now". */
  relative: string;
}

/** Formats an upload time both ways. `now` is a parameter so tests can pin the clock. */
export function formatUploadedAt(
  uploadedAt: string,
  now: Date = new Date(),
  locale?: string,
): UploadedAt {
  const date = new Date(uploadedAt);
  const elapsed = date.getTime() - now.getTime();
  const unit = UNITS.find(([, size]) => Math.abs(elapsed) >= size);
  const relativeFormat = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });

  return {
    absolute: new Intl.DateTimeFormat(locale, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(date),
    relative: unit ? relativeFormat.format(Math.trunc(elapsed / unit[1]), unit[0]) : "just now",
  };
}
