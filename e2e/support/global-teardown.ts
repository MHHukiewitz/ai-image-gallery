import { accountPrefix } from "./accounts";

interface AdminUser {
  id: string;
  email?: string;
}

interface ImageRow {
  original_path: string;
  thumbnail_path: string;
}

/** Calls the local Supabase API with the secret key, which bypasses RLS and may delete users. */
async function supabaseFetch(baseUrl: string, secretKey: string, path: string, init?: RequestInit) {
  const response = await fetch(new URL(path, baseUrl), {
    ...init,
    headers: { apikey: secretKey, "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    throw new Error(`${init?.method ?? "GET"} ${path} answered ${response.status}`);
  }
  return response;
}

/** Removes the files first: deleting the user cascades to the rows but never to Storage. */
async function deleteUser(baseUrl: string, secretKey: string, user: AdminUser): Promise<void> {
  const rows = (await (
    await supabaseFetch(
      baseUrl,
      secretKey,
      `/rest/v1/images?select=original_path,thumbnail_path&user_id=eq.${user.id}`,
    )
  ).json()) as ImageRow[];
  const prefixes = rows.flatMap((row) => [row.original_path, row.thumbnail_path]);
  if (prefixes.length > 0) {
    await supabaseFetch(baseUrl, secretKey, "/storage/v1/object/images", {
      method: "DELETE",
      body: JSON.stringify({ prefixes }),
    });
  }
  await supabaseFetch(baseUrl, secretKey, `/auth/v1/admin/users/${user.id}`, { method: "DELETE" });
}

/**
 * Deletes the accounts this run created, with their images. It needs SUPABASE_URL and
 * SUPABASE_SECRET_KEY (both in the repo-root .env). Without them the users stay, which is fine
 * on a throwaway CI stack and only untidy on a developer's. A failure here is reported but never
 * fails the run: the tests already passed or failed on their own.
 */
export default async function globalTeardown(): Promise<void> {
  const baseUrl = process.env.SUPABASE_URL;
  const secretKey = process.env.SUPABASE_SECRET_KEY;
  const runId = process.env.E2E_RUN_ID;
  if (!baseUrl || !secretKey || !runId) {
    console.log("e2e: SUPABASE_URL or SUPABASE_SECRET_KEY is not set, so the test users stay.");
    return;
  }

  try {
    const listing = await supabaseFetch(baseUrl, secretKey, "/auth/v1/admin/users?per_page=1000");
    const { users } = (await listing.json()) as { users: AdminUser[] };
    const prefix = accountPrefix(runId);
    const mine = users.filter((user) => user.email?.startsWith(prefix));
    for (const user of mine) await deleteUser(baseUrl, secretKey, user);
    console.log(`e2e: deleted ${mine.length} test user(s) from run ${runId}.`);
  } catch (error) {
    console.warn("e2e: could not delete the test users:", error);
  }
}
