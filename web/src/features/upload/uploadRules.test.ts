import { describe, expect, it } from "vitest";

import { parseMaxUploadMb, rejectionMessage } from "./uploadRules";

function rejection(name: string, ...codes: string[]) {
  return {
    file: new File(["x"], name),
    errors: codes.map((code) => ({ code, message: `message for ${code}` })),
  };
}

describe("parseMaxUploadMb", () => {
  it("reads a positive number, fractions included", () => {
    expect(parseMaxUploadMb("25")).toBe(25);
    expect(parseMaxUploadMb("0.5")).toBe(0.5);
  });

  it.each([undefined, "", "abc", "0", "-3", "Infinity"])("falls back to 10 for %j", (value) => {
    expect(parseMaxUploadMb(value)).toBe(10);
  });
});

describe("rejectionMessage", () => {
  it("names the file and says a wrong type isn't JPEG or PNG", () => {
    expect(rejectionMessage(rejection("cat.gif", "file-invalid-type"))).toBe(
      "cat.gif isn't a JPEG or PNG image, so it was skipped.",
    );
  });

  it("states the limit for a file that is too large", () => {
    expect(rejectionMessage(rejection("huge.png", "file-too-large"), 10)).toBe(
      "huge.png is larger than the 10 MB limit, so it was skipped.",
    );
  });

  it("reports the type first when a file is both the wrong type and too large", () => {
    expect(
      rejectionMessage(rejection("huge.gif", "file-too-large", "file-invalid-type")),
    ).toContain("isn't a JPEG or PNG");
  });

  it("passes on the library's message for any other problem", () => {
    expect(rejectionMessage(rejection("a.png", "too-many-files"))).toBe(
      "a.png couldn't be added: message for too-many-files.",
    );
  });
});
