import { Toaster as SonnerToaster } from "sonner";

/** Follows the OS light/dark setting, like the rest of the UI. */
export function Toaster() {
  return <SonnerToaster theme="system" position="bottom-right" />;
}
