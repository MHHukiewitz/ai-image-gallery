import { describe, expect, it } from "vitest";

import {
  PASSWORD_MIN_LENGTH,
  validateEmail,
  validateNewPassword,
  validatePassword,
} from "./validation";

describe("validateEmail", () => {
  it("asks for an address when the field is empty or blank", () => {
    expect(validateEmail("")).toBe("Enter your email address.");
    expect(validateEmail("   ")).toBe("Enter your email address.");
  });

  it.each(["ada", "ada@", "@example.com", "ada@example", "ada @example.com"])(
    "rejects %s as not an address",
    (email) => {
      expect(validateEmail(email)).toBe("Enter an email address like name@example.com.");
    },
  );

  it.each(["ada@example.com", "  ada@example.com  ", "first.last+tag@sub.example.co.uk"])(
    "accepts %s",
    (email) => {
      expect(validateEmail(email)).toBeUndefined();
    },
  );
});

describe("validatePassword", () => {
  it("only asks for something to be typed, since sign-in doesn't enforce the new-password rule", () => {
    expect(validatePassword("")).toBe("Enter your password.");
    expect(validatePassword("x")).toBeUndefined();
  });
});

describe("validateNewPassword", () => {
  it(`requires at least ${PASSWORD_MIN_LENGTH} characters`, () => {
    expect(validateNewPassword("1234567")).toBe(`Use at least ${PASSWORD_MIN_LENGTH} characters.`);
    expect(validateNewPassword("12345678")).toBeUndefined();
  });
});
