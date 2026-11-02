"""Keep a Google Sheet of leads in step with Harvey (config: sheet).

The sheet is shared between you and Harvey, one row per company:

- **Harvey writes** a row for every first email it sends (status ``sent``,
  ``source`` and ``template_used`` set to ``sheet.label``), and later moves
  that row to ``bounced``, ``replied`` or ``unsubscribed`` when the mailbox
  says so, and notes when a follow-up went out.
- **You write** ``clicked`` in the status column of Harvey's rows for people
  who visited the link. With ``channels.email.follow_ups: clicked`` that
  queues email 2 for them, and only them. You can also mark a row
  ``bounced``, ``replied``, ``unsubscribed`` or ``excluded`` and Harvey
  stops emailing that person.
- **Nobody in the sheet is a new prospect.** Every company domain and
  address anywhere in the sheet, whatever its status or whoever added it, is
  skipped by discovery and the Writer, and a first email to one is
  cancelled at send time. Until the sheet has been read once, first emails
  wait.

Columns are found by their header names (``email``, ``status``,
``domain``, ``website``, ``source``, ...), so their order doesn't matter
and columns Harvey doesn't know are left alone. Harvey only ever changes
cells in its own rows, and only moves a status on from ``sent`` or
``clicked``: a status you set by hand (``excluded``, ...) stays.
"""

import json
import logging
import re
from datetime import datetime, timezone

from harvey.cta import ref_domain

logger = logging.getLogger("harvey.sheet_sync")

# Settings keys.
CLICKED_KEY = "clicked:"              # + prospect id: when the sheet said clicked
SHEET_ROW_KEY = "sheet_row:"          # + prospect id: their row was added
SHEET_FOLLOW_UP_KEY = "sheet_follow_up:"  # + prospect id: follow-up noted
SUPPRESS_KEY = "sheet_suppress"       # JSON: domains and addresses in the sheet
SHEET_PROBLEM_KEY = "sheet_problem"   # why the last sync failed, for status

# A prospect skipped because their company or address is in the sheet.
IN_SHEET_STATUS = "in_sheet"

SENT, CLICKED = "sent", "clicked"
BOUNCED, REPLIED, UNSUBSCRIBED, EXCLUDED = "bounced", "replied", "unsubscribed", "excluded"
# Harvey moves a row on only from these; anything else was set by hand.
OVERWRITABLE = {"", SENT, CLICKED}
# Prospect statuses that already mean "no more email".
_STOPPED = {"replied", "opted_out", "lost", "meeting", "closed", IN_SHEET_STATUS}

SENT_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def sheet_enabled(config) -> bool:
    return bool(getattr(getattr(config, "sheet", None), "enabled", False))


def _label(config) -> str:
    return str(getattr(getattr(config, "sheet", None), "label", "harvey") or "harvey").strip().lower()


def _host(value: str) -> str:
    """A bare lowercase host from a domain or a URL: 'https://www.acme.example/x' -> 'acme.example'."""
    value = (value or "").strip().lower()
    value = re.sub(r"^[a-z][a-z0-9+.-]*://", "", value)
    value = re.split(r"[/?#:\s]", value, maxsplit=1)[0].strip(".")
    if value.startswith("www."):
        value = value[4:]
    return value if "." in value else ""


def _domain_hit(host: str, domains) -> str:
    if not host:
        return ""
    for d in domains:
        if host == d or host.endswith("." + d):
            return d
    return ""


async def sheet_suppression(state) -> dict | None:
    """What the last read of the sheet found, or None if it was never read."""
    raw = await state.get_setting(SUPPRESS_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return {"domains": set(data.get("domains", [])), "emails": set(data.get("emails", [])),
            "read_at": data.get("read_at", "")}


async def sheet_match(state, config, prospect) -> str:
    """The sheet entry (address or domain) this prospect is already under, or ""."""
    if not sheet_enabled(config) or prospect is None:
        return ""
    found = await sheet_suppression(state)
    if not found:
        return ""
    email = (getattr(prospect, "email", "") or "").strip().lower()
    if email and email in found["emails"]:
        return email
    company = None
    if getattr(prospect, "company_id", ""):
        try:
            company = await state.get_company(prospect.company_id)
        except Exception:
            company = None
    for host in (_host(getattr(company, "domain", "") or ""),
                 _host(getattr(company, "website", "") or ""),
                 ref_domain(None, email)):
        hit = _domain_hit(host, found["domains"])
        if hit:
            return hit
    return ""


async def sheet_domains(state, config) -> list[str]:
    """Every company domain in the sheet, for discovery to skip."""
    if not sheet_enabled(config):
        return []
    found = await sheet_suppression(state)
    return sorted(found["domains"]) if found else []


async def sheet_hold_reason(state, config) -> str:
    """Why first emails must wait for the sheet, or ""."""
    if not sheet_enabled(config):
        return ""
    if await sheet_suppression(state) is None:
        return ("waiting for the lead sheet to be read, so nobody already in it is "
                "emailed. `harvey sheet` shows what's wrong.")
    return ""


def _format_sent_at(value) -> str:
    try:
        when = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime(SENT_AT_FORMAT)


def _format_local(value, zone: str) -> str:
    if not zone:
        return ""
    try:
        import pytz
        when = datetime.fromisoformat(str(value))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.astimezone(pytz.timezone(zone)).strftime("%Y-%m-%d %a %H:%M %Z")
    except Exception:
        return ""


def _city_country(location: str) -> tuple[str, str]:
    parts = [p.strip() for p in (location or "").split(",") if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[-1]
    return (parts[0] if parts else ""), ""


class Sheet:
    """One read of the worksheet: header lookups and Harvey's own rows."""

    def __init__(self, values: list[list[str]], label: str):
        if not values or not any(v.strip() for v in values[0]):
            raise ValueError("the sheet has no header row")
        self.header = [h.strip().lower() for h in values[0]]
        self.rows = values[1:]
        self.cols = {}
        for i, name in enumerate(self.header):
            self.cols.setdefault(name, i)
        missing = [c for c in ("email", "status") if c not in self.cols]
        if missing:
            raise ValueError(f"the sheet has no {' or '.join(missing)} column")
        self.label = label

    def cell(self, row: list[str], name: str) -> str:
        i = self.cols.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    def is_ours(self, row: list[str]) -> bool:
        return self.label in (self.cell(row, "source").lower(), self.cell(row, "template_used").lower())

    def ours(self) -> dict[str, tuple[int, list[str]]]:
        """Harvey's rows by address: (sheet row number, cells)."""
        found = {}
        for offset, row in enumerate(self.rows, start=2):
            email = self.cell(row, "email").lower()
            if email and self.is_ours(row):
                found.setdefault(email, (offset, row))
        return found

    def suppression(self) -> dict:
        from harvey.state import FREE_MAIL_DOMAINS
        domains, emails = set(), set()
        for row in self.rows:
            for host in (_host(self.cell(row, "domain")), _host(self.cell(row, "website"))):
                if host and host not in FREE_MAIL_DOMAINS:
                    domains.add(host)
            email = self.cell(row, "email").lower()
            if "@" in email:
                emails.add(email)
        return {"domains": sorted(domains), "emails": sorted(emails)}

    def line(self, values: dict) -> list[str]:
        width = max(self.cols.values()) + 1 if self.cols else 0
        out = [""] * width
        for name, value in values.items():
            i = self.cols.get(name)
            if i is not None:
                out[i] = "" if value is None else str(value)
        return out


class SheetSync:
    def __init__(self, state, config, env, sender=None, client=None):
        self.state = state
        self.config = config
        self.sender = sender
        if client is None:
            from harvey.integrations.sheets import SheetsClient
            client = SheetsClient(
                getattr(env, "google_sheet_id", ""), getattr(env, "google_sheets_credentials", ""),
                config.sheet.worksheet)
        self.client = client

    @property
    def enabled(self) -> bool:
        return sheet_enabled(self.config)

    async def _read(self) -> Sheet:
        try:
            return Sheet(await self.client.read(), _label(self.config))
        except ValueError as e:
            from harvey.integrations.sheets import SheetsError
            raise SheetsError(str(e)) from None

    async def run(self) -> dict:
        """Read the sheet (clicks, your statuses, who's in it), then write
        Harvey's news back. Never raises: a failure is logged and kept for
        `harvey status`, and the last good read stays in force."""
        summary = {}
        if not self.enabled:
            return summary
        try:
            summary.update(await self.pull())
            summary.update(await self.push())
        except Exception as e:
            await self._failed(e)
            return summary
        await self.state.delete_setting(SHEET_PROBLEM_KEY)
        return summary

    async def push_only(self) -> dict:
        if not self.enabled:
            return {}
        try:
            summary = await self.push()
        except Exception as e:
            await self._failed(e)
            return {}
        await self.state.delete_setting(SHEET_PROBLEM_KEY)
        return summary

    async def _failed(self, e: Exception):
        logger.warning(f"Lead sheet: sync failed: {e}")
        await self.state.set_setting(SHEET_PROBLEM_KEY, str(e)[:300])

    # ── sheet -> Harvey ──

    async def pull(self) -> dict:
        sheet = await self._read()
        found = sheet.suppression()
        found["read_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        await self.state.set_setting(SUPPRESS_KEY, json.dumps(found))

        from harvey.config import follow_up_mode
        clicked_mode = follow_up_mode(self.config) == "clicked"
        counts = {"clicked": 0, "follow_ups_queued": 0, "stopped": 0}
        for email, (_, row) in sheet.ours().items():
            status = sheet.cell(row, "status").lower()
            if status in OVERWRITABLE - {CLICKED}:
                continue
            prospect = await self.state.get_prospect_by_email(email)
            if prospect is None:
                continue
            if status == CLICKED:
                if not await self.state.get_setting(CLICKED_KEY + prospect.id):
                    await self.state.set_setting(
                        CLICKED_KEY + prospect.id, datetime.now(timezone.utc).date().isoformat())
                    counts["clicked"] += 1
                    await self.state.log_action(
                        action_type="link_clicked", agent="sheet",
                        details={"prospect_id": prospect.id, "email": email})
                if clicked_mode and self.sender is not None:
                    if await self.sender.stage_follow_up(prospect.id):
                        counts["follow_ups_queued"] += 1
                        logger.info(f"Lead sheet: {email} clicked; follow-up queued.")
            elif status == BOUNCED:
                if prospect.email_status != "invalid":
                    await self.state.update_prospect_email(prospect.id, prospect.email, "invalid")
                    await self.state.cancel_pending_outbox_for_prospect(
                        prospect.id, reason="bounced (lead sheet)")
                    counts["stopped"] += 1
            elif status in (REPLIED, UNSUBSCRIBED, EXCLUDED):
                if prospect.status not in _STOPPED:
                    new = {REPLIED: "replied", UNSUBSCRIBED: "opted_out", EXCLUDED: "lost"}[status]
                    await self.state.update_prospect_status(prospect.id, new)
                    await self.state.cancel_pending_outbox_for_prospect(
                        prospect.id, reason=f"{status} (lead sheet)")
                    if status != EXCLUDED:
                        await self.state.stop_company(prospect.id)
                    counts["stopped"] += 1
        if counts["clicked"] or counts["follow_ups_queued"] or counts["stopped"]:
            logger.info(
                f"Lead sheet: {counts['clicked']} new click(s), {counts['follow_ups_queued']} "
                f"follow-up(s) queued, {counts['stopped']} stopped by the sheet.")
        return counts

    # ── Harvey -> sheet ──

    async def push(self) -> dict:
        sheet = await self._read()
        ours = sheet.ours()
        new_rows, recorded = [], []
        for item in await self.state.get_sent_sequence_items(1):
            pid = item["prospect_id"]
            if await self.state.get_setting(SHEET_ROW_KEY + pid):
                continue
            email = (item.get("to_email") or "").strip().lower()
            if email in ours:
                await self.state.set_setting(SHEET_ROW_KEY + pid, email)
                continue
            prospect = await self.state.get_prospect(pid)
            if prospect is None:
                continue
            new_rows.append(sheet.line(await self._row_for(item, prospect)))
            recorded.append((pid, email))
        if new_rows:
            await self.client.append_rows(new_rows)
            for pid, email in recorded:
                await self.state.set_setting(SHEET_ROW_KEY + pid, email)
            logger.info(f"Lead sheet: added {len(new_rows)} row(s) for emails Harvey sent.")
            sheet = await self._read()
            ours = sheet.ours()

        cells, noted = [], []
        status_col = sheet.cols["status"] + 1
        notes_col = sheet.cols.get("notes")
        for email, (row_no, row) in ours.items():
            prospect = await self.state.get_prospect_by_email(email)
            if prospect is None:
                continue
            current = sheet.cell(row, "status").lower()
            wanted = self._status_for(prospect)
            if wanted and current in OVERWRITABLE and current != wanted:
                cells.append((row_no, status_col, wanted))
            if notes_col is not None and not await self.state.get_setting(
                    SHEET_FOLLOW_UP_KEY + prospect.id):
                follow_up = await self.state.get_sequence_item(prospect.id, 2, status="sent")
                if follow_up:
                    day = _format_sent_at(follow_up.get("sent_at"))[:10]
                    note = f"{_label(self.config).capitalize()} follow-up sent {day}"
                    old = sheet.cell(row, "notes")
                    cells.append((row_no, notes_col + 1, f"{old}; {note}" if old else note))
                    noted.append(prospect.id)
        if cells:
            await self.client.write_cells(cells)
            for pid in noted:
                await self.state.set_setting(SHEET_FOLLOW_UP_KEY + pid, "1")
        return {"rows_added": len(new_rows), "cells_updated": len(cells)}

    @staticmethod
    def _status_for(prospect) -> str:
        if (prospect.email_status or "") == "invalid":
            return BOUNCED
        if prospect.status == "opted_out":
            return UNSUBSCRIBED
        if prospect.status in ("replied", "meeting", "closed", "lost"):
            return REPLIED
        return ""

    async def _row_for(self, item: dict, prospect) -> dict:
        company = None
        if prospect.company_id:
            try:
                company = await self.state.get_company(prospect.company_id)
            except Exception:
                company = None
        city, country = _city_country(getattr(company, "location", "") or "")
        links = _URL_RE.findall(item.get("body") or "")
        label = _label(self.config)
        return {
            "company": prospect.company or getattr(company, "name", ""),
            "website": getattr(company, "website", "") or "",
            "domain": ref_domain(company, prospect.email),
            "category": prospect.industry or getattr(company, "industry", "") or "",
            "city": city,
            "country": country,
            "email": prospect.email,
            "contact_name": "" if prospect.is_company_inbox else prospect.full_name(),
            "contact_role": prospect.title,
            "personalized_email": f"{item.get('subject', '')}\n\n{item.get('body', '')}".strip(),
            "was_personalized": "TRUE",
            "status": SENT,
            "template_used": label,
            "source": label,
            "sent_at": _format_sent_at(item.get("sent_at")),
            "sent_at_local": _format_local(item.get("sent_at"), prospect.timezone),
            "landing_url": links[0].rstrip(".,)") if links else "",
            "verification_status": prospect.email_status,
            "gmail_thread_id": item.get("thread_ref") or "",
        }
