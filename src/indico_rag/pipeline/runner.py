"""One coroutine per talk: fetch, convert, extract, record.

Two semaphores gate the work. A talk arriving as a PDF never reaches the
conversion gate, so total time approaches the larger of the download and
conversion totals rather than their sum.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx

from indico_rag.client import IndicoClient
from indico_rag.indico.ranking import google_slides_pdf_url, is_google_slides
from indico_rag.model import Attachment, Day, Event, Session, Talk
from indico_rag.pipeline.convert import ConversionError, convert_to_pdf, needs_conversion
from indico_rag.pipeline.extract import write_sidecar
from indico_rag.pipeline.manifest import MANIFEST_NAME, Manifest, TalkRecord, sha256_of
from indico_rag.pipeline.paths import deduplicate, destination, event_dir
from indico_rag.store import Config

Stage = Literal["queued", "downloading", "converting", "extracting", "done", "failed", "skipped"]
Update = Callable[[str, Stage, str], None]


@dataclass
class Job:
    talk: Talk
    session: Session
    day: Day
    attachment: Attachment
    dest: Path


@dataclass
class Summary:
    done: int = 0
    failed: int = 0
    no_material: int = 0
    skipped: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)


def build_jobs(event: Event, selected: set[str], root: Path) -> tuple[list[Job], list[Talk]]:
    jobs: list[Job] = []
    without: list[Talk] = []
    taken: set[Path] = set()

    for day in event.days:
        for session in day.sessions:
            for talk in session.talks:
                if talk.contribution_id not in selected:
                    continue
                if talk.chosen is None:
                    without.append(talk)
                    continue
                suffix = "pdf" if talk.chosen.kind == "link" else (talk.chosen.suffix or "bin")
                path = deduplicate(
                    destination(root, event, day, session, talk, suffix=suffix), taken
                )
                taken.add(path)
                jobs.append(Job(talk=talk, session=session, day=day,
                                attachment=talk.chosen, dest=path))
    return jobs, without


def _record(job: Job, *, status: str, local_path: Path | None = None, sha: str | None = None,
            size: int | None = None, converted: bool = False,
            conversion_error: str | None = None, text_path: str | None = None,
            text_chars: int | None = None, error: str | None = None) -> TalkRecord:
    talk = job.talk
    return TalkRecord(
        contribution_id=talk.contribution_id, title=talk.title, abstract=talk.abstract,
        speakers=list(talk.speakers), affiliations=list(talk.affiliations),
        session=job.session.title, track=talk.track,
        contribution_type=talk.contribution_type,
        start=talk.start.isoformat() if talk.start else None,
        end=talk.end.isoformat() if talk.end else None,
        indico_url=talk.url, source_url=job.attachment.url,
        source_filename=job.attachment.filename,
        local_path=str(local_path) if local_path else None, sha256=sha, bytes=size,
        converted=converted, conversion_error=conversion_error,
        text_path=text_path, text_chars=text_chars, status=status, error=error,
    )


async def _fetch(job: Job, client: IndicoClient, external: httpx.AsyncClient) -> None:
    job.dest.parent.mkdir(parents=True, exist_ok=True)
    partial = job.dest.with_suffix(job.dest.suffix + ".part")

    if job.attachment.kind == "link":
        if not is_google_slides(job.attachment.url):
            raise ValueError(f"unsupported link attachment: {job.attachment.url}")
        url = google_slides_pdf_url(job.attachment.url)
        async with external.stream("GET", url) as response:
            response.raise_for_status()
            with partial.open("wb") as handle:
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    handle.write(chunk)
    else:
        await client.stream_to(job.attachment.url, partial)

    partial.replace(job.dest)


async def _process(job: Job, *, client: IndicoClient, external: httpx.AsyncClient,
                   manifest: Manifest, summary: Summary, config: Config,
                   download_sem: asyncio.Semaphore, convert_sem: asyncio.Semaphore,
                   profiles: asyncio.Queue[int], on_update: Update) -> None:
    talk_id = job.talk.contribution_id

    if manifest.is_complete(talk_id):
        summary.skipped += 1
        on_update(talk_id, "skipped", "already downloaded")
        return

    try:
        try:
            on_update(talk_id, "downloading", job.attachment.filename)
            async with download_sem:
                await _fetch(job, client, external)
        except Exception as exc:  # noqa: BLE001 - recorded, never suppressed
            summary.failed += 1
            summary.failures.append((job.talk.title, f"download failed: {exc}"))
            manifest.add(_record(job, status="failed", error=f"download failed: {exc}"))
            on_update(talk_id, "failed", str(exc))
            return

        final = job.dest
        converted = False
        conversion_error: str | None = None

        if needs_conversion(job.dest.suffix.lstrip(".")):
            on_update(talk_id, "converting", job.dest.name)
            profile = await profiles.get()
            try:
                async with convert_sem:
                    final = await convert_to_pdf(job.dest, profile_index=profile)
                converted = True
                job.dest.unlink(missing_ok=True)
            except (ConversionError, OSError) as exc:
                conversion_error = str(exc)
            finally:
                profiles.put_nowait(profile)

            if conversion_error is not None:
                summary.failed += 1
                summary.failures.append((job.talk.title, f"conversion failed: {conversion_error}"))
                manifest.add(_record(
                    job, status="failed", local_path=job.dest, sha=sha256_of(job.dest),
                    size=job.dest.stat().st_size, converted=False,
                    conversion_error=conversion_error,
                    error=f"conversion failed: {conversion_error}",
                ))
                on_update(talk_id, "failed", conversion_error)
                return

        text_path: str | None = None
        text_chars: int | None = None
        if config.extract_text:
            on_update(talk_id, "extracting", final.name)
            try:
                sidecar, text_chars = write_sidecar(final)
                text_path = str(sidecar)
            except Exception as exc:  # noqa: BLE001 - recorded, never suppressed
                conversion_error = f"text extraction failed: {exc}"

        manifest.add(_record(
            job, status="done", local_path=final, sha=sha256_of(final),
            size=final.stat().st_size, converted=converted, conversion_error=conversion_error,
            text_path=text_path, text_chars=text_chars,
        ))
        summary.done += 1
        on_update(talk_id, "done", final.name)
    except Exception as exc:  # noqa: BLE001 - last-resort net for this talk
        # asyncio.TaskGroup cancels every sibling task the moment one raises,
        # so any failure past this point that the stage-specific handlers
        # above did not already catch must still be recorded here rather
        # than propagate and abort every other talk in the run. Being an
        # `Exception` and not a `BaseException` catch, it never intercepts
        # `asyncio.CancelledError` or `KeyboardInterrupt`, so a genuine
        # cancellation still stops the run.
        summary.failed += 1
        summary.failures.append((job.talk.title, f"unexpected error: {exc}"))
        manifest.add(_record(job, status="failed", error=f"unexpected error: {exc}"))
        on_update(talk_id, "failed", str(exc))


async def run_jobs(event: Event, jobs: list[Job], no_material: list[Talk], *, config: Config,
                   root: Path, token: str | None = None,
                   on_update: Update | None = None) -> Summary:
    report: Update = on_update or (lambda *_: None)
    manifest_path = event_dir(root, event) / MANIFEST_NAME
    manifest = Manifest.load(manifest_path)
    summary = Summary()

    for talk in no_material:
        summary.no_material += 1
        manifest.records[talk.contribution_id] = TalkRecord(
            contribution_id=talk.contribution_id, title=talk.title, abstract=talk.abstract,
            speakers=list(talk.speakers), affiliations=list(talk.affiliations),
            session=talk.session_title, track=talk.track,
            contribution_type=talk.contribution_type,
            start=talk.start.isoformat() if talk.start else None,
            end=talk.end.isoformat() if talk.end else None,
            indico_url=talk.url, source_url="", source_filename="", local_path=None,
            sha256=None, bytes=None, converted=False, conversion_error=None,
            text_path=None, text_chars=None, status="no_material", error=None,
        )
        report(talk.contribution_id, "skipped", "no material")

    download_sem = asyncio.Semaphore(config.download_concurrency)
    convert_sem = asyncio.Semaphore(config.convert_concurrency)
    profiles: asyncio.Queue[int] = asyncio.Queue()
    for index in range(max(1, config.convert_concurrency)):
        profiles.put_nowait(index)

    for job in jobs:
        report(job.talk.contribution_id, "queued", job.attachment.filename)

    async with IndicoClient(event.host, token=token) as client, httpx.AsyncClient(
        follow_redirects=True, timeout=120
    ) as external:
        async with asyncio.TaskGroup() as group:
            for job in jobs:
                group.create_task(_process(
                    job, client=client, external=external, manifest=manifest, summary=summary,
                    config=config, download_sem=download_sem, convert_sem=convert_sem,
                    profiles=profiles, on_update=report,
                ))

    manifest.save()
    return summary
