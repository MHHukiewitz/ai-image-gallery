import { createBrowserRouter, Navigate } from "react-router";
import type { RouteObject } from "react-router";

import { AdminPage } from "../features/admin/AdminPage";
import { RequireAuth } from "../features/auth/RequireAuth";
import { SignInPage } from "../features/auth/SignInPage";
import { SignUpPage } from "../features/auth/SignUpPage";
import { AppLayout } from "./AppLayout";
import { GalleryRoute } from "./GalleryRoute";

/** Exported separately so tests can mount the same routes in a memory router. */
export const routes: RouteObject[] = [
  { path: "/sign-in", element: <SignInPage /> },
  { path: "/sign-up", element: <SignUpPage /> },
  {
    path: "/",
    element: (
      <RequireAuth>
        <AppLayout>
          <GalleryRoute />
        </AppLayout>
      </RequireAuth>
    ),
  },
  {
    // Linked from the user menu for admins only. Anyone else who opens it gets the server's 403.
    path: "/admin",
    element: (
      <RequireAuth>
        <AppLayout search={false}>
          <AdminPage />
        </AppLayout>
      </RequireAuth>
    ),
  },
  { path: "*", element: <Navigate to="/" replace /> },
];

export function createAppRouter() {
  return createBrowserRouter(routes);
}
