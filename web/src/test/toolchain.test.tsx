import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

function Counter() {
  const [count, setCount] = useState(0);
  return (
    <button type="button" onClick={() => setCount(count + 1)}>
      Clicked {count} times
    </button>
  );
}

describe("test toolchain", () => {
  it("renders React in jsdom with jest-dom matchers and user-event", async () => {
    render(<Counter />);

    await userEvent.click(screen.getByRole("button"));

    expect(screen.getByRole("button")).toHaveTextContent("Clicked 1 times");
  });

  it("exposes typed public env vars", () => {
    const apiBase: string | undefined = import.meta.env.VITE_API_BASE;

    expect(apiBase === undefined || apiBase.length > 0).toBe(true);
  });
});
