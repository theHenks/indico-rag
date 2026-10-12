# indico-rag

Download a conference's talks from [Indico](https://getindico.io) as PDFs, choosing
what you want in a terminal interface.

Point it at an event, pick the talks, and it writes one PDF per talk into a folder
tree sorted by day and session. Slides uploaded as PowerPoint or Keynote are
converted; slides that live in Google Slides are exported. Alongside them it writes
a manifest carrying each talk's title, abstract, speakers, session and source URL,
so the result can be indexed by a retrieval pipeline rather than only read.

## Install

Needs Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```bash
git clone git@github.com:theHenks/indico-rag.git
cd indico-rag
uv sync
```

Converting PowerPoint and OpenDocument slides needs LibreOffice:

```bash
brew install --cask libreoffice
```

Without it, talks whose only material is a `.pptx` are downloaded unconverted and
reported as such. Keynote files are converted by Keynote.app on macOS.

## Use it

```bash
uv run indico-rag
```

The interface opens on the list of past scans. Press `n`, paste an event URL, and
wait for the agenda.

### Choosing talks

The tree shows `day → session → talk`, with the file each talk would produce:

```
09:30  Latest LEGEND Results — L. Chen              [slides.pdf]
11:15  Background Modeling — A. Müller              [deck.pptx → pdf]
14:00  Discussion — panel                           [no material]
```

| key | does |
| --- | --- |
| `space` | select or deselect the row under the cursor |
| `a` | select every talk currently shown |
| `N` | clear the selection |
| `i` | invert the selection |
| `/` | jump to the filter box |
| `ctrl+r` | switch the filter between plain text and regular expressions |
| `o` | cycle which file a talk contributes, or skip it |
| `d` | change the output directory |
| `D` | start downloading |
| `h` | back to the list of scans |
| `ctrl+q` | quit |

### Filtering

The filter matches a talk's title, its speakers and its session name. Since the
session name is also what the talk's folder is named after, filtering by it selects
exactly what lands in that folder.

```
plenary          the plenary talks
!parallel        everything except the parallel sessions
^Neutrino        with ctrl+r: talks whose title or speaker starts with "Neutrino"
```

Filtering changes what is shown, never what is selected, so selections accumulate:
filter, press `a`, clear, filter again, press `a` again.

## From the command line

```bash
indico-rag scan  <event-url>                 print the agenda
indico-rag fetch <event-url> -o <dir> -j 8   download every talk
indico-rag scans                             list cached scans
indico-rag login <host> --token <TOKEN>      store a token for a host
```

`fetch` takes the whole event; use the interface to choose. `--extract` also writes
a text sidecar per talk. Re-running skips whatever is already downloaded and retries
only what failed.

## What lands on disk

```
~/Desktop/indico-talks/
└── 50058_now-2026/
    ├── 2026-09-01/
    │   └── Session-I-plenary-Standard-three-neutrino-oscillations/
    │       ├── 0900_Caccianiga_New-results-of-the-JUNO-experiment.pdf
    │       └── 0930_Russo_T2K-and-SK-oscillation-results.pdf
    └── _manifest.json
```

`_manifest.json` records every talk that was considered, including the ones that
produced nothing. Each entry carries the Indico contribution URL, the original
attachment URL, a checksum, whether conversion was needed, and the outcome. It is
also what makes a re-run skip completed work.

It also records the event itself under `event`: the host, the event id and title,
and the event URL. Each talk's `indico_url` is relative to that host.

## Authentication

Public events need nothing. An instance that requires logging in will ask, once,
whether you want to store a personal API token; a token entered there is kept for
later runs.

Create one at `https://<your-indico>/user/tokens/` and **tick "Classic API (read
only)"**. Without that scope the token authenticates but reads nothing, and Indico
reports it as a bare `400` that looks like a bug rather than a permissions problem.
A token the server rejects for scope is ignored with a warning rather than being
allowed to break a download that would have worked anonymously.

Some instances answer an unauthenticated client with a timetable whose days are
simply empty instead of refusing. That is treated as a refusal, so an event does not
quietly appear to have no talks.

## Bot protection

Some Indico instances sit behind [Anubis](https://github.com/TecharoHQ/anubis),
which asks a client to complete a proof of work before serving anything. This is
handled automatically: the challenge is solved once and the resulting cookie covers
the run. Only one challenge is answered at a time, because Anubis tracks a single
outstanding challenge per client and parallel challenges invalidate one another.

## Limits worth knowing

- One file per talk. Where a talk offers several, the newest PDF wins, then
  PowerPoint or OpenDocument, then Keynote, then a Google Slides link. Press `o` to
  override.
- Google **Slides** are exported; Google Docs and Sheets are not, being notes rather
  than a talk's material. A deck shared only with its author cannot be exported.
- Download concurrency defaults to 8. Higher is impolite to the host rather than
  faster, and the bot protection reacts to it.
- A cached scan is a snapshot. Press `r` on a scan to fetch the agenda again.

## Licence

MIT.
