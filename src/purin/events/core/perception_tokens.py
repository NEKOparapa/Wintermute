"""Count observation text with the offline tokenizer published by DeepSeek."""

import gzip
from functools import cache
from importlib.resources import files

from tokenizers import Tokenizer


@cache
def _tokenizer() -> Tokenizer:
    asset = files(__package__).joinpath("deepseek_v4_tokenizer.json.gz")
    return Tokenizer.from_buffer(gzip.decompress(asset.read_bytes()))


def count_tokens(text: str) -> int:
    return len(_tokenizer().encode(text, add_special_tokens=False)) if text else 0
