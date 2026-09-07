# indico-rag — design

Date: 2026-09-07

## Purpose

A terminal tool that mirrors the timetable of an Indico event to local disk: browse
the agenda in a TUI, select the talks you want by day, session, track or free-text
search, and download exactly one slide file per talk into a folder tree sorted by
day and session. Non-PDF slides are converted to PDF so the result is a uniform
corpus. A manifest records enough metadata about every talk for the corpus to be
indexed by a retrieval pipeline later.

## Scope

In scope: scanning an event, caching scans, interactive selection, single-file
selection per talk, parallel download, conversion to PDF, optional text extraction,
a metadata manifest.

Out of scope: embedding, chunking, vector storage, or any retrieval logic; uploading
anything back to Indico; downloading videos or session recordings; following
arbitrary external link attachments other than Google Slides.

## Access regimes

Indico instances differ in what stands between the client and the JSON. The client
must detect the regime per host rather than assume one, and must report which regime
it detected.

- **Open.** The export API answers directly. No preparation needed.
- **Proof of work (Anubis).** Verified present on `agenda.infn.it`, version v1.25.0.
  Every path tested — the timetable page, `/export/timetable/<id>.json`, and
  `/export/event/<id>.json` — returns **HTTP 200** carrying an interstitial HTML page
  rather than the requested content, with both a default `curl` User-Agent and a
  Chrome User-Agent. Because the status is 200, detection keys on the response body
  signature, not on the status code.
- **Authenticated.** The instance requires a logged-in user, either for the whole
  site or for this event's material. Handled with an Indico personal API token sent
  as `Authorization: Bearer <token>`; tokens are created by the user at
  `https://<host>/user/tokens/`.

### Access strategy layer

`AccessStrategy` is a protocol with two operations: `prepare(client)`, run once per
host before the first request, and `inspect(response)`, which classifies a response
as satisfied, retry-after-preparation, or fatal.

Detection runs once per host: issue the request, then classify.

| Observation | Regime | Action |
|---|---|---|
| Expected JSON content type and body | Open | Proceed |
| 200 with Anubis interstitial signature | Proof of work | Solve, store cookie, replay |
| 401, 403, or a redirect to a login page | Authenticated | Prompt for an API token, replay |

The outcome is cached in `hosts/<host>.json` in the state directory, together with
the Anubis cookie or a reference to the stored token, so later runs skip detection.
Strategies compose: an instance may be both Anubis-protected and require a token.

Credentials are written to a file with `0600` permissions, one entry per host, and
are never written into a scan cache or a manifest.

### Anubis solver

Parse the challenge from the interstitial, search for a nonce whose
`sha256(challenge + nonce)` has the required number of leading zero bits, and post the
result to the Anubis pass-challenge endpoint to obtain the auth cookie. The search
runs in a worker thread so it does not block the event loop, and has a hard attempt
ceiling: if the difficulty exceeds what the ceiling allows, the run aborts with a
message naming the difficulty rather than spinning.

Concurrent requests share one cookie jar. When a mid-run response comes back as a
fresh challenge, a single coroutine re-solves while holding a lock and the others
wait on it, then all retry.

## Data model

The parser normalizes Indico's JSON into a model that does not mirror the API shape,
so an API change is confined to the parser.

- `Event` — id, host, title, slug, date range, `days`
- `Day` — date, `sessions`
- `Session` — id, title, slug, `talks`. Contributions scheduled outside any session
  are placed in a synthetic session so the tree and the output layout always have
  this level.
- `Talk` — id, title, abstract, speakers with affiliations, track, contribution type,
  start and end time, Indico contribution URL, `attachments`, `chosen`
- `Attachment` — id, filename, kind (`file` or `link`), url, size, content type,
  upload time, folder title

Breaks and other non-contribution timetable entries are dropped at parse time.

Two endpoints are relevant: `/export/timetable/<id>.json` for the day, session and
contribution structure, and `/export/event/<id>.json?detail=contributions` for
attachment metadata, joined on contribution id. Whether the timetable export already
carries attachments inline varies by Indico version; the parser probes for them and
issues the second request only when they are absent.

### Choosing one file per talk

`chosen` is computed at parse time, so the TUI can display the resulting filename on
every row and an override is a single field reassignment. Ranking, in order:

1. Format: PDF, then `pptx`/`ppt`/`odp`, then `key`, then a Google Slides link.
2. Newest revision or upload time.
3. Folder or title matching slides, talk, or presentation, ahead of poster, backup,
   or paper.

A talk with no usable attachment has `chosen = None` and is shown as having no
material. The downloader receives a flat list of `(talk, attachment, destination)`
and knows nothing about ranking.

## Persistence

Under `platformdirs.user_state_dir("indico-rag")`:

- `scans/<host>_<event-id>.json` — the normalized model, `fetched_at`, the event
  title, the last selection, and the last output directory.
- `scans/index.json` — backs the home screen: title, host, id, date range, scan time,
  talk count.
- `hosts/<host>.json` — detected access regime and its cached cookie.
- `credentials.json` — API tokens, mode `0600`.

`platformdirs.user_config_dir("indico-rag")/config.toml` holds `output_root`
(default `~/Desktop/indico-talks`), `download_concurrency` (default 8),
`convert_concurrency` (default 2), format preferences, and the extraction flag. Every
value is overridable per run from the TUI.

Concurrency above 16 emits a warning: the limit is politeness toward the host, not
available bandwidth.

## TUI

Built with Textual. Four screens.

**Home.** A table of past scans plus a `New event…` row. `Enter` opens a scan and
restores its selection and output directory, `n` starts a new scan, `r` re-scans in
place, `d` forgets a scan.

**New event.** One input accepting either a full URL such as
`https://agenda.infn.it/event/50058/timetable/#20260831` or a bare event id, from
which host and id are parsed. Scan progress is shown by stage — detecting access,
solving proof of work, fetching timetable, fetching attachments, parsed *N* talks —
because the proof-of-work step takes seconds and silence there reads as a hang.

**Selection.** A `Day → Session → Talk` tree with tri-state checkboxes; a partially
selected parent renders as partial. Each talk row shows start time, title, speaker,
and the chosen file with its disposition:

```
09:30  Latest LEGEND Results — L. Chen         [slides.pdf]
11:15  Background Modeling — A. Müller         [deck.pptx → pdf]
14:00  Discussion — panel                      [no material]
```

Keys: `space` toggle, `a` select all shown, `N` clear, `i` invert, `/` focus filter,
`o` override the chosen attachment through a modal listing all attachments plus a
skip option, `d` change output directory, `D` start.

Filtering matches title, speaker, and session. The default is a case-insensitive
substring; `Ctrl+R` switches the box to regular-expression mode, and the active mode
is displayed in the box. An invalid pattern shows an inline error and matches
nothing — it does not fall back to substring matching, which would silently present
the wrong talk set. Structured filters cover day, session, Indico track, Indico
contribution type, and material state (`has PDF`, `needs conversion`, `no material`).

Filtering changes only what is displayed, never what is selected. Selecting all while
a filter is active adds the visible talks to the selection; clearing the filter
leaves that selection intact. This invariant is tested directly.

**Progress.** One row per selected talk showing its stage — queued, downloading,
converting, extracting, done, failed — with overall counts and a closing summary
listing every failure and its reason.

## Pipeline

One coroutine per selected talk, gated by two semaphores, `download_concurrency` and
`convert_concurrency`:

```
fetch → stream to <name>.part → atomic rename
      → if not PDF: convert
      → if extraction enabled: extract text → sidecar
      → record manifest entry
```

A talk that arrives as a PDF never touches the conversion gate. Total wall time
approaches the larger of the download and conversion totals rather than their sum.

### Conversion

`soffice --headless --convert-to pdf` handles `pptx`, `ppt`, and `odp`. Each
concurrent instance requires its own profile via
`-env:UserInstallation=file:///…/lo-profile-<n>`; instances sharing a profile attach
to the first process and return without converting. Every conversion runs under a
hard timeout followed by a kill, because LibreOffice hangs indefinitely on some
malformed files.

`key` files are converted by driving Keynote through `osascript`. Google Slides links
are fetched from the presentation's PDF export URL and need no local renderer.

LibreOffice is not currently installed on the target machine; `brew install --cask
libreoffice` is a setup prerequisite. Keynote and Microsoft PowerPoint are present.

Pandoc cannot serve as a converter: pandoc 3.3 has no `pptx`, `ppt`, `odp`, or `key`
input format. It writes `pptx` but does not read it, because its intermediate
representation is a document AST with no faithful encoding of slide layout.

### Text extraction

Opt-in. Uses `pypdfium2`, which is permissively licensed; PyMuPDF is excluded because
its AGPL licence would propagate to this repository. Output is one text sidecar per
talk. Slides that are images yield little or no text; this is recorded rather than
treated as an error.

## Output layout

```
<output_root>/
└── <event-id>_<event-slug>/
    ├── <YYYY-MM-DD>/
    │   └── <Session-Name>/
    │       └── <HHMM>_<Speaker>_<Title>.pdf
    └── _manifest.json
```

`<Speaker>` is the surname of the first listed speaker. A talk with several speakers
uses the first only; a talk with none omits the component, giving
`<HHMM>_<Title>.pdf`.

Path components are sanitized for the filesystem: unicode preserved where legal,
path separators replaced, length capped, and collisions between two talks sharing a
time and speaker resolved with a numeric suffix.

## Manifest

`_manifest.json` is both the resume record and the retrieval-indexing record. Per
talk: Indico contribution id and URL, title, abstract, speakers with affiliations,
session, track, contribution type, start and end time, original attachment filename
and URL, local path, checksum, converted flag with the reason when false, extracted
text sidecar path, and the outcome.

Resume reads the manifest before starting and skips talks whose recorded checksum
matches a file already on disk, retrying only failures.

## Failure handling

Failures are isolated per talk; the run continues. Retries with exponential backoff
apply to timeouts and 5xx. A 4xx fails immediately, since retrying a rejection wastes
time. A failed or timed-out conversion keeps the original file and records
`converted: false` with the reason; a zero-page PDF is never emitted.

No blanket exception suppression anywhere. Every failure appears in both the manifest
and the closing summary.

The whole run aborts only when the proof of work is unsolvable within the ceiling,
the event does not exist, credentials are required but absent or rejected, or the
output directory is not writable.

## Testing

Pure logic, tested without network or TUI:

- Challenge parsing, against the captured interstitials in `probes/captures/`.
- Proof-of-work solving, against a challenge with a known-good nonce.
- Access-regime detection, one case per regime including the 200-with-interstitial
  case that a status-code check would misread.
- The ranking rule, table-driven.
- Filtering, including regex mode, invalid patterns, and the invariant that filtering
  does not mutate selection.
- Path sanitization: unicode, separators, overlong names, and collisions.

Integration:

- The pipeline against a local server serving fixture files with an injected fake
  converter, asserting that concurrency limits hold, that resume skips completed
  work, and that a single failure does not abort the run.
- Textual's `run_test()` pilot for key bindings and tri-state propagation.
- One LibreOffice conversion test marked slow, skipped when `soffice` is absent.

## Repository layout

```
indico-rag/
├── src/indico_rag/
│   ├── access/        strategies: open, anubis, token
│   ├── indico/        endpoints, parser, ranking
│   ├── model.py
│   ├── store.py       scans, config, credentials
│   ├── pipeline/      download, convert, extract, manifest
│   ├── tui/           screens, widgets, filtering
│   └── cli.py
├── tests/
├── docs/superpowers/specs/
└── pyproject.toml
```

Managed with `uv`, permissively licensed. The repository is initialized and
developed locally; the GitHub remote is created and pushed only once the tool is
verified working end to end.

## Verification required before implementation depends on it

The Anubis solver is unproven against `agenda.infn.it`. A throwaway probe against
event 50058 is the first implementation step: solve the challenge, retrieve the
timetable JSON, and confirm the attachment shape. The parser design and the
single-versus-two-request question above both depend on its result.
