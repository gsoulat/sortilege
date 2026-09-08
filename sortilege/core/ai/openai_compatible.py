"""Resolveur pour tout service parlant l'API OpenAI.

Une seule implementation couvre ChatGPT, Mistral, OpenRouter, Ollama, Groq,
DeepSeek, LM Studio, Together, et Gemini via son point d'entree compatible.
Tous exposent le meme ``POST /chat/completions`` ; seules l'URL de base, la
cle et le nom du modele changent.

C'est aussi ce qui permet a un service que je n'ai pas prevu de fonctionner :
il suffit qu'il respecte la meme forme.

Pas de SDK : httpx est deja une dependance, et chaque SDK proprietaire
ajouterait un paquet pour une seule requete POST.
"""

from __future__ import annotations

import logging

import httpx

from .base import SYSTEM_PROMPT, AIProposal, AmbiguousItem, parse_response, user_message

logger = logging.getLogger(__name__)

TIMEOUT = 120.0
"""Genereux : un modele local sur CPU met parfois une minute sur un lot."""


class OpenAICompatibleResolver:
    """Client minimal pour un point d'entree ``/chat/completions``."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        name: str = "openai",
        client: httpx.Client | None = None,
    ) -> None:
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._client = client or httpx.Client(timeout=TIMEOUT)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        # Ollama et LM Studio n'exigent pas de cle : envoyer un en-tete vide
        # ferait echouer certaines implementations strictes.
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def resolve(self, items: list[AmbiguousItem]) -> dict[int, AIProposal]:
        if not items:
            return {}

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message(items)},
            ],
            "temperature": 0,
            # Demande le mode JSON quand le service le connait. Les services
            # qui l'ignorent le rejettent parfois : voir la reprise plus bas.
            "response_format": {"type": "json_object"},
        }

        raw = self._post(payload)
        if raw is None:
            # Certains services (Ollama selon le modele, quelques passerelles)
            # refusent response_format. On retente sans plutot que d'abandonner
            # — le prompt demande deja du JSON explicitement.
            payload.pop("response_format", None)
            raw = self._post(payload)

        if raw is None:
            return {}

        return parse_response(raw, {it.index for it in items})

    def _post(self, payload: dict) -> str | None:
        try:
            response = self._client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            logger.warning("%s : reponse %s du service IA", self.name, exc.response.status_code)
            return None
        except httpx.HTTPError as exc:
            logger.warning("%s injoignable (%s)", self.name, type(exc).__name__)
            return None
        except ValueError:
            logger.warning("%s : reponse illisible", self.name)
            return None

        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            logger.warning("%s : reponse de forme inattendue", self.name)
            return None

    def close(self) -> None:
        self._client.close()
