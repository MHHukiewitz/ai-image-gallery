/**
 * Where to send someone once they're signed in: back to the page RequireAuth stopped them on,
 * or the gallery. Only paths inside this app count, so a crafted link can't redirect off-site.
 */
export function postSignInPath(locationState: unknown): string {
  const from =
    typeof locationState === "object" && locationState !== null && "from" in locationState
      ? locationState.from
      : undefined;
  return typeof from === "string" && from.startsWith("/") && !from.startsWith("//") ? from : "/";
}
