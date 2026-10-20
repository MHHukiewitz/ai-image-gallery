import { createBrowserRouter, Navigate } from "react-router";
import type { RouteObject } from "react-router";

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
  { path: "*", element: <Navigate to="/" replace /> },
];

export function createAppRouter() {
  return createBrowserRouter(routes);
}
