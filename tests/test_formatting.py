"""Checks for the writing standard enforced on every reply."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zoe.formatting import split, to_plain, to_telegram


def test_markdown_becomes_telegram_html():
    assert to_telegram("**Totale** e *nota* con `id_42`") == "<b>Totale</b> e <i>nota</i> con <code>id_42</code>"
    assert to_telegram("[sito](https://example.com/a_b_c)") == '<a href="https://example.com/a_b_c">sito</a>'
    assert to_telegram("~~vecchio~~ e ||segreto||") == "<s>vecchio</s> e <tg-spoiler>segreto</tg-spoiler>"


def test_code_is_left_untouched():
    assert to_telegram("```python\nx = a_b * c_d — 2\n```") == '<pre><code class="language-python">x = a_b * c_d — 2</code></pre>'


def test_plain_symbols_are_not_mistaken_for_markup():
    assert to_telegram("snake_case_name e 2*3*4") == "snake_case_name e 2*3*4"
    assert to_telegram("A → B, -5 gradi") == "A → B, -5 gradi"


def test_telegram_html_from_the_model_is_kept_and_anything_else_escaped():
    assert to_telegram("<b>ok</b> <script>x</script>") == "<b>ok</b> &lt;script&gt;x&lt;/script&gt;"
    assert to_telegram("<blockquote expandable>dettagli</blockquote>") == "<blockquote expandable>dettagli</blockquote>"
    assert to_telegram("1 &lt; 2 & 3") == "1 &lt; 2 &amp; 3"


def test_long_and_en_dashes_are_replaced():
    assert to_telegram("Ciao — come va") == "Ciao, come va"
    assert to_telegram("dalle 14:00–20:00") == "dalle 14:00-20:00"


def test_emoji_ending_a_sentence_ends_its_line():
    assert to_telegram("Fatto ✅ Il tasto Disconnect non c'è più.") == "Fatto ✅\nIl tasto Disconnect non c'è più."
    assert to_telegram("<b>Pronto 👍</b> Ti mando il file") == "<b>Pronto 👍</b>\nTi mando il file"
    assert to_telegram("Spedito 📦.") == "Spedito 📦."


def test_emoji_inside_a_sentence_stays_in_place():
    line = "• Per il tuo \"Ciao\" ho messo la reazione 😴, che Telegram accetta"
    assert to_telegram(line + "\n• Altro punto") == line + "\n• Altro punto"
    assert to_telegram("Wait, 😴 isn't in the list.") == "Wait, 😴 isn't in the list."
    assert to_telegram("Mia ha scritto 👍 e poi è uscita") == "Mia ha scritto 👍 e poi è uscita"


def test_a_bullet_split_after_an_emoji_stays_one_item():
    text = "• Ordine spedito 📦 Arriva domani.\n• Fattura pronta"
    assert to_telegram(text) == "• Ordine spedito 📦\nArriva domani.\n• Fattura pronta"
    assert to_telegram("• Pagato ✅ Grazie Mia\nAltro paragrafo") == "• Pagato ✅\nGrazie Mia\n\nAltro paragrafo"


def test_emoji_opening_a_title_or_bullet_stays():
    assert to_telegram("<b>👤 Mario</b> (<code>123</code>)") == "<b>👤 Mario</b> (<code>123</code>)"
    assert to_telegram("• ✅ Pagato") == "• ✅ Pagato"


def test_headings_lists_quotes_and_tables():
    assert to_telegram("## Riepilogo") == "<b>Riepilogo</b>"
    assert to_telegram("- uno\n* due") == "• uno\n• due"
    assert to_telegram("> nota\n> seconda") == "<blockquote>nota\nseconda</blockquote>"
    table = "| Nome | Totale |\n|---|---|\n| Mario | 91 € |\n| Anna | 12 € |"
    assert to_telegram(table) == "<b>Nome, Totale</b>\n• Mario: 91 €\n• Anna: 12 €"


def test_blocks_get_breathing_room():
    text = "Ecco il riepilogo\n<b>📦 Ordine</b>\n• uno\n• due\nAltro paragrafo"
    assert to_telegram(text) == "Ecco il riepilogo\n\n<b>📦 Ordine</b>\n• uno\n• due\n\nAltro paragrafo"


def test_broken_markup_never_reaches_telegram():
    assert to_telegram("<b>aperto <i>male</b>") == "aperto male"


def test_split_respects_the_limit_at_block_boundaries():
    text = "\n\n".join(f"paragrafo {i} " + "x" * 50 for i in range(200))
    chunks = split(text, limit=1000)
    assert len(chunks) > 1 and all(len(c) <= 1000 for c in chunks)
    assert "\n\n".join(chunks) == text


def test_plain_text_for_speech():
    assert to_plain("**Fatto** ✅ Il file è <code>pronto</code>") == "Fatto\nIl file è pronto"


def _run() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run()
