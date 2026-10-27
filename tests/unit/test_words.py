"""The words for random addresses: friendly, easy to type, and the same list in the add-on and on the relay."""

from pathlib import Path

from airbound.settings import WORDS_FILE, address_words

RELAY_COPY = Path(__file__).resolve().parents[2] / "relay-server" / "controller" / "airbound_relay" / "words.txt"

# Never in a word, not even inside another one (woodcock, peacock, ...).
BLOCKED_PARTS = (
    "cock", "dick", "sex", "porn", "nude", "tit", "boob", "butt", "arse", "anal", "cum", "slut", "whore", "pecker",
    "god", "hell", "devil", "demon", "satan", "angel", "jesus", "allah", "saint", "holy", "church", "pray",
    "kill", "dead", "death", "die", "gun", "bomb", "war", "nazi", "hate", "slave", "drug", "weed", "coke", "crack",
    "kuk", "pikk", "fitte", "faen", "hore", "drit", "fart", "piss", "poop", "shit", "fuck",
)
# Whole words that are fine on their own somewhere, but not in an address.
BLOCKED_WORDS = {
    "black", "white", "brown", "yellow", "red", "swallow", "skunk", "bunny", "easy", "date", "passion", "swing", "pot",
    "organ", "loon", "cuckoo", "eggplant", "cherub", "cardinal", "anthem", "pagoda", "merlin", "jaguar", "frisbee",
    "cracker", "cucumber",
}


def test_every_word_follows_the_rules():
    words = address_words()
    assert len(words) >= 900  # about 900 million three-word combinations at least
    assert len(set(words)) == len(words), "duplicates"
    for word in words:
        assert word.isascii() and word.isalpha() and word.islower(), word
        assert 3 <= len(word) <= 8, word
        assert word not in BLOCKED_WORDS, word
        assert not any(part in word for part in BLOCKED_PARTS if part not in ALLOWED_INSIDE.get(word, ())), word


# Innocent words that contain a blocked part.
ALLOWED_INSIDE = {
    "warm": ("war",), "warbler": ("war",), "wardrobe": ("war",), "butter": ("butt",), "button": ("butt",),
    "shore": ("hore",), "titanium": ("tit",), "skillet": ("kill",), "hoodie": ("die",),
}


def test_the_relay_uses_the_same_list():
    assert RELAY_COPY.read_bytes() == WORDS_FILE.read_bytes()
