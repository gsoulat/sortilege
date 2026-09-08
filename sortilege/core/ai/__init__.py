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
    ),
    ProviderInfo(
        "openai",
        "ChatGPT",
        "https://api.openai.com/v1",
        "gpt-4o-mini",
        True,
        "platform.openai.com",
    ),
    ProviderInfo(
        "mistral",
        "Mistral",
        "https://api.mistral.ai/v1",
        "mistral-small-latest",
        True,
        "console.mistral.ai",
    ),
    ProviderInfo(
        "gemini",
        "Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "gemini-2.0-flash",
        True,
        "aistudio.google.com — via le point d'entrée compatible OpenAI",
    ),
    ProviderInfo(
        "openrouter",
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        "google/gemini-2.0-flash-001",
        True,
        "openrouter.ai — passerelle vers des centaines de modèles",
    ),
    ProviderInfo(
        "groq",
        "Groq",
        "https://api.groq.com/openai/v1",
        "llama-3.3-70b-versatile",
        True,
        "console.groq.com",
    ),
    ProviderInfo(
        "ollama",
        "Ollama (local)",
        "http://host.docker.internal:11434/v1",
        "llama3.1",
        False,
        "Aucune clé. Depuis un conteneur, vise l'hôte et non localhost.",
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
