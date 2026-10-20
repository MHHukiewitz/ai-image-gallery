import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Button } from "./Button";
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from "./Dialog";
import { EmptyState } from "./EmptyState";
import { ErrorState } from "./ErrorState";
import { Input } from "./Input";
import { Skeleton } from "./Skeleton";
import { Spinner } from "./Spinner";

describe("Button", () => {
  it("is type=button unless told otherwise, so it never submits a form by accident", () => {
    render(
      <>
        <Button>Plain</Button>
        <Button type="submit">Send</Button>
      </>,
    );

    expect(screen.getByRole("button", { name: "Plain" })).toHaveAttribute("type", "button");
    expect(screen.getByRole("button", { name: "Send" })).toHaveAttribute("type", "submit");
  });

  it("disables itself and shows a spinner while loading, keeping its label", async () => {
    const onClick = vi.fn();
    render(
      <Button loading onClick={onClick}>
        Save
      </Button>,
    );

    const button = screen.getByRole("button", { name: "Save" });
    await userEvent.click(button);

    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button.querySelector("svg")).toBeInTheDocument();
    expect(onClick).not.toHaveBeenCalled();
  });

  it.each(["primary", "secondary", "ghost", "danger"] as const)(
    "renders the %s variant",
    (variant) => {
      render(<Button variant={variant}>Go</Button>);

      expect(screen.getByRole("button", { name: "Go" })).toBeEnabled();
    },
  );
});

describe("Input", () => {
  it("marks itself invalid for assistive tech", () => {
    render(<Input aria-label="Email" invalid />);

    expect(screen.getByLabelText("Email")).toHaveAttribute("aria-invalid", "true");
  });

  it("is not marked invalid by default", () => {
    render(<Input aria-label="Email" />);

    expect(screen.getByLabelText("Email")).not.toHaveAttribute("aria-invalid");
  });
});

describe("Spinner and Skeleton", () => {
  it("announces the spinner unless it is decorative", () => {
    const { rerender } = render(<Spinner label="Searching" />);
    expect(screen.getByRole("status", { name: "Searching" })).toBeInTheDocument();

    rerender(<Spinner decorative />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("hides skeletons from assistive tech", () => {
    const { container } = render(<Skeleton className="h-4" />);

    expect(container.firstElementChild).toHaveAttribute("aria-hidden", "true");
  });
});

describe("EmptyState and ErrorState", () => {
  it("shows the title, description and action", () => {
    render(
      <EmptyState
        title="No matches"
        description="Try another word."
        action={<button>Reset</button>}
      />,
    );

    expect(screen.getByRole("heading", { name: "No matches" })).toBeInTheDocument();
    expect(screen.getByText("Try another word.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reset" })).toBeInTheDocument();
  });

  it("announces the error and retries on click", async () => {
    const onRetry = vi.fn();
    render(<ErrorState message="Couldn't load images." onRetry={onRetry} />);

    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't load images.");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("has no button without a retry handler", () => {
    render(<ErrorState message="Nope." />);

    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("Dialog", () => {
  function Example() {
    return (
      <Dialog>
        <DialogTrigger asChild>
          <button>Open</button>
        </DialogTrigger>
        <DialogContent>
          <DialogTitle>Delete image?</DialogTitle>
          <DialogDescription>This can't be undone.</DialogDescription>
        </DialogContent>
      </Dialog>
    );
  }

  it("opens as a labelled dialog and closes on Escape", async () => {
    render(<Example />);

    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(screen.getByRole("dialog", { name: "Delete image?" })).toHaveAccessibleDescription(
      "This can't be undone.",
    );

    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("closes with the corner button", async () => {
    render(<Example />);
    await userEvent.click(screen.getByRole("button", { name: "Open" }));

    await userEvent.click(screen.getByRole("button", { name: "Close" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
