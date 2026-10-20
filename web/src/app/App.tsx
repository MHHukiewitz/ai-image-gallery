import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router/dom";

import { Toaster } from "../components/ui/Toaster";
import { AppErrorBoundary } from "../lib/observability";
import { createQueryClient } from "./queryClient";
import { createAppRouter } from "./router";

const queryClient = createQueryClient();
const router = createAppRouter();

export function App() {
  return (
    <AppErrorBoundary>
      <QueryClientProvider client={queryClient}>
        {/* The one place that sets the page's colors; dark mode follows the OS. */}
        <div className="min-h-dvh bg-zinc-50 text-zinc-900 antialiased dark:bg-zinc-950 dark:text-zinc-100">
          <RouterProvider router={router} />
        </div>
        <Toaster />
      </QueryClientProvider>
    </AppErrorBoundary>
  );
}
