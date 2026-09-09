# Working on indico-rag

Notes for anyone — person or agent — picking this up. They record what the code
cannot: what the servers actually do, which assumptions turned out to be wrong, and
where the tests will let you down.

## The shape of it

```
access/     is this host open, behind a proof of work, or asking for a token?
client.py   one HTTP client that adapts to whichever answer it got
indico/     Indico's JSON in, the agenda model out; and which file a talk contributes
pipeline/   fetch → convert → extract → record, one coroutine per talk
tui/        four screens over that: scans, new event, selection, progress
store.py    config, cached scans, credentials
```

`indico/parser.py` is the only place that knows Indico's wire format. If Indico
changes, it changes there and nowhere else — keep it that way.

## Verify against a real server, not only against mocks

Every serious bug in this project's history passed the whole test suite first. Not
some of them; every one.

- The two Indico exports identify a contribution by **different ids**. Joining on
  the wrong one matched 0 of 100 talks while every test stayed green, because the
  fixture assertion only checked that *some* talk had attachments.
- Regime detection called every Indico instance "needs a token", because every
  Indico homepage contains a login link. `fetch` was broken against every host in
  the world; 180 tests passed.
- The first request probed the host root, which is not challenged, so no
  proof-of-work cookie was ever obtained and parallel downloads each drew their own
  challenge and cancelled each other out. Zero files downloaded, suite green.
- Sessions were stored in a dict keyed by session id, so a session with a morning
  and an afternoon block lost one of them. Half an agenda vanished with no error.
- A link attachment carries both its target and an Indico redirect. Reading the
  redirect made every Google Slides deck invisible.

The pattern: a mock returns what its author expected the server to return, and the
test then agrees with the author. Before believing a network-facing change, run it
against a real instance. `probes/` outside the repository is where throwaway checks
of this kind belong.

Two servers worth knowing:

- `agenda.infn.it` — public, behind Anubis. Event `50058` is the fixture source.
- `indico.legend-exp.org` — needs a token, and answers without one by returning a
  timetable whose days are empty rather than refusing.

## Anubis

A challenged request comes back **200** with an interstitial, so classification
reads the body, never the status. The challenge object holds `randomData` (what gets
hashed) and an `id` that must be echoed back — omitting it returns 500. `difficulty`
counts leading zero **hex characters**.

Anubis tracks **one** outstanding challenge per client, named in a cookie. Two
challenges drawn in parallel overwrite each other and the one being solved stops
matching, which re-challenges everything. So: one request establishes the cookie
before the rest start, and a gate closes while an answer is being computed. Do not
"simplify" that away — it is the difference between 0 and 18 of 18 downloads.

## Indico

- `/export/` is the classic API and needs a token with the **Classic API (read
  only)** scope. A token without it authenticates but reads nothing, and the refusal
  arrives as a `400` carrying `insufficient_scope` — not a 401 or 403.
- A rejected token is dropped for the run rather than allowed to fail a download
  that would have worked anonymously. Public material is the common case.
- `track` and `type` are frequently null. That is data, not a parsing failure.
- Attachment lists arrive as `null` where a list is expected. Guard for it.

## House style

- Fail loudly. No blanket `except Exception: pass`. The one broad catch in
  `pipeline/runner.py` is deliberate: it isolates a single talk's failure and records
  it in both the manifest and the summary. It is `Exception`, not `BaseException`, so
  cancellation still propagates.
- Comments and commit messages state what is true now. No task numbers, no plan
  references, nothing that needs a chat log to make sense of.
- Dependencies are `httpx`, `textual`, `platformdirs`, `pypdfium2`, and nothing else.
  `pypdfium2` rather than PyMuPDF, whose AGPL licence would take the project with it.

## Textual

Pinned to 8.x, and most guidance online targets 0.x. Things that have already cost
time:

- `Widget.visible` is reserved; do not use it as your own attribute.
- `Screen.AUTO_FOCUS = None` does not disable auto-focus, it falls back to the app
  default. Use `""` — and then focus something explicitly, or nothing has a cursor
  and every key that acts on "the current row" silently does nothing.
- `Tree` and `DataTable` render cell content as Rich markup, so a literal `[` is
  eaten. Wrap in `rich.text.Text`. Real talk titles contain brackets.
- `Tree` binds `space` itself, and a focused widget's binding beats the screen's.
- `DataTable.add_columns` returns the keys cells are addressed by. The header
  strings are labels and will not work.
- A worker that raises leaves a blank screen and no message, which reads as a hang.
  Catch, show it, re-raise.

Screen tests that only exercise keys needing no cursor will pass while the primary
interaction is dead. Two separate bugs got through exactly that way.

## Running things

```bash
uv run pytest -q                 # the suite
uv run pytest -m slow            # real LibreOffice conversions
uv run indico-rag                # the interface
```

If pytest cannot import the package, iCloud has probably hidden the editable
install's `.pth` file: `chflags nohidden .venv/lib/python*/site-packages/*.pth`. The
virtualenv is excluded from sync with an extended attribute; a fresh clone elsewhere
may need it again.
