"""Resolveur Claude.

Seul fournisseur a ne pas parler l'API OpenAI, d'ou une implementation a part.
Elle utilise le SDK officiel — dependance optionnelle, installee par l'extra
« ai » — ce qui donne acces aux sorties structurees validees par schema plutot
qu'a du JSON a rattraper au vol.
"""

from __future__ import annotations

import logging

from .base import SYSTEM_PROMPT, AIBatchResult, AIProposal, AmbiguousItem, user_message

logger = logging.getLogger(__name__)


class AnthropicResolver:
    name = "anthropic"

    def __init__(self, api_key: str, model: str = "claude-opus-5") -> None:
        # Import tardif : le paquet est optionnel et une installation sans lui
        # doit fonctionner normalement.
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def resolve(self, items: list[AmbiguousItem]) -> dict[int, AIProposal]:
        """Identifie un lot.

        Un echec n'est jamais fatal : les fichiers concernes partent en revue
        manuelle, ce qu'ils auraient fait sans resolveur.
        """
        if not items:
            return {}

        import anthropic

        try:
            response = self._client.messages.parse(
                model=self._model,
                max_tokens=16000,
                thinking={"type": "adaptive"},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message(items)}],
                output_format=AIBatchResult,
            )
        except anthropic.RateLimitError:
            logger.warning("resolveur Claude limite en debit ; lot renvoye en revue manuelle")
            return {}
        except anthropic.APIStatusError as exc:
            logger.warning("resolveur Claude indisponible (%s) ; revue manuelle", exc.status_code)
            return {}
        except anthropic.APIConnectionError:
            logger.warning("resolveur Claude injoignable ; revue manuelle")
            return {}

        if response.stop_reason == "refusal":
            logger.warning("resolveur Claude a refuse le lot ; revue manuelle")
            return {}

        result = response.parsed_output
        if result is None:
            return {}

        valid = {it.index for it in items}
        out: dict[int, AIProposal] = {}
        for proposal in result.proposals:
            if proposal.index not in valid:
                logger.debug("proposition ignoree, index hors lot : %s", proposal.index)
                continue
            if proposal.notes:
                logger.info("entree %s — note du resolveur : %s", proposal.index, proposal.notes)
            out[proposal.index] = proposal
        return out

    def close(self) -> None:
        pass
