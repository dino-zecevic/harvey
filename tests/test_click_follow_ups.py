"""channels.email.follow_ups: "clicked" sends email 2 only to people who clicked;
"off" sends the first email only. And the first email carries the CTA link."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from harvey.agents.sender import FOLLOW_UPS_OFF_REASON, NOT_CLICKED_REASON, Sender
from harvey.config import CtaConfig
from harvey.models.campaign import Campaign, EmailStep
from harvey.sheet_sync import CLICKED_KEY
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    Cfg, Env, FakeProvider, seed_prospect, state,
)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def make_sender(state, mode="clicked", cta=None, require_approval=True):
    cfg = Cfg()
    cfg.channels = SimpleNamespace(email=SimpleNamespace(
        enabled=True, provider="smtp", max_daily_sends=50, send_to_risky=False,
        require_approval=require_approval, max_bounce_rate=0.05, follow_ups=mode))
    cfg.writer = SimpleNamespace(cta=cta or CtaConfig())
    sender = Sender(brain=None, state=state, config=cfg, env=Env())
    sender.provider = FakeProvider()
    sender.send_pacing = False
    return sender


async def seed_campaign(state, pids):
    campaign = Campaign(id="", name="c", channel="email", status="draft", prospect_ids=pids, sequence=[
        EmailStep(step=1, subject="hi {{first_name}}", body="Hi {{first_name}}.\n\n{{cta_url}}\n\nQuestion?",
                  delay_days=0),
        EmailStep(step=2, subject="again", body="Another angle {{first_name}}. {{cta_url}}", delay_days=3),
        EmailStep(step=3, subject="last", body="Closing the loop.", delay_days=4),
    ])
    campaign.id = await state.add_campaign(campaign)
    return campaign


async def sent_first_email(state, pid, campaign_id, days_ago=4):
    item = await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="s1", body="One. Question?",
        send_at=(_now() - timedelta(days=days_ago)).isoformat(), status="sent",
        campaign_id=campaign_id, step=1)
    await state.update_outbox_item(item, sent_at=(_now() - timedelta(days=days_ago)).isoformat())
    await state.update_prospect_status(pid, "contacted")
    return item


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["clicked", "off"])
async def test_only_the_first_email_is_staged(state, mode):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])
    await make_sender(state, mode)._run_native()
    assert [i["step"] for i in await state.get_outbox()] == [1]


@pytest.mark.asyncio
async def test_auto_stages_the_whole_sequence(state):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])
    await make_sender(state, "auto")._run_native()
    assert sorted(i["step"] for i in await state.get_outbox()) == [1, 2, 3]


@pytest.mark.asyncio
async def test_the_first_email_gets_the_link_and_follow_ups_dont(state):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])
    cta = CtaConfig(default="https://ours.example/?ref={domain}")
    await make_sender(state, "auto", cta=cta)._run_native()
    items = {i["step"]: i for i in await state.get_outbox()}
    assert "https://ours.example/?ref=acme.com" in items[1]["body"]
    assert "cta_url" not in items[1]["body"] + items[2]["body"]
    assert "https://" not in items[2]["body"]


@pytest.mark.asyncio
async def test_a_click_queues_email_two_after_its_delay(state):
    pid = await seed_prospect(state)
    campaign = await seed_campaign(state, [pid])
    await sent_first_email(state, pid, campaign.id, days_ago=1)
    sender = make_sender(state)
    item_id = await sender.stage_follow_up(pid)
    item = await state.get_outbox_item(item_id)
    assert item["step"] == 2 and item["status"] == "pending_review"
    assert item["body"] == "Another angle Jane."            # placeholder dropped
    assert datetime.fromisoformat(item["send_at"]) > _now() + timedelta(days=1)  # 3 days after email 1
    assert await sender.stage_follow_up(pid) is None        # once only


@pytest.mark.asyncio
async def test_on_autopilot_the_follow_up_is_approved(state):
    pid = await seed_prospect(state)
    campaign = await seed_campaign(state, [pid])
    await sent_first_email(state, pid, campaign.id)
    item_id = await make_sender(state, require_approval=False).stage_follow_up(pid)
    item = await state.get_outbox_item(item_id)
    assert item["status"] == "approved" and item["approved_by"] == "autopilot"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["replied", "opted_out", "bounced", "unsent"])
async def test_no_follow_up_for_someone_who_cant_get_one(state, change):
    pid = await seed_prospect(state)
    campaign = await seed_campaign(state, [pid])
    if change != "unsent":
        await sent_first_email(state, pid, campaign.id)
    if change in ("replied", "opted_out"):
        await state.update_prospect_status(pid, change)
    if change == "bounced":
        await state.update_prospect_email(pid, "jane@acme.com", "invalid")
    assert await make_sender(state).stage_follow_up(pid) is None


@pytest.mark.asyncio
async def test_a_follow_up_without_a_click_is_held_and_comes_back_on_a_click(state):
    # Staged under "auto", then the mode changed to "clicked".
    pid = await seed_prospect(state)
    campaign = await seed_campaign(state, [pid])
    await sent_first_email(state, pid, campaign.id)
    two = await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="s2", body="Two. Question?",
        send_at=(_now() - timedelta(hours=1)).isoformat(), status="approved",
        campaign_id=campaign.id, step=2)
    sender = make_sender(state)
    await sender._drain_due()
    assert sender.provider.sent == []
    held = await state.get_outbox_item(two)
    assert held["status"] == "cancelled" and held["error"] == NOT_CLICKED_REASON

    await state.set_setting(CLICKED_KEY + pid, "2026-01-01")
    assert await sender.stage_follow_up(pid) == two
    back = await state.get_outbox_item(two)
    assert back["status"] == "pending_review" and back["error"] == ""
    await state.approve_outbox(two)
    await state.update_outbox_item(two, send_at=(_now() - timedelta(minutes=1)).isoformat())
    await sender._drain_due()
    assert [m["subject"] for m in sender.provider.sent] == ["s2"]


@pytest.mark.asyncio
async def test_clicked_mode_never_sends_email_three(state):
    pid = await seed_prospect(state)
    campaign = await seed_campaign(state, [pid])
    await sent_first_email(state, pid, campaign.id, days_ago=10)
    await state.set_setting(CLICKED_KEY + pid, "2026-01-01")
    three = await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="s3", body="Three. Question?",
        send_at=(_now() - timedelta(hours=1)).isoformat(), status="approved",
        campaign_id=campaign.id, step=3)
    sender = make_sender(state)
    await sender._drain_due()
    assert sender.provider.sent == []
    assert (await state.get_outbox_item(three))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_off_cancels_a_queued_follow_up(state):
    pid = await seed_prospect(state)
    campaign = await seed_campaign(state, [pid])
    await sent_first_email(state, pid, campaign.id)
    two = await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="s2", body="Two. Question?",
        send_at=(_now() - timedelta(hours=1)).isoformat(), status="approved",
        campaign_id=campaign.id, step=2)
    sender = make_sender(state, "off")
    await sender._drain_due()
    assert sender.provider.sent == []
    assert (await state.get_outbox_item(two))["error"] == FOLLOW_UPS_OFF_REASON
