"""Name normalisation and matching tests (synthetic name fixtures)."""

from app.matching.names import (
    jockey_names_match,
    normalize_name,
    runner_names_match,
    strip_bracketed_suffixes,
    venue_names_match,
)


class TestNormalize:
    def test_capitalization(self):
        assert normalize_name("JAMES MCDONALD") == normalize_name("James McDonald")

    def test_punctuation_and_whitespace(self):
        assert normalize_name("  James   McDonald. ") == "james mcdonald"

    def test_apostrophes_all_variants(self):
        for s in ["O'Brien", "O’Brien", "O`Brien", "OBrien"]:
            assert normalize_name(s) == "obrien"

    def test_runner_number_prefixes(self):
        assert normalize_name("4. Fast Horse") == "fast horse"
        assert normalize_name("12) Fast Horse") == "fast horse"
        assert normalize_name("7 - Fast Horse") == "fast horse"

    def test_bracketed_suffixes(self):
        assert normalize_name("Winx (NZ)") == "winx"
        assert normalize_name("Some Horse (GB) (2)") == "some horse"
        assert strip_bracketed_suffixes("J Smith (a3)") == "J Smith"

    def test_unicode_folding(self):
        assert normalize_name("Café Métro") == "cafe metro"


class TestJockeyMatching:
    def test_exact(self):
        assert jockey_names_match("James McDonald", "James McDonald")

    def test_initial_vs_full(self):
        assert jockey_names_match("James McDonald", "J McDonald")
        assert jockey_names_match("J. McDonald", "James McDonald")

    def test_apprentice_claim_suffix(self):
        assert jockey_names_match("Tom Sherry (a)", "Tom Sherry")
        assert jockey_names_match("T Sherry (a1.5)", "Tom Sherry")

    def test_different_surnames_do_not_match(self):
        assert not jockey_names_match("J Smith", "James Smyth")

    def test_same_initial_different_first_names_do_not_match(self):
        assert not jockey_names_match("James McDonald", "Jason McDonald")

    def test_different_initial_same_surname_do_not_match(self):
        assert not jockey_names_match("James McDonald", "Will McDonald")

    def test_empty_never_matches(self):
        assert not jockey_names_match("", "James McDonald")


class TestRunnerMatching:
    def test_number_prefix_and_country_suffix(self):
        assert runner_names_match("4. Winx (NZ)", "WINX")

    def test_apostrophes(self):
        assert runner_names_match("Kingman's Pride", "Kingmans Pride")

    def test_different_horses_do_not_match(self):
        assert not runner_names_match("Fast Horse", "Fast Horses")


class TestVenueMatching:
    def test_subset_tokens(self):
        assert venue_names_match("Royal Randwick", "Randwick")
        assert venue_names_match("Sandown Hillside", "Sandown")

    def test_different_venues(self):
        assert not venue_names_match("Flemington", "Caulfield")
