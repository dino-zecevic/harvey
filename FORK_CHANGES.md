# Changes in this fork

What this fork adds to [ethanplusai/harvey](https://github.com/ethanplusai/harvey).
Every feature is off (or behaves as upstream) until its setting is turned on,
and each one has tests. Pull upstream updates with `git pull upstream main`.

## 1. A CTA link in every first email (`writer.cta`)

One link in each first email, picked by Harvey (never by the model) from what
it knows about the business: its industry first, then its name and
description, else a default. `{domain}` becomes the company's website domain
(`?ref={domain}`), and the `ref` parameter is dropped when no domain is known
or the address is a free mailbox.

```yaml
writer:
  cta:
    default: "https://example.com/?ref={domain}"
    links:
      - url: "https://example.com/freight/?ref={domain}"
        keywords: ["freight", "logistic", "haulage"]
```

The model places a `{{cta_url}}` placeholder; Harvey swaps in the link, drops
duplicates, and adds the link before the sign-off if the placeholder is
missing. Follow-ups carry no link.

Files: `harvey/cta.py`, `harvey/agents/writer.py`, `harvey/agents/sender.py`,
`harvey/config.py` (`CtaConfig`, `CtaLinkConfig`). Tests: `tests/test_cta.py`,
`tests/test_writer_cta.py`.

## 2. Follow-ups only for people who clicked (`channels.email.follow_ups`)

- `auto`: the whole sequence until they reply (upstream behaviour, default).
- `clicked`: only email 2, only to people marked as clicked; email 3 never.
- `off`: the first email only.

Email 2 is queued at least the campaign's delay after email 1 actually went
out, inside the send window, with the usual approval.

Files: `harvey/agents/sender.py` (`stage_follow_up`), `harvey/state.py`
(`get_sequence_item`, `get_sent_sequence_items`, `get_campaign`).
Tests: `tests/test_click_follow_ups.py`.

## 3. Marking a click by hand (`harvey clicked`)

For links tracked with `?ref=<domain>` in your own visit logs:

```
harvey clicked acme.example                 # by company domain
harvey clicked https://example.com/?ref=acme.example   # or the pasted link
harvey clicked                              # list who got email 1, and who clicked
```

The dashboard's Contacts tab has a **Clicked** button on everyone who got
email 1. Either way the click is recorded and, with `follow_ups: clicked`,
email 2 waits in the Outbox for approval.

Files: `harvey/clicks.py`, `harvey/cli.py`, `harvey/dashboard.py`
(`POST /api/prospects/{id}/clicked`), `harvey/web/app.js`. Tests: `tests/test_clicks.py`.

## 4. Google Sheet lead sync (`sheet`, optional)

Syncs with a Google Sheet of leads through a service account
(`pip install -e '.[sheets]'`, `GOOGLE_SHEET_ID` and
`GOOGLE_SHEETS_CREDENTIALS` in `.env`; a relative key path resolves from
Harvey's folder, and `secrets/` is gitignored).

- Companies and addresses already in the sheet are never emailed first, and
  discovery skips them.
- Each sent first email is appended as a row (source = `sheet.label`).
- `clicked` / `replied` / `unsubscribed` / `bounced` / `excluded` on Harvey's
  rows flow back.
- `harvey sheet` checks the connection; `harvey sheet sync` syncs once.

Files: `harvey/integrations/sheets.py`, `harvey/sheet_sync.py`, `harvey/main.py`,
`harvey/collectors/discover.py`, `pyproject.toml`. Tests: `tests/test_sheet_sync.py`,
`tests/test_sheets_client.py`.

## 5. A fixed signature (`persona.signature`)

```yaml
persona:
  signature: |
    Sam Rivera
    Founder · Example Co
    example.com
```

Added under every email at send time (cold email and replies), above the
postal address and opt-out line. The writer is told not to sign off; a draft
that does anyway ("Best,\nSam") keeps its "Best," and has the name replaced,
so nothing is signed twice. The Outbox preview shows it.

Files: `harvey/signature.py`, `harvey/agents/sender.py`, `harvey/agents/writer.py`,
`harvey/dashboard.py`, `harvey/web/app.js`. Tests: `tests/test_persona_signature.py`.

## 6. Postal address only for some countries (`compliance.postal_address_countries`)

```yaml
compliance:
  postal_address: "1 Main St, Springfield"
  postal_address_countries: ["US", "CA"]   # empty = every cold email
```

CAN-SPAM (US) and CASL (Canada) ask for a postal address; UK and EU rules
don't. A recipient whose country Harvey can't be sure of still gets it. The
Outbox preview and "Open in Gmail" show each email's own footer.

Files: `harvey/config.py`, `harvey/agents/sender.py`, `harvey/dashboard.py`,
`harvey/web/app.js`. Tests: `tests/test_postal_countries.py`.

## 7. Training reads the whole site

`harvey train` used to analyse only the first ~10 crawled pages (about 60k
characters, 8k per page). It now reads every crawled page up to ~200k
characters (15k per page) and skips legal pages (privacy, terms, cookies,
imprint, DPA, EULA).

Files: `harvey/trainer.py`. Tests: `tests/test_trainer_content.py`.

## 8. Choosing the Claude account (`CLAUDE_CONFIG_DIR` in `.env`)

With two Claude subscriptions, where the second runs as
`CLAUDE_CONFIG_DIR=~/.claude-second claude` (often via a shell alias Harvey
can't see), set `CLAUDE_CONFIG_DIR` in `.env`. It wins over the shell that
starts Harvey, the quota gauge reads only that login's token (it used to
fall back to the default login), and `harvey status --check` shows the
account and plan in use.

Files: `harvey/claude_runtime.py` (`pin_account`, `account_status`),
`harvey/integrations/quota.py`, `harvey/cli.py`, `harvey/trainer.py`.
Tests: `tests/test_claude_account.py`.

## 9. Do-not-contact prospects are never drafted

Prospects on `compliance.do_not_contact` are now skipped by the writer
(status `do_not_contact`), not only blocked at send time, so no draft for
them reaches the Outbox.

Files: `harvey/agents/writer.py`, `harvey/agents/sender.py`. Tests: `tests/test_clicks.py`.

## Docs

`harvey.yaml` and `.env.example` document every new setting.
