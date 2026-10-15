"""Meaning: where a text sits among the others by what it says, not by its words.

A small multilingual embedding model (EmbeddingGemma, run by ONNX Runtime on the CPU)
turns a text into a vector, and texts that say the same thing in different words end
up close. Recall searches by meaning alongside words, so "a dollar in euro" finds
"USD/EUR rates". The model lives in ``ZOE_HOME/models/embeddinggemma`` and needs the
``meaning`` extra (numpy, onnxruntime, tokenizers); without them recall goes by words
alone.

``python -m zoe.meaning`` downloads the model when it is missing, from one fixed
revision of its Hugging Face repository with every file checked against its SHA-256,
and places every neuron that is not placed yet. EmbeddingGemma is Google's, under the
Gemma Terms of Use.
"""

from __future__ import annotations

import hashlib
import logging
import urllib.request
from pathlib import Path

log = logging.getLogger("zoe.meaning")

MODEL = "embeddinggemma"
# One fixed revision of the model's repository, and the SHA-256 every file must have: a file that
# changed upstream, or arrived damaged, is refused instead of loaded.
_REPO = ("https://huggingface.co/onnx-community/embeddinggemma-300m-ONNX/resolve/"
         "5090578d9565bb06545b4552f76e6bc2c93e4a66")
_FILES = {
    "tokenizer.json": "4dda02faaf32bc91031dc8c88457ac272b00c1016cc679757d1c441b248b9c47",
    "onnx/model_quantized.onnx": "172efde319fe1542dc41f31be6154910b05b78f7a861c265c4600eec906bd6d8",
    "onnx/model_quantized.onnx_data": "705626e28e4c23c82ade34566b4197d97f534c12275fa406dfb71e9937d388c0",
}
# The prompts the model was trained with, for what is searched and for what is searched through.
_QUERY = "task: search result | query: "
_DOCUMENT = "title: none | text: "
_TOKENS = 512
_BATCH = 8


class Meaning:
    """The embedding model, loaded once per process; safe to share across threads."""

    name = MODEL

    def __init__(self, folder: Path) -> None:
        import numpy
        import onnxruntime
        import tokenizers

        self._np = numpy
        self._tokenizer = tokenizers.Tokenizer.from_file(str(folder / "tokenizer.json"))
        self._tokenizer.enable_truncation(max_length=_TOKENS)
        self._tokenizer.enable_padding(pad_id=self._tokenizer.token_to_id("<pad>") or 0, pad_token="<pad>")
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 4
        self._session = onnxruntime.InferenceSession(
            str(folder / "onnx" / "model_quantized.onnx"), options, providers=["CPUExecutionProvider"]
        )

    def _embed(self, texts: list[str]):
        np = self._np
        # Texts of similar length go together, so a batch pads little.
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        rows = [None] * len(texts)
        for start in range(0, len(order), _BATCH):
            chunk = order[start:start + _BATCH]
            encoded = self._tokenizer.encode_batch([texts[i] for i in chunk])
            feed = {
                "input_ids": np.array([e.ids for e in encoded], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in encoded], dtype=np.int64),
            }
            batch = self._session.run(["sentence_embedding"], feed)[0].astype(np.float32)
            for i, row in zip(chunk, batch / np.linalg.norm(batch, axis=1, keepdims=True)):
                rows[i] = row
        return np.vstack(rows)

    def documents(self, texts: list[str]) -> list[bytes]:
        """Each text's place in meaning, ready to be stored."""
        return [row.tobytes() for row in self._embed([_DOCUMENT + text for text in texts])]

    def rank(self, query: str, vectors: list[bytes], limit: int) -> list[int]:
        """The positions of the stored places closest in meaning to the query, closest first."""
        if not vectors:
            return []
        np = self._np
        places = np.frombuffer(b"".join(vectors), dtype=np.float32).reshape(len(vectors), -1)
        closeness = places @ self._embed([_QUERY + query])[0]
        return [int(i) for i in np.argsort(-closeness)[:limit]]


def load(models: Path) -> Meaning | None:
    """The model kept in ``models``, or None when it or its libraries are missing."""
    folder = models / MODEL
    if not all((folder / name).exists() for name in _FILES):
        log.info("no meaning model in %s: recall goes by words alone", folder)
        return None
    try:
        return Meaning(folder)
    except ImportError as exc:
        log.warning("meaning search is off, its libraries are missing: %s", exc)
        return None


def download(models: Path) -> None:
    """Fetch the model files that are missing, each kept only when its SHA-256 is the expected one."""
    folder = models / MODEL
    for name, expected in _FILES.items():
        target = folder / name
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        log.info("downloading %s", name)
        partial = target.with_suffix(target.suffix + ".part")
        urllib.request.urlretrieve(f"{_REPO}/{name}", partial)
        digest = hashlib.sha256()
        with partial.open("rb") as data:
            for chunk in iter(lambda: data.read(1 << 20), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            partial.unlink()
            raise ValueError(f"{name} is not the expected file (SHA-256 {digest.hexdigest()}): it was refused")
        partial.rename(target)


def main() -> None:
    from . import access
    from .brain import Brain
    from .config import Config
    from .db import apply_schema, connect

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    download(cfg.data_dir / "models")
    Brain.meaning = load(cfg.data_dir / "models")
    if Brain.meaning is None:
        raise SystemExit("the meaning model could not be loaded")
    conn = connect(cfg.db_path)
    apply_schema(conn)
    placed = sum(Brain(conn, owner).fill(limit=None) for owner in [None, *(p.id for p, _ in access.people(conn))])
    print(f"{placed} neurons placed by meaning")


if __name__ == "__main__":
    main()
