"""Marking a click by hand: `harvey clicked <domain>` or the dashboard's
Clicked button records it and queues email 2 for approval."""

import pytest

from harvey.agents.writer import DO_NOT_CONTACT_STATUS, Writer
from harvey.clicks import click_target, contacted, find, mark_clicked
from harvey.config import ComplianceConfig
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.sheet_sync import CLICKED_KEY
from tests.test_click_follow_ups import make_sender, seed_campaign, sent_first_email
from tests.test_outbox_native import state  # noqa: F401  (fixture)
from tests.test_writer_cta import Brain, config as writer_config


@pytest.mark.parametrize("text,target", [
    ("acme.com", "acme.com"),
    ("www.Acme.com", "acme.com"),
    ("https://ours.example/freight/?ref=acme.com", "acme.com"),
    ("/?ref=acme.com&utm=x", "acme.com"),
    ("Jane@Acme.com", "jane@acme.com"),
    ("not a domain", ""),
])
def test_what_was_clicked(text, target):
    assert click_target(text) == target


async def contacted_prospect(state, email="jane@acme.com", domain="acme.com"):
    cid = await state.add_company(Company(name="Acme", domain=domain))
    pid = await state.add_prospect(Prospect(
        first_name="Jane", last_name="Doe", title="Owner", company="Acme", company_id=cid,
        email=email, email_status="verified", email_verified=True, status="new"))
    campaign = await seed_campaign(state, [pid])
    await sent_first_email(state, pid, campaign.id)
    return await state.get_prospect(pid)


@pytest.mark.asyncio
async def test_a_click_by_domain_queues_email_two(state):
    prospect = await contacted_prospect(state)
    [found] = await find(state, click_target("https://ours.example/?ref=acme.com"))
    assert found.id == prospect.id
    message = await mark_clicked(state, make_sender(state), make_sender(state).config, found)
    assert "Outbox" in message
    assert await state.get_setting(CLICKED_KEY + prospect.id)
    item = await state.get_sequence_item(prospect.id, 2)
    assert item["status"] == "pending_review"
    # Marking again does nothing new.
    again = await mark_clicked(state, make_sender(state), make_sender(state).config, found)
    assert "already" in again
    assert [p.id for p, _, clicked in await contacted(state) if clicked] == [prospect.id]


@pytest.mark.asyncio
async def test_nobody_matches_a_domain_harvey_never_emailed(state):
    await contacted_prospect(state)
    assert await find(state, "other.example") == []
    assert await find(state, "") == []


@pytest.mark.asyncio
async def test_with_follow_ups_off_the_click_is_only_recorded(state):
    prospect = await contacted_prospect(state)
    sender = make_sender(state, mode="off")
    message = await mark_clicked(state, sender, sender.config, prospect)
    assert "'off'" in message
    assert await state.get_sequence_item(prospect.id, 2) is None


@pytest.mark.asyncio
async def test_the_writer_skips_the_do_not_contact_list(state):
    cfg = writer_config(cta=None)
    cfg.writer.cta = None
    cfg.compliance = ComplianceConfig(do_not_contact=["dock.test"])
    cid = await state.add_company(Company(name="Dock Co", domain="dock.test"))
    pid = await state.add_prospect(Prospect(
        first_name="Ana", last_name="Lee", title="Owner", company="Dock Co", company_id=cid,
        email="ana@dock.test", email_status="verified"))
    brain = Brain({"subject": "s", "body": "b"})
    await Writer(brain, state, cfg).run()
    assert brain.prompts == []
    assert (await state.get_prospect(pid)).status == DO_NOT_CONTACT_STATUS
