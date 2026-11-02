"""persona.signature: one sign-off block under every email Harvey sends,
never twice, above the postal address and opt-out line."""

from types import SimpleNamespace

from harvey.agents.sender import Sender
from harvey.agents.writer import SIGNATURE_RULE, footer_rule
from harvey.config import ComplianceConfig, PersonaConfig, outgoing_footer
from harvey.signature import sign

SIG = "Sam Rivera\nFounder · Example Co\nexample.com"


def persona(signature=SIG):
    return PersonaConfig(name="Sam Rivera", company="Example Co", role="Founder",
                         email="sam@example.com", linkedin="", tone="plain", signature=signature)


def config(signature=SIG, address="1 Main St, Springfield", opt_out="Reply no thanks to opt out."):
    return SimpleNamespace(persona=persona(signature),
                           compliance=ComplianceConfig(postal_address=address, opt_out_line=opt_out))


# ── sign() ──

def test_no_signature_changes_nothing():
    assert sign("Hi Jo,\n\nA question?", "") == "Hi Jo,\n\nA question?"


def test_the_signature_goes_under_the_question():
    assert sign("Hi Jo,\n\nA question?", SIG, "Sam Rivera") == f"Hi Jo,\n\nA question?\n\n{SIG}"


def test_a_sign_off_keeps_its_greeting_and_loses_the_name():
    out = sign("Hi Jo,\n\nA question?\n\nBest,\nSam", SIG, "Sam Rivera")
    assert out == f"Hi Jo,\n\nA question?\n\nBest,\n{SIG}"
    assert sign("Hi Jo,\n\nA question?\n\nSam", SIG, "Sam Rivera") == f"Hi Jo,\n\nA question?\n\n{SIG}"


def test_a_closing_that_isnt_the_sender_stays():
    out = sign("Hi Jo,\n\nThanks,\nThe team", SIG, "Sam Rivera")
    assert out == f"Hi Jo,\n\nThanks,\nThe team\n\n{SIG}"


def test_the_question_is_never_taken_for_a_sign_off():
    out = sign("Hi Jo,\n\nSam here, still on?", SIG, "Sam Rivera")
    assert out.startswith("Hi Jo,\n\nSam here, still on?")


def test_never_signed_twice():
    once = sign("Hi Jo,\n\nA question?", SIG, "Sam Rivera")
    assert sign(once, SIG, "Sam Rivera") == once


# ── config ──

def test_the_signature_is_tidied():
    assert persona("  Sam Rivera  \r\nFounder\n\n").signature == "Sam Rivera\nFounder"
    assert persona(None).signature == ""


def test_the_footer_is_signature_then_address_then_opt_out():
    assert outgoing_footer(config()) == (
        f"{SIG}\n\n1 Main St, Springfield\nReply no thanks to opt out.")
    assert outgoing_footer(config(signature="")) == "1 Main St, Springfield\nReply no thanks to opt out."
    assert outgoing_footer(config(address="", opt_out="")) == SIG


def test_the_writer_is_told_not_to_sign_off_only_when_there_is_a_signature():
    assert SIGNATURE_RULE in footer_rule(config(), "")
    assert SIGNATURE_RULE in footer_rule(config(), "operator rules")
    assert SIGNATURE_RULE not in footer_rule(config(signature=""), "")


# ── what leaves ──

def sender(cfg):
    s = Sender.__new__(Sender)
    s.config = cfg
    return s


def test_a_cold_email_leaves_signed_with_the_footer_under_it():
    body = sender(config())._outgoing_body({"kind": "sequence", "body": "Hi Jo,\n\nA question?"})
    assert body == (f"Hi Jo,\n\nA question?\n\n{SIG}\n\n"
                    "1 Main St, Springfield\nReply no thanks to opt out.")
    assert sender(config())._with_footer(body) == body


def test_a_reply_is_signed_without_the_footer():
    body = sender(config())._outgoing_body({"kind": "reply", "body": "Thanks Jo, Tuesday works."})
    assert body == f"Thanks Jo, Tuesday works.\n\n{SIG}"
