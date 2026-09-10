"""Choix du fournisseur IA.

Le catalogue existe pour epargner a l'utilisateur la recherche d'une URL de
base : il choisit un nom, on connait l'adresse. Mais « custom » reste ouvert —
tout service parlant l'API OpenAI fonctionnera, y compris ceux qui n'existent
pas encore.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .base import AIProposal, AmbiguousItem, Resolver, kind_from_ai

logger = logging.getLogger(__name__)

__all__ = [
    "PROVIDERS",
    "AIProposal",
    "AmbiguousItem",
    "Resolver",
    "build_resolver",
    "kind_from_ai",
]


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    key: str
    label: str
    base_url: str
    """Vide pour Anthropic (SDK dedie) et pour « custom » (saisie libre)."""

    default_model: str
    needs_key: bool
    hint: str

    models: tuple[str, ...] = ()
    """Modeles proposes dans l'interface pour ce fournisseur.

    Une liste FIGEE, et non recuperee par appel : la plupart de ces services
    exposent des centaines de modeles dont trois conviennent a la tache, et
    demander la liste imposerait une cle valide avant meme de pouvoir choisir.
    Le champ reste libre a la saisie — c'est ce qui permet d'utiliser un modele
    sorti apres cette version sans attendre une mise a jour."""


# Tous parlent l'API OpenAI sauf Claude. C'est ce qui permet de couvrir sept
# services avec deux implementations — et d'en accueillir d'autres sans code.
PROVIDERS: tuple[ProviderInfo, ...] = (
    ProviderInfo(
        "anthropic",
        "Claude",
        "",
        "claude-opus-5",
        True,
        "console.anthropic.com",
        ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"),
    ),
    ProviderInfo(
        "openai",
        "ChatGPT",
        "https://api.openai.com/v1",
        "gpt-4o-mini",
        True,
        "platform.openai.com",
        ("gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"),
    ),
    ProviderInfo(
        "mistral",
        "Mistral",
        "https://api.mistral.ai/v1",
        "mistral-small-latest",
        True,
        "console.mistral.ai",
        ("mistral-small-latest", "mistral-large-latest", "open-mistral-nemo"),
    ),
    ProviderInfo(
        "gemini",
        "Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "gemini-2.0-flash",
        True,
        "aistudio.google.com — via le point d'entrée compatible OpenAI",
        ("gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-1.5-pro"),
    ),
    ProviderInfo(
        "openrouter",
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        "google/gemini-2.0-flash-001",
        True,
        "openrouter.ai — passerelle vers des centaines de modèles",
        (
            "google/gemini-2.0-flash-001",
            "anthropic/claude-sonnet-5",
            "meta-llama/llama-3.3-70b-instruct",
        ),
    ),
    ProviderInfo(
        "groq",
        "Groq",
        "https://api.groq.com/openai/v1",
        "llama-3.3-70b-versatile",
        True,
        "console.groq.com",
        ("llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"),
    ),
    ProviderInfo(
        "ollama",
        "Ollama (local)",
        "http://host.docker.internal:11434/v1",
        "llama3.1",
        False,
        "Aucune clé. Depuis un conteneur, vise l'hôte et non localhost.",
        ("llama3.1", "qwen2.5", "mistral"),
    ),
    ProviderInfo(
        "custom",
        "Autre (compatible OpenAI)",
        "",
        "",
        False,
        "Toute API parlant /chat/completions : LM Studio, vLLM, Together…",
    ),
)

BY_KEY = {p.key: p for p in PROVIDERS}


def resolver_status(
    provider: str, api_key: str, model: str, base_url: str = ""
) -> tuple[bool, str]:
    """Le resolveur est-il utilisable, et sinon POURQUOI.

    Toutes les raisons de renoncer sont silencieuses cote serveur — elles
    partent dans les journaux, que personne ne lit avant d'avoir un doute.
    Quelqu'un qui coche « activer » et ne voit rien se passer n'a aucun moyen
    de savoir s'il manque une cle, si le modele est vide, ou si simplement
    aucun fichier n'etait assez ambigu pour justifier un appel.
    """
    info = BY_KEY.get(provider)
    if info is None:
        return False, f"fournisseur inconnu : {provider}"
    if info.needs_key and not api_key:
        return False, "aucune clé enregistrée"
    if not (model or info.default_model):
        return False, "aucun modèle indiqué"
    if provider == "anthropic":
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False, "le paquet « anthropic » est absent de l'image"
    elif not (base_url or info.base_url):
        return False, "aucune adresse de service"
    return True, ""


def build_resolver(provider: str, api_key: str, model: str, base_url: str = "") -> Resolver | None:
    """Construit le resolveur demande, ou None s'il est inutilisable.

    Renvoyer None plutot que lever : un resolveur mal configure doit degrader
    vers la revue manuelle, pas empecher un scan de tourner.
    """
    info = BY_KEY.get(provider)
    if info is None:
        logger.warning("fournisseur IA inconnu : %s", provider)
        return None

    if info.needs_key and not api_key:
        logger.warning("fournisseur IA « %s » sans cle ; resolveur desactive", provider)
        return None

    chosen_model = model or info.default_model
    if not chosen_model:
        logger.warning("aucun modele indique pour « %s » ; resolveur desactive", provider)
        return None

    if provider == "anthropic":
        try:
            from .anthropic_resolver import AnthropicResolver

            return AnthropicResolver(api_key, chosen_model)
        except ImportError:
            logger.warning(
                "le paquet `anthropic` est absent ; installe l'extra « ai » ou "
                "choisis un fournisseur compatible OpenAI"
            )
            return None

    url = base_url or info.base_url
    if not url:
        logger.warning("aucune URL de base pour « %s » ; resolveur desactive", provider)
        return None

    from .openai_compatible import OpenAICompatibleResolver

    return OpenAICompatibleResolver(
        base_url=url, api_key=api_key, model=chosen_model, name=provider
    )
