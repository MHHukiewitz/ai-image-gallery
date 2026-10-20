# End-to-end tests

Playwright drives the real app: the SPA behind nginx, the API, the worker and a local Supabase, with the fake analyzer so the analysis is deterministic and needs no AI key. Nothing is mocked.

Two specs, one per project in `playwright.config.ts`:

| Spec | Project | What it does |
|---|---|---|
| `journey.spec.ts` | `chromium` (Desktop Chrome) | One user's whole path, then a second user to prove the data is isolated. |
| `mobile.spec.ts` | `pixel-7` (Pixel 7 emulation) | Sign in, upload, browse and open an image on a phone, with no sideways scroll. It saves the screenshots in `docs/screenshots/`. |

## Run it

You need Docker, Node 22 or newer, and the repo's `.env` (copy `.env.example` and fill in the Supabase values that `make dev-db` prints).

```bash
make dev-db                       # local Supabase; skip if it is already running
docker compose up -d --build      # API, worker and web on http://localhost:8080 (ANALYZER=fake by default)
cd e2e
npm ci
npx playwright install chromium   # once
npx playwright test
```

Useful variations:

```bash
npx playwright test --project=pixel-7   # one project
npx playwright test --ui                # step through it in Playwright's UI
npx playwright show-report              # open the HTML report after a CI-style run
E2E_BASE_URL=http://localhost:5173 npx playwright test   # another address for the SPA
```

Stop the stack with `docker compose down`. Leave Supabase running.

The suite checks `/api/health` first and says so when the stack isn't up, instead of timing out on a blank page.

### Test users

Every test signs up its own accounts with a unique `e2e-<run id>-…@example.test` email and a generated password, so runs never depend on each other. When the run ends, global teardown deletes that run's users and their Storage files through Supabase's admin API. It needs `SUPABASE_URL` and `SUPABASE_SECRET_KEY`, which the suite reads from the repo-root `.env`. Without them the users stay, which only matters on a stack you keep.

## Which requirement is checked where

`J1`…`J8` are the steps of `journey.spec.ts`. `M` is `mobile.spec.ts`.

| Requirement | Where | What is asserted |
|---|---|---|
| Sign up with email and password | J1 | A new account is created and lands on the gallery, which shows the empty state. The account menu shows its email. |
| The gallery needs a session | J1 | Opening `/` signed out goes to `/sign-in`. |
| Sign in | M | A phone signs in with the account it made. |
| Sign out | J8 | The menu's "Sign out" ends on `/sign-in`. |
| Upload several images at once, with progress | J2 | Three files go in together. Each gets a progress bar that reaches 100. |
| Thumbnail grid | J3 | Three tiles appear, and each thumbnail is fetched from Storage and decoded. |
| Analysis runs in the background | J3 | Every tile goes from "Analyzing…" to done within 30 s. |
| Tags | J3, J5 | The first tags show on hover. The modal lists 5 to 10. |
| One-sentence description | J5 | The modal shows it. It is also each finished tile's accessible name. |
| Up to 3 dominant colors | J5 | The modal shows 1 to 3 swatches. |
| Detail view | J5 | Clicking a tile opens a modal with `?image=<id>` in the URL, the file name and the full-size picture. |
| Text search, live | J4 | Typing `red` puts `q=red` in the URL and leaves the 2 red images. A marker set on `window` before the search is still there afterwards, so the page never reloaded. |
| Find similar | J6 | The result includes `red2.png`, as the closest match, and leaves out `red.png` itself. |
| Search by color | J7 | A swatch of `blue.png` gives `color=…` in the URL, and the result is the blue image only. |
| Per-user data (tenant isolation) | J8 | A second user's gallery is empty, their search for `red` finds nothing, and the first user's `?image=<id>` says "Image not found". |
| Works on a phone | M | At 412 px wide, the sign-in page, the empty and filled gallery and the open modal have no sideways scroll, and the modal fits the screen. |

Not covered here: deleting an image and retrying a failed analysis. The web and API unit tests cover them.

## How it avoids flakiness

- **Only web-first assertions.** Everything uses Playwright's auto-retrying `expect`, and `expect.poll` for counts that need a range. There is no `waitForTimeout`.
- **Selectors are roles, labels and accessible names**: `getByRole("searchbox", { name: "Search images" })`, `getByLabel("Email")`. The one exception is the parent of a hovered tag, whose opacity shows the hover.
- **A tile is found by what the analysis says about it.** The fixtures differ in orientation (square, landscape, portrait), and FakeAnalyzer builds its description from the orientation and the dominant hue. A finished tile's accessible name is that description, so waiting for a tile also waits for its analysis. See `support/fixtures.ts`.
- **Up to 30 s for the analysis** (`ANALYSIS_TIMEOUT_MS`). It takes a few seconds locally; the grid polls every 2 s and the worker once a second or so.
- **Pages are awaited by their heading before typing.** After a link click the URL changes a moment before React swaps the page, and `fill` would hit the page being left.
- **No retries**, since they would hide the flakes the suite is meant not to have. One worker keeps the load on the single API and worker steady.

## Notes on the local stack

- `host.docker.internal` is mapped to `127.0.0.1` inside the browser (`--host-resolver-rules` in `playwright.config.ts`). Compose gives the API `http://host.docker.internal:54321` as the Supabase address, and the API puts that address into the signed image URLs. A browser on the host can't resolve it, so without the mapping no picture would load.
- The fixtures in `fixtures/` come from `npm run fixtures` (`scripts/generate-fixtures.mjs`, no dependencies). They are committed, so you only run it to change them. Each is well under 5 KB.

## CI

The `e2e` job in `.github/workflows/ci.yml` starts Supabase with `supabase start`, builds and starts the compose stack, and runs `npx playwright test`. It runs on every push to `main` and on pull requests labeled `e2e`. On a failure it uploads the Playwright report and traces as an artifact, and prints the container logs.
