"""What the trainer's analysis reads: every page of the site except legal
boilerplate, in crawl order, within one prompt-sized budget."""

from harvey.trainer import ANALYSIS_CHARS, PAGE_CHARS, Trainer


def trainer(pages):
    t = Trainer.__new__(Trainer)
    t.scraped_pages = pages
    return t


def test_every_product_page_is_read():
    pages = {f"https://acme.example/page-{n}/": f"content {n} " * 400 for n in range(30)}
    text = trainer(pages)._get_all_content()
    assert all(f"--- PAGE: /page-{n}/ ---" in text for n in range(30))


def test_legal_pages_are_left_out():
    text = trainer({
        "https://acme.example/": "home",
        "https://acme.example/privacy/": "policy",
        "https://acme.example/terms-of-service": "terms",
        "https://acme.example/legal/dpa": "dpa",
        "https://acme.example/blog/privacy-for-small-firms/": "a post",
        "https://acme.example/termsheet-guide/": "a guide",
    })._get_all_content()
    assert "policy" not in text and "terms\n" not in text and "dpa" not in text
    assert "a post" in text and "a guide" in text


def test_a_long_page_is_cut_and_the_total_is_capped():
    pages = {f"https://acme.example/p{n}": "x" * (PAGE_CHARS * 2) for n in range(40)}
    text = trainer(pages)._get_all_content()
    assert len(text) <= ANALYSIS_CHARS
    assert "x" * (PAGE_CHARS + 1) not in text
    assert trainer({"https://acme.example/": "y" * 50})._get_all_content(max_chars=20) == (
        "\n\n--- PAGE: / ---\nyy")
