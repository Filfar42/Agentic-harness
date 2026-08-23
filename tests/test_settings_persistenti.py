"""Le impostazioni devono sopravvivere al riavvio.

Erano l'unico pezzo di stato che non lo faceva: conversazioni e memorie stavano
gia' su disco, le preferenze ripartivano da DEFAULTS ogni volta.
"""

from __future__ import annotations

import json

from core.config import DEFAULTS
from core.prompts import SYSTEM_PROMPT
from core.settings import (
    VOLATILE_KEYS,
    load_settings,
    persistable,
    pick_available_model,
    save_settings,
)


def test_un_giro_completo_di_salvataggio_e_ricarica(tmp_path):
    f = tmp_path / "s.json"
    scelte = load_settings(f)
    scelte["model_name"] = "qwen3.5:latest"
    scelte["num_ctx"] = 32768
    scelte["native_think"] = "high"
    scelte["sandbox"] = "host"
    assert save_settings(scelte, f)

    riletto = load_settings(f)
    assert riletto["model_name"] == "qwen3.5:latest"
    assert riletto["num_ctx"] == 32768
    assert riletto["native_think"] == "high"
    assert riletto["sandbox"] == "host"


def test_una_chiave_nuova_arriva_col_suo_default_nuovo(tmp_path):
    """Il merge sui default e' quello che evita di congelare la versione
    vecchia: un file salvato prima che la chiave esistesse non deve lasciarla
    mancante, ne' impedire che il default nuovo la raggiunga."""
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"model_name": "vecchio:7b"}), encoding="utf-8")

    riletto = load_settings(f)
    assert riletto["model_name"] == "vecchio:7b"     # la sua scelta vince
    for chiave, default in DEFAULTS.items():
        if chiave != "model_name":
            assert riletto[chiave] == default        # il resto e' aggiornato


def test_una_chiave_sparita_dal_codice_non_sopravvive(tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"opzione_di_tre_versioni_fa": True}), encoding="utf-8")
    assert "opzione_di_tre_versioni_fa" not in load_settings(f)


def test_il_prompt_stock_non_viene_congelato():
    """Salvarlo significherebbe che ogni miglioramento futuro al prompt non
    arriva mai all'utente, senza che nulla glielo dica."""
    stock = dict(DEFAULTS, system_prompt=SYSTEM_PROMPT)
    assert "system_prompt" not in persistable(stock)

    mio = dict(DEFAULTS, system_prompt="Sei un pirata. Rispondi in rima.")
    assert persistable(mio)["system_prompt"] == "Sei un pirata. Rispondi in rima."


def test_lo_stato_del_turno_non_e_una_preferenza():
    vivo = dict(DEFAULTS, agent_running=True, pending_prompt="ciao", last_usage={"a": 1})
    salvato = persistable(vivo)
    assert not (VOLATILE_KEYS & salvato.keys())


def test_un_file_corrotto_non_impedisce_l_avvio(tmp_path):
    f = tmp_path / "s.json"
    f.write_text("{ questo non e' json", encoding="utf-8")
    assert load_settings(f)["model_name"] == DEFAULTS["model_name"]

    f.write_text('["una lista, non un oggetto"]', encoding="utf-8")
    assert load_settings(f)["num_ctx"] == DEFAULTS["num_ctx"]


def test_un_valore_del_tipo_sbagliato_viene_ignorato(tmp_path):
    """Il file e' modificabile a mano: un num_ctx stringa farebbe esplodere
    int(...) molto lontano da qui."""
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"num_ctx": "tantissimo", "temperature": 0.9}), encoding="utf-8")
    riletto = load_settings(f)
    assert riletto["num_ctx"] == DEFAULTS["num_ctx"]
    assert riletto["temperature"] == 0.9


def test_senza_file_si_parte_dai_default(tmp_path):
    assert load_settings(tmp_path / "mai-scritto.json") == DEFAULTS


# --- il default che invecchia -------------------------------------------------


def test_il_modello_configurato_che_non_esiste_piu_viene_sostituito():
    s = {"model_name": "qwen2.5-coder:7b"}
    assert pick_available_model(s, ["qwen3.5:latest", "llama3:8b"]) == "qwen3.5:latest"


def test_un_modello_presente_non_si_tocca():
    s = {"model_name": "llama3:8b"}
    assert pick_available_model(s, ["qwen3.5:latest", "llama3:8b"]) is None


def test_il_tag_diverso_conta_come_lo_stesso_modello():
    """'qwen3.5' e 'qwen3.5:latest' sono la stessa cosa: meglio quello che
    ripiegare sul primo della lista, che potrebbe essere tutt'altro."""
    s = {"model_name": "qwen3.5"}
    assert pick_available_model(s, ["llama3:8b", "qwen3.5:latest"]) == "qwen3.5:latest"


def test_col_server_spento_non_si_riscrivono_le_preferenze():
    """Lista vuota = Ollama non risponde, non 'non hai modelli'. Sovrascrivere
    la scelta dell'utente in quel momento sarebbe un danno permanente."""
    s = {"model_name": "qwen3.5:latest"}
    assert pick_available_model(s, []) is None


# --- la suite non gira sulle preferenze vere della macchina ------------------
# Prima il percorso del file stava nella firma (`path: Path = SETTINGS_FILE`),
# cioe' valutato all'import: spostarlo altrove era impossibile, e i test
# leggevano e riscrivevano le impostazioni dell'utente. Due conseguenze viste:
# con la sandbox lasciata su 'host' cinque test di prontezza fallivano da soli,
# e un giro di pytest riempiva le "cartelle recenti" di percorsi temporanei.


def test_il_percorso_si_risolve_alla_chiamata(tmp_path, monkeypatch):
    from core import settings as settings_mod

    altrove = tmp_path / "altrove.json"
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", altrove)
    assert settings_mod.save_settings({**DEFAULTS, "num_ctx": 4096}) is True
    assert altrove.is_file()
    assert settings_mod.load_settings()["num_ctx"] == 4096


def test_la_suite_gira_su_un_file_isolato():
    """Il conftest lo sposta per tutti: se questa asserzione cade, qualcuno ha
    tolto la fixture e i test hanno ricominciato a scrivere sul file vero."""
    from core import config as config_mod
    from core import settings as settings_mod

    assert settings_mod.SETTINGS_FILE != config_mod.SETTINGS_FILE
