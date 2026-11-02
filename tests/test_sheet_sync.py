"""The lead sheet: Harvey writes its sent emails in, reads clicks and your
statuses back, and never emails a company that's already in it."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from harvey.agents.sender import Sender
from harvey.config import CtaConfig, SheetConfig
from harvey.models.campaign import Campaign, EmailStep
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.sheet_sync import (
    CLICKED_KEY, IN_SHEET_STATUS, SHEET_PROBLEM_KEY, SheetSync, sheet_domains,
    sheet_hold_reason, sheet_match,
)
from tests.test_outbox_native import Cfg, Env, FakeProvider, state  # noqa: F401

HEADER = ["company", "website", "domain", "city", "country", "email", "contact_name",
          "status", "template_used", "sent_at", "notes", "landing_url", "source", "extra"]


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class FakeSheet:
    """In-memory worksheet with the client's three calls."""

    def __init__(self, rows=None, header=HEADER):
        self.values = [list(header)] + [list(r) for r in (rows or [])]
        self.appended = 0
        self.fail = None

    def row(self, **cells):
        return [cells.get(h, "") for h in HEADER]

    async def read(self):
        if self.fail:
            raise self.fail
        return [list(r) for r in self.values]

    async def write_cells(self, cells):
        for r, c, v in cells:
            row = self.values[r - 1]
            row.extend([""] * (c - len(row)))
            row[c - 1] = v
        return len(cells)

    async def append_rows(self, rows):
        self.values.extend(list(r) for r in rows)
        self.appended += len(rows)
        return len(rows)

    def get(self, email, col):
        i = HEADER.index(col)
        for row in self.values[1:]:
            if row[HEADER.index("email")] == email:
                return row[i] if i < len(row) else ""
        return None


def config(mode="clicked"):
    cfg = Cfg()
    cfg.channels = SimpleNamespace(email=SimpleNamespace(
        enabled=True, provider="smtp", max_daily_sends=50, send_to_risky=False,
        require_approval=True, max_bounce_rate=0.05, follow_ups=mode))
    cfg.writer = SimpleNamespace(cta=CtaConfig())
    cfg.sheet = SheetConfig(enabled=True)
    return cfg


def sync_for(state, sheet, mode="clicked"):
    cfg = config(mode)
    sender = Sender(brain=None, state=state, config=cfg, env=Env())
    sender.provider = FakeProvider()
    sender.send_pacing = False
    return SheetSync(state, cfg, Env(), sender=sender, client=sheet), sender


async def prospect(state, email="ana@harbour.test", domain="harbour.test", status="new"):
    cid = await state.add_company(Company(name="Harbour Freight Co", domain=domain,
                                          website=f"https://{domain}", location="Leeds, UK"))
    return await state.add_prospect(Prospect(
        first_name="Ana", last_name="Lee", title="Owner", company="Harbour Freight Co",
        company_id=cid, email=email, email_status="verified", status=status, industry="Freight"))


async def sent(state, pid, campaign_id="", email="ana@harbour.test", step=1, days_ago=4):
    item = await state.add_outbox_item(
        prospect_id=pid, to_email=email, subject=f"s{step}",
        body=f"Body {step}.\n\nhttps://ours.example/?ref=harbour.test",
        send_at=(_now() - timedelta(days=days_ago)).isoformat(), status="sent",
        campaign_id=campaign_id, step=step)
    await state.update_outbox_item(item, sent_at=(_now() - timedelta(days=days_ago)).isoformat(),
                                   thread_ref="thread-1")
    await state.update_prospect_status(pid, "contacted")
    return item


async def campaign(state, pid):
    c = Campaign(id="", name="c", status="active", prospect_ids=[pid], sequence=[
        EmailStep(step=1, subject="a", body="One?", delay_days=0),
        EmailStep(step=2, subject="b", body="Two {{first_name}}?", delay_days=3),
    ])
    c.id = await state.add_campaign(c)
    return c


# ── who's already in the sheet ──

@pytest.mark.asyncio
async def test_first_emails_wait_until_the_sheet_has_been_read(state):
    cfg = config()
    assert await sheet_hold_reason(state, cfg)
    sync, _ = sync_for(state, FakeSheet())
    await sync.run()
    assert await sheet_hold_reason(state, cfg) == ""


@pytest.mark.asyncio
async def test_anyone_in_the_sheet_is_matched_by_domain_or_address(state):
    sheet = FakeSheet()
    sheet.values += [sheet.row(domain="harbour.test", status="sent"),
                     sheet.row(website="https://www.dock.test/about", status="new"),
                     sheet.row(email="kim@gmail.com", domain="gmail.com", status="excluded")]
    sync, _ = sync_for(state, sheet)
    await sync.pull()
    cfg = config()
    assert await sheet_match(state, cfg, await state.get_prospect(await prospect(state))) == "harbour.test"
    dock = await prospect(state, email="jo@mail.dock.test", domain="mail.dock.test")
    assert await sheet_match(state, cfg, await state.get_prospect(dock)) == "dock.test"
    kim = await state.add_prospect(Prospect(first_name="Kim", last_name="Ng", title="Owner",
                                            email="kim@gmail.com", email_status="verified"))
    assert await sheet_match(state, cfg, await state.get_prospect(kim)) == "kim@gmail.com"
    # A free-mail domain in the sheet doesn't block everyone on that provider.
    other = await state.add_prospect(Prospect(first_name="Lu", last_name="Ma", title="Owner",
                                              email="lu@gmail.com", email_status="verified"))
    assert await sheet_match(state, cfg, await state.get_prospect(other)) == ""
    assert "gmail.com" not in await sheet_domains(state, cfg)


@pytest.mark.asyncio
async def test_a_first_email_to_a_company_in_the_sheet_is_cancelled(state):
    sheet = FakeSheet()
    sheet.values.append(sheet.row(domain="harbour.test", status="new"))
    sync, sender = sync_for(state, sheet)
    await sync.pull()
    pid = await prospect(state, status="queued")
    item = await state.add_outbox_item(
        prospect_id=pid, to_email="ana@harbour.test", subject="s", body="Hi. Question?",
        send_at=(_now() - timedelta(minutes=1)).isoformat(), status="approved", campaign_id="c", step=1)
    await sender._drain_due()
    assert sender.provider.sent == []
    assert "already in your lead sheet" in (await state.get_outbox_item(item))["error"]
    assert (await state.get_prospect(pid)).status == IN_SHEET_STATUS


@pytest.mark.asyncio
async def test_before_the_first_read_a_first_email_waits_rather_than_goes(state):
    sync, sender = sync_for(state, FakeSheet())
    pid = await prospect(state, status="queued")
    item = await state.add_outbox_item(
        prospect_id=pid, to_email="ana@harbour.test", subject="s", body="Hi. Question?",
        send_at=(_now() - timedelta(minutes=1)).isoformat(), status="approved", campaign_id="c", step=1)
    await sender._drain_due()
    assert sender.provider.sent == []
    assert (await state.get_outbox_item(item))["status"] == "approved"
    await sync.pull()
    await sender._drain_due()
    assert len(sender.provider.sent) == 1


# ── Harvey -> sheet ──

@pytest.mark.asyncio
async def test_a_sent_first_email_becomes_a_row(state):
    sheet = FakeSheet()
    sync, _ = sync_for(state, sheet)
    pid = await prospect(state)
    await sent(state, pid)
    await sync.push()
    assert sheet.appended == 1
    row = dict(zip(HEADER, sheet.values[1]))
    assert row["email"] == "ana@harbour.test" and row["status"] == "sent"
    assert row["source"] == row["template_used"] == "harvey"
    assert row["domain"] == "harbour.test" and row["city"] == "Leeds" and row["country"] == "UK"
    assert row["landing_url"] == "https://ours.example/?ref=harbour.test"
    assert row["sent_at"].endswith("Z") and row["contact_name"] == "Ana Lee"
    await sync.push()
    assert sheet.appended == 1          # once only


@pytest.mark.asyncio
async def test_a_row_already_there_isnt_added_twice(state):
    sheet = FakeSheet()
    sheet.values.append(sheet.row(email="ana@harbour.test", status="sent", source="harvey"))
    sync, _ = sync_for(state, sheet)
    await sent(state, await prospect(state))
    await sync.push()
    assert sheet.appended == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change, expected", [
    ("opted_out", "unsubscribed"), ("replied", "replied"), ("lost", "replied"), ("bounce", "bounced"),
])
async def test_what_the_mailbox_says_reaches_the_sheet(state, change, expected):
    sheet = FakeSheet()
    sheet.values.append(sheet.row(email="ana@harbour.test", status="clicked", source="harvey"))
    sync, _ = sync_for(state, sheet)
    pid = await prospect(state)
    await sent(state, pid)
    if change == "bounce":
        await state.update_prospect_email(pid, "ana@harbour.test", "invalid")
    else:
        await state.update_prospect_status(pid, change)
    await sync.push()
    assert sheet.get("ana@harbour.test", "status") == expected


@pytest.mark.asyncio
async def test_a_status_you_set_by_hand_stays(state):
    sheet = FakeSheet()
    sheet.values.append(sheet.row(email="ana@harbour.test", status="excluded", source="harvey"))
    sync, _ = sync_for(state, sheet)
    pid = await prospect(state)
    await sent(state, pid)
    await state.update_prospect_status(pid, "opted_out")
    await sync.push()
    assert sheet.get("ana@harbour.test", "status") == "excluded"


@pytest.mark.asyncio
async def test_a_sent_follow_up_is_noted(state):
    sheet = FakeSheet()
    sheet.values.append(sheet.row(email="ana@harbour.test", status="clicked", source="harvey",
                                  notes="called once"))
    sync, _ = sync_for(state, sheet)
    pid = await prospect(state)
    await sent(state, pid)
    await sent(state, pid, step=2, days_ago=0)
    await sync.push()
    await sync.push()
    assert sheet.get("ana@harbour.test", "notes").startswith("called once; Harvey follow-up sent ")
    assert sheet.get("ana@harbour.test", "notes").count("follow-up") == 1


# ── sheet -> Harvey ──

@pytest.mark.asyncio
async def test_a_click_queues_the_follow_up(state):
    sheet = FakeSheet()
    sheet.values.append(sheet.row(email="ana@harbour.test", status="clicked", source="harvey"))
    sync, _ = sync_for(state, sheet)
    pid = await prospect(state)
    c = await campaign(state, pid)
    await sent(state, pid, campaign_id=c.id)
    summary = await sync.run()
    assert summary["clicked"] == 1 and summary["follow_ups_queued"] == 1
    assert await state.get_setting(CLICKED_KEY + pid)
    follow_up = await state.get_sequence_item(pid, 2)
    assert follow_up["status"] == "pending_review" and follow_up["body"] == "Two Ana?"
    again = await sync.run()
    assert again["clicked"] == 0 and again["follow_ups_queued"] == 0


@pytest.mark.asyncio
async def test_a_click_on_a_row_harvey_didnt_send_does_nothing(state):
    sheet = FakeSheet()
    sheet.values.append(sheet.row(email="ana@harbour.test", status="clicked", source=""))
    sync, _ = sync_for(state, sheet)
    pid = await prospect(state)
    c = await campaign(state, pid)
    await sent(state, pid, campaign_id=c.id)
    await sync.pull()
    assert await state.get_sequence_item(pid, 2) is None


@pytest.mark.asyncio
async def test_in_auto_mode_a_click_is_recorded_but_queues_nothing_extra(state):
    sheet = FakeSheet()
    sheet.values.append(sheet.row(email="ana@harbour.test", status="clicked", source="harvey"))
    sync, _ = sync_for(state, sheet, mode="auto")
    pid = await prospect(state)
    c = await campaign(state, pid)
    await sent(state, pid, campaign_id=c.id)
    await sync.pull()
    assert await state.get_setting(CLICKED_KEY + pid)
    assert await state.get_sequence_item(pid, 2) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status, check", [
    ("bounced", lambda p: p.email_status == "invalid"),
    ("replied", lambda p: p.status == "replied"),
    ("unsubscribed", lambda p: p.status == "opted_out"),
    ("excluded", lambda p: p.status == "lost"),
])
async def test_your_status_stops_them(state, status, check):
    sheet = FakeSheet()
    sheet.values.append(sheet.row(email="ana@harbour.test", status=status, source="harvey"))
    sync, _ = sync_for(state, sheet)
    pid = await prospect(state)
    await sent(state, pid)
    queued = await state.add_outbox_item(
        prospect_id=pid, to_email="ana@harbour.test", subject="s2", body="Two?",
        send_at=_now().isoformat(), status="pending_review", campaign_id="c", step=2)
    await sync.pull()
    assert check(await state.get_prospect(pid))
    assert (await state.get_outbox_item(queued))["status"] == "cancelled"


# ── failures ──

@pytest.mark.asyncio
async def test_a_sync_that_fails_is_noted_and_doesnt_raise(state):
    sheet = FakeSheet()
    sheet.fail = RuntimeError("network down")
    sync, _ = sync_for(state, sheet)
    assert await sync.run() == {}
    assert "network down" in await state.get_setting(SHEET_PROBLEM_KEY)
    sheet.fail = None
    await sync.run()
    assert await state.get_setting(SHEET_PROBLEM_KEY) == ""


@pytest.mark.asyncio
async def test_a_sheet_without_the_columns_it_needs_is_a_clear_error(state):
    sync, _ = sync_for(state, FakeSheet(header=["company", "website"]))
    await sync.run()
    assert "no email or status column" in await state.get_setting(SHEET_PROBLEM_KEY)


@pytest.mark.asyncio
async def test_off_means_nothing_is_read_or_held(state):
    cfg = config()
    cfg.sheet = SheetConfig()
    assert await sheet_hold_reason(state, cfg) == ""
    sheet = FakeSheet()
    sync = SheetSync(state, cfg, Env(), client=sheet)
    assert await sync.run() == {}
