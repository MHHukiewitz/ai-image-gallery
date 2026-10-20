import { describe, expect, it } from "vitest";

import { postSignInPath } from "./authRedirect";

describe("postSignInPath", () => {
  it("returns the page the visitor was stopped on", () => {
    expect(postSignInPath({ from: "/?image=7" })).toBe("/?image=7");
  });

  it("falls back to the gallery when there is no usable state", () => {
    expect(postSignInPath(null)).toBe("/");
    expect(postSignInPath(undefined)).toBe("/");
    expect(postSignInPath("/images")).toBe("/");
    expect(postSignInPath({})).toBe("/");
  });

  it("never sends the visitor off the app", () => {
    expect(postSignInPath({ from: "//evil.example" })).toBe("/");
    expect(postSignInPath({ from: "https://evil.example" })).toBe("/");
    expect(postSignInPath({ from: 42 })).toBe("/");
  });
});
