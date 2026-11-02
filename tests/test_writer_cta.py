"""The Writer with writer.cta set: the model is told where the link goes, and
the staged first email carries the real link Harvey picked."""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey.agents.writer import Writer
from harvey.config import ComplianceConfig, CtaConfig, SheetConfig
from harvey.cta import CTA_RULE, CTA_SEQUENCE_RULE
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.sheet_sync import IN_SHEET_STATUS, SUPPRESS_KEY
from harvey.state import StateManager

CTA = CtaConfig(default="https://ours.example/?ref={domain}", links=[
    {"url": "https://ours.example/freight/?ref={domain}", "keywords": ["freight"]}])


class Brain:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "prompt"

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply


def config(cta=CTA, sheet=False):
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(provider="gmail", require_approval=True)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
        writer=SimpleNamespace(humanizer=False, email_rules="", cta=cta),
        compliance=ComplianceConfig(),
        sheet=SheetConfig(enabled=sheet),
    )


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "w.db"))
    await s.init_db()
    return s


async def prospect(state, industry="Freight forwarding"):
    cid = await state.add_company(Company(name="Dock Co", domain="dock.test"))
    pid = await state.add_prospect(Prospect(
        first_name="Ana", last_name="Lee", title="Owner", company="Dock Co", company_id=cid,
        industry=industry, email="ana@dock.test", email_status="verified"))
    return await state.get_prospect(pid)


@pytest.mark.asyncio
async def test_the_prompt_asks_for_the_placeholder_and_the_outbox_gets_the_link(state):
    reply = {"subject": "quick note", "body": "Hi Ana,\n\nAn observation.\n\nOverview:\n{{cta_url}}\n\nQuestion?\n\nSam"}
    brain = Brain(reply)
    w = Writer(brain, state, config())
    w.skills, w.email_rules = "", ""
    p = await prospect(state)
    await w._personalize_first_emails("c1", [p])
    assert CTA_RULE in brain.prompts[0]
    item = (await state.get_outbox())[0]
    assert "Overview:\nhttps://ours.example/freight/?ref=dock.test\n\nQuestion?" in item["body"]


@pytest.mark.asyncio
async def test_without_a_cta_nothing_about_links_changes(state):
    brain = Brain({"subject": "s", "body": "Hi Ana,\n\nQuestion?"})
    w = Writer(brain, state, config(cta=CtaConfig()))
    w.skills, w.email_rules = "", ""
    await w._personalize_first_emails("c1", [await prospect(state)])
    assert CTA_RULE not in brain.prompts[0]
    assert "https://" not in (await state.get_outbox())[0]["body"]


@pytest.mark.asyncio
async def test_the_sequence_prompt_puts_the_link_in_email_one_only(state):
    seq = [{"step": n, "subject": f"s{n}", "body": f"b{n} {{{{company}}}}", "delay_days": 0}
           for n in (1, 2, 3)]
    brain = Brain(seq)
    w = Writer(brain, state, config())
    w.skills, w.email_rules = "", ""
    await w._write_sequence([await prospect(state)])
    assert CTA_SEQUENCE_RULE in brain.prompts[0]


@pytest.mark.asyncio
async def test_a_prospect_already_in_the_sheet_is_skipped(state):
    await state.set_setting(SUPPRESS_KEY, '{"domains": ["dock.test"], "emails": [], "read_at": "x"}')
    brain = Brain({"subject": "s", "body": "b"})
    w = Writer(brain, state, config(sheet=True))
    p = await prospect(state)
    await w.run()
    assert brain.prompts == []
    assert (await state.get_prospect(p.id)).status == IN_SHEET_STATUS
