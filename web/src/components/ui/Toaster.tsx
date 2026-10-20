import { Toaster as SonnerToaster } from "sonner";

/**
 * Follows the OS light/dark setting, like the rest of the UI. Every toast gets a × so it can be
 * dismissed without waiting out its timer. Sonner already drops its motion under reduced motion.
 */
export function Toaster() {
  return <SonnerToaster theme="system" position="bottom-right" closeButton />;
}
